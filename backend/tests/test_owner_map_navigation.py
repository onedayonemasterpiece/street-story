"""Photo GPS is a hint; owner navigation and selected geometry remain authoritative."""
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.fact_agents import agent_context
from street_story.live import StreetStoryLiveAdapter, ensure_live_schema
from street_story.live_author_intent import begin_turn
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.owner_group_identity import resolve_group
from street_story.place_selection import confirm_place, place_action, place_map, prepare_place_context
from street_story.service import ConflictError
from test_identity_recovery_policy import photo
from test_owner_map_selection import MapOSM, command, make_service


def manual(service):
    return service.create_story(key='intake', client_story_id='manual', photo_sha256='token',
        photo_mime_type='image/jpeg', photo_bytes=photo(), voice_protocol='voice-chunks-v2',
        lat=0, lon=0, identity_mode='manual')


def live(service, story):
    ensure_live_schema(service)
    adapter = StreetStoryLiveAdapter(service, lambda *_: None, lambda *_: None)
    session = SimpleNamespace(id='live_owner_navigation', resource_id=story['id'], state={
        'research_control_scope': {'expected_photo_sha256': story['photo_sha256'],
            'expected_identity_generation': story['identity_generation']}}, capability='place_map')
    return adapter, session


class NoCity(MapOSM):
    reverse_url = 'https://osm.test/reverse'

    async def _map_nearby(self, *args):
        raw = await super()._map_nearby(*args)
        for item in raw['elements']:
            item['tags'].pop('addr:city', None)
            item['tags'].pop('name', None)
        return raw


@pytest.mark.asyncio
@pytest.mark.parametrize('content', [b'invalid', photo(gps=False), photo(redacted=True)], ids=['invalid', 'missing', 'redacted'])
async def test_manual_missing_actual_exif_rejects_before_story_or_jobs(tmp_path, content):
    service, _ = make_service(tmp_path)
    app = create_app(service.settings, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/stories', headers={
            'Authorization': 'Bearer ' + reveal(service.settings.device_token), 'Idempotency-Key': 'bad'},
            data={'client_story_id': 'bad', 'photo_sha256': 'bad', 'identity_mode': 'manual',
                'voice_protocol': 'voice-chunks-v2', 'lat': '54.7', 'lon': '20.5'},
            files={'photo': ('photo.jpg', content, 'image/jpeg')})
    assert response.status_code == 422 and response.json()['error']['code'] == 'photo_gps_required'
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM stories').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_selected_15km_house_reverse_city_cached_and_exif_immutable(tmp_path):
    service, gemini = make_service(tmp_path)
    story = manual(service)
    assert story['camera_point']['latitude'] == pytest.approx(54.7016666667)
    calls = []
    def reverse(request):
        calls.append(request)
        return httpx.Response(200, json={'address': {'city': 'Selected City', 'road': 'Wrong Reverse Road'}})
    osm = NoCity()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reverse)) as http:
        osm.http = http
        service.providers.osm = osm
        snapshot = await place_map(service, story['id'], latitude=54.7152, longitude=20.5025)
        body = command(story, snapshot, 'osm:way:1', 'selected')
        await prepare_place_context(service, story['id'], body)
        await prepare_place_context(service, story['id'], body)
        result = confirm_place(service, story['id'], 'selected', body)
    assert len(calls) == 1
    assert float(calls[0].url.params['lat']) == pytest.approx(54.71525)
    anchor = result['visual_identity']['search_anchor']
    assert anchor['city'] == 'Selected City' and anchor['street'] == 'Street'
    assert anchor['city_provenance'] == 'osm.selected_center_reverse'
    assert result['place_selection']['location_basis'] == 'owner_relocated'
    assert 1450 < result['place_selection']['gps_offset_m'] < 1600
    assert result['camera_point'] == story['camera_point']
    assert result['visual_identity']['candidate_id'] == 'osm:way:1' and not gemini.identity_calls
    assert result['identity_generation'] == 1


@pytest.mark.asyncio
async def test_snapshot_neighbor_city_never_copies_neighbor_address_or_calls_reverse(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = MapOSM()
    snapshot = await place_map(service, story['id'])
    body = command(story, snapshot, 'osm:way:2', 'neighbor')
    await prepare_place_context(service, story['id'], body)
    result = confirm_place(service, story['id'], 'neighbor', body)
    anchor = result['visual_identity']['search_anchor']
    assert anchor['city'] == 'City'
    assert anchor['city_provenance'] == 'osm.snapshot_neighbor.addr_city'
    assert anchor['city_source_candidate_id'] == 'osm:way:1'
    assert not anchor.get('street') and not anchor.get('house_number')


@pytest.mark.asyncio
async def test_reverse_unavailable_cached_and_selection_remains_final(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    calls = []
    def unavailable(request):
        calls.append(request)
        return httpx.Response(503)
    osm = NoCity()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        osm.http = http
        service.providers.osm = osm
        snapshot = await place_map(service, story['id'])
        body = command(story, snapshot, 'osm:way:1', 'unavailable')
        await prepare_place_context(service, story['id'], body)
        await prepare_place_context(service, story['id'], body)
        result = confirm_place(service, story['id'], 'unavailable', body)
    assert len(calls) == 1 and result['visual_identity']['status'] == 'owner_confirmed'
    assert result['visual_identity']['search_anchor']['city'] == ''


@pytest.mark.asyncio
async def test_live_search_and_focus_move_map_without_identity_jobs_or_camera_mutation(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    adapter, session = live(service, story)
    requests = []
    def search(request):
        requests.append(request)
        return httpx.Response(200, json=[{'lat': '55.2', 'lon': '21.2', 'display_name': f'Area {i}'} for i in range(8)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(search)) as http:
        service.providers.osm.http = http
        async def query(description, **kwargs):
            return await MvpLocationStreetStoryService._resolve_place_query(service, description, **kwargs)
        service._resolve_place_query = query
        begin_turn(session, 'Помню район у моря', origin='audio')
        found = await adapter.execute_tool(session, {'name': 'find_place_area', 'id': 'find',
            'args': {'description': 'Помню район у моря'}})
        assert len(found['areas']) == 5
        focused = await adapter.execute_tool(session, {'name': 'focus_owner_map', 'id': 'focus',
            'args': {'area_id': found['areas'][0]['area_id']}})
        focused2 = await adapter.execute_tool(session, {'name': 'focus_owner_map', 'id': 'focus2',
            'args': {'area_id': found['areas'][1]['area_id']}})
        replay = await adapter.execute_tool(session, {'name': 'focus_owner_map', 'id': 'focus',
            'args': {'area_id': found['areas'][0]['area_id']}})
    assert requests[0].url.params['bounded'] == '0' and 'viewbox' in requests[0].url.params
    assert focused['place_map_focus']['lat'] == 55.2
    assert replay['place_map_focus'] == focused2['place_map_focus']
    reopened = service.story(story['id'])
    assert reopened['camera_point'] == story['camera_point'] and reopened['identity_generation'] == 0
    assert reopened['owner_place_hint']['provenance'] == 'owner_voice'
    assert not reopened['owner_place_hint']['independently_verified']
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unknown_clears_selected_work_and_stale_live_cannot_clear_later_house(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = MapOSM()
    snapshot = await place_map(service, story['id'])
    a = confirm_place(service, story['id'], 'a', command(story, snapshot, 'osm:way:1', 'a'))
    adapter, session = live(service, a)
    begin_turn(session, 'Не знаю, где это', origin='audio')
    result = await adapter.execute_tool(session, {'name': 'mark_place_unknown', 'id': 'unknown', 'args': {}})
    assert result['story']['state'] == 'place_unknown'
    unknown = service.story(story['id'])
    assert unknown['state'] == 'place_unknown' and unknown['identity_generation'] == 2
    assert unknown['camera_point'] == story['camera_point'] and unknown['facts'] == []
    with service.store.connection() as db:
        assert not list(db.execute("SELECT id FROM jobs WHERE state IN ('ready','running','retry')"))
    snapshot = await place_map(service, story['id'])
    b = confirm_place(service, story['id'], 'b', command(unknown, snapshot, 'osm:way:2', 'b'))
    begin_turn(session, 'Не знаю, где это', origin='audio')
    with pytest.raises(ConflictError, match='изменился'):
        await adapter.execute_tool(session, {'name': 'mark_place_unknown', 'id': 'late-a', 'args': {}})
    assert service.story(story['id'])['visual_identity']['candidate_id'] == b['visual_identity']['candidate_id']
    assert place_action(service, story['id'], 'unknown', {'expected_photo_sha256': a['photo_sha256'],
        'expected_identity_generation': a['identity_generation'], 'expected_candidate_id': 'osm:way:1'}, unknown=True)['identity_generation'] == 3


@pytest.mark.asyncio
async def test_voice_context_preserves_exact_geometry_generation_draft_and_city(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    a = confirm_place(service, story['id'], 'a', command(story, snapshot, 'osm:way:1', 'a'))
    with service.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='Keep draft' WHERE id=?", (a['id'],))
    adapter, session = live(service, a)
    begin_turn(session, 'Это Дом у моря, адрес Житомирская 22–24.', origin='audio')
    enriched = await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'context',
        'args': {'candidate_id': 'osm:way:1', 'name': 'Дом у моря', 'address': 'Житомирская 22–24'}})
    assert enriched['story']['draft_text'] == 'Keep draft'
    current = service.story(story['id'])
    assert current['identity_generation'] == a['identity_generation'] and current['draft_text'] == 'Keep draft'
    assert current['place_selection']['candidate']['map_geometry'] == a['place_selection']['candidate']['map_geometry']
    assert current['visual_identity']['candidate_name'] == 'Дом у моря'
    assert current['visual_identity']['search_anchor']['city'] == ''
    assert current['visual_identity']['search_anchor']['owner_context']['provenance'] == 'owner_voice'
    begin_turn(session, 'Привет', origin='audio')
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'hallucinated',
            'args': {'candidate_id': 'osm:way:1', 'name': 'Invented place'}})
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_relocated_rough_group_does_not_supply_gps_or_direction_to_identity(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = MapOSM()
    snapshot = await place_map(service, story['id'], latitude=54.7152, longitude=20.5025)
    body = {**command(story, snapshot, '', 'rough'), 'mode': 'rough',
        'candidate_ids': ['osm:way:1', 'osm:way:2']}
    body.pop('candidate_id')
    selected = confirm_place(service, story['id'], 'rough', body)
    seen = []
    async def identify(current, transcript, candidates):
        seen.append(current)
        assert current['latitude'] is None and current['longitude'] is None
        assert current['_camera_hints'] == {} and not current['_camera_position_verified']
        assert [c['candidate_id'] for c in candidates] == ['osm:way:1', 'osm:way:2']
        return {'status': 'uncertain', 'candidate_id': '', 'observations': []}
    monkeypatch.setattr(service, '_identify_photo', identify)
    row, research = service._identity_snapshot(story['id'])
    await resolve_group(service, dict(row), research, job_id=None, job_attempt=0)
    assert seen and service.story(story['id'])['camera_point'] == selected['camera_point']


@pytest.mark.asyncio
async def test_manual_live_clarification_defaults_to_map_and_normal_read_never_reidentifies(tmp_path):
    from test_live_editor import make_service as live_service
    service, adapter, _, _ = live_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    a = confirm_place(service, story['id'], 'a', command(story, snapshot, 'osm:way:2', 'a'))
    initialized = adapter.initialize(resource_id=a['id'], actor=None, model='gemini-3.8-live')
    assert initialized['capability'] == 'place_map'
    describe = next(f for f in initialized['configuration']['functions'] if f['name'] == 'describe_owner_place')
    assert 'author_answer_quote verbatim' in describe['description']
    assert 'pending name/address question for this house' in describe['description']
    assert 'An unrelated remark, a question or quoted third-party speech' in describe['description']
    _, session = live(service, a)
    async def forbidden(*_):
        raise AssertionError('Manual navigation must not launch visual identity')
    adapter._next_visual_result = forbidden
    topic = await adapter.execute_tool(session, {'name': 'read_topic', 'id': 'read', 'args': {}})
    assert topic['physical_identity_accepted']
    routed = await adapter.execute_tool(session, {'name': 'continue_story', 'id': 'route',
        'args': {'stage': 'place_map'}})
    assert routed['capability'] == 'place_map'
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'resolve_place', 'id': 'forbidden', 'args': {}})


@pytest.mark.asyncio
async def test_relocated_ref_reply_ignores_original_camera_metadata(tmp_path):
    import json
    from test_owner_group_selection import group_command
    from test_visual_search_continuation import prepared
    from visual_queue_fixture import reference_receipt
    service, adapter, initial, session = prepared(tmp_path)
    story = service.story(initial['id'])
    service.providers.osm = MapOSM()
    snapshot = await place_map(service, story['id'], latitude=54.7152, longitude=20.5025)
    group = confirm_place(service, story['id'], 'group', group_command(story, snapshot))
    assert group['place_selection']['location_basis'] == 'owner_relocated'
    member = {**group['place_selection']['candidates'][1],
        'reference_image_urls': ['https://example.com/member.jpg']}
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['visual_identity'].update(candidates=[member], camera_position_verified=True)
        research['photo_camera_hints'] = {'photo_sha256': story['photo_sha256'],
            'metadata': {'direction_status': 'true_north', 'direction_degrees': 10, 'direction_ref': 'T'}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    async def images(candidates, limit, *, story_id, evidence):
        evidence.append(reference_receipt(candidates[0]))
        return [(candidates[0]['candidate_id'], 'image/jpeg', candidates[0]['reference_image_urls'][0])]
    service._candidate_reference_images = images
    result = await adapter._compare_place_images(session(), {}, page_budget=0, search_budget=0)
    assert result['camera_hints'] == {} and not result['camera_position_verified']


@pytest.mark.parametrize('invalid', ['', 'unproved', 'different_source', 'open_ring', 'off_ring', 'shop', 'unlinked'])
def test_selected_address_comes_only_from_proved_own_closed_way_entrances(invalid):
    from street_story.place_selection import _candidate, snapshot_member_address
    points = [{'lat': 54.7106, 'lon': 20.5066}, {'lat': 54.7105, 'lon': 20.5063},
              {'lat': 54.7107, 'lon': 20.5061}, {'lat': 54.7106, 'lon': 20.5066}]
    way = _candidate({'type': 'way', 'id': 133035113, 'tags': {'building': 'yes'},
        'geometry': points, 'building_entrance_node_ids': [1876020727, 1876020730]})
    nodes = [_candidate({'type': 'node', 'id': nid, 'lat': points[i]['lat'], 'lon': points[i]['lon'],
        'tags': {'entrance': 'staircase', 'addr:street': 'Житомирская улица', 'addr:housenumber': number}})
        for i, (nid, number) in enumerate([(1876020727, '22'), (1876020730, '24')])]
    receipt = way['map_object']['building_entrances']
    if invalid == 'unproved':
        receipt['proof'] = 'nearby'
    elif invalid == 'different_source':
        receipt['source_url'] = 'https://www.openstreetmap.org/way/999'
    elif invalid == 'open_ring':
        way['map_geometry']['rings'][0]['closed'] = False
    elif invalid == 'off_ring':
        for node in nodes:
            node['map_coordinates']['latitude'] = 0
    elif invalid == 'shop':
        for node in nodes:
            node['map_object']['tags']['shop'] = 'yes'
    elif invalid == 'unlinked':
        receipt['candidate_ids'] = ['osm:node:999']
    geometry = __import__('copy').deepcopy(way['map_geometry'])
    snapshot_member_address({'address_objects': nodes}, way)
    assert way['candidate_id'] == 'osm:way:133035113' and way['map_geometry'] == geometry
    assert way['map_object']['tags'] == {'building': 'yes'}
    if invalid:
        assert not way['search_anchor'].get('address')
        assert way['name_provenance'] == 'map.placeholder'
    else:
        anchor = way['search_anchor']
        assert anchor['street'] == 'Житомирская улица'
        assert anchor['house_numbers'] == ['22', '24'] and anchor['house_number'] == '22, 24'
        assert anchor['address_provenance'] == 'osm_closed_way_node_membership'
        assert [e['candidate_id'] for e in anchor['address_entries']] == receipt['candidate_ids']
        assert way['name'] == 'Житомирская улица 22, 24'


@pytest.mark.asyncio
async def test_nameless_selected_house_waits_for_voice_then_enqueues_once_same_identity(tmp_path):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:2', 'select'))
    assert selected['state'] == 'needs_review'
    assert selected['visual_identity']['status'] == 'owner_confirmed'
    assert selected['identity_clarification']['reason'] == 'owner_selected_context_missing'
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    # Even a recovered identity_ready projection must not silently bypass the hold.
    service.providers.research = SimpleNamespace(facts_available=True)
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='identity_ready' WHERE id=?", (story['id'],))
    service._schedule_confirmed_facts()
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    adapter, session = live(service, selected)
    begin_turn(session, 'Житомирская улица 22', origin='audio')
    args = {'candidate_id': 'osm:way:2', 'address': 'Житомирская улица 22'}
    await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'describe', 'args': args})
    await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'describe', 'args': args})
    current = service.story(story['id'])
    assert current['state'] == 'researching' and current['identity_generation'] == selected['identity_generation']
    assert current['visual_identity']['candidate_id'] == 'osm:way:2'
    assert current['visual_identity']['candidate_name'] == 'Житомирская улица 22'
    assert current['place_selection']['candidate']['map_geometry'] == selected['place_selection']['candidate']['map_geometry']
    assert not current['identity_clarification']
    with service.store.connection() as db:
        jobs = list(db.execute('SELECT kind,payload_json FROM jobs'))
    assert len(jobs) == 1 and jobs[0]['kind'] == 'research'


class WrongAddressTag(NoCity):
    async def _map_nearby(self, *args):
        raw = await super()._map_nearby(*args)
        raw['elements'][0]['tags'].update({'addr:street': 'Университетская улица', 'addr:housenumber': '3'})
        return raw


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['selection_hint', 'voice'])
async def test_owner_address_replaces_a_wrong_map_address_tag_as_the_only_search_address(tmp_path, path):
    # The map tag of the chosen contour names another address than the author's.
    # Research must not search the tag next to the author's address.
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = WrongAddressTag()
    snapshot = await place_map(service, story['id'])
    owner = 'Калининград, улица Профессора Севастьянова, 3'
    body = command(story, snapshot, 'osm:way:1', 'select')
    if path == 'selection_hint':
        confirm_place(service, story['id'], 'select', {**body, 'owner_context': {'address': owner}})
    else:
        selected = confirm_place(service, story['id'], 'select', body)
        assert selected['visual_identity']['search_anchor']['street'] == 'Университетская улица'
        adapter, session = live(service, selected)
        begin_turn(session, 'Это дом с адресом Калининград, улица Профессора Севастьянова, 3', origin='audio')
        await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'describe',
            'args': {'candidate_id': 'osm:way:1', 'address': owner}})
    current = service.story(story['id'])
    with service.store.connection() as db:
        identity = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?',
                                         (story['id'],)).fetchone()[0])['visual_identity']
    anchor = identity['search_anchor']
    assert anchor['address'] == owner and not {'street', 'house_number', 'address_entries'} & set(anchor)
    assert identity['candidate_id'] == 'osm:way:1'
    assert identity['candidate_name'] == current['place_name'] == owner
    chosen = current['place_selection']['candidate']
    assert chosen['replaced_map_address']['street'] == 'Университетская улица'
    assert chosen['map_address']['street'] == 'Университетская улица'  # observation kept
    assert chosen['map_geometry']['rings'][0]['closed']
    selected_object = json.dumps(agent_context(identity, 'goal', 'run', 0)['selected_object'], ensure_ascii=False)
    assert 'Профессора Севастьянова' in selected_object and 'Университетская' not in selected_object


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['does_not_know', 'same_address', 'new_address'])
async def test_owner_answer_after_empty_search_reruns_only_for_new_search_terms(tmp_path, reply):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = WrongAddressTag()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:1', 'select'))
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        generation = int(research['identity_generation'])
        # The state an empty completed search leaves behind.
        research['identity_clarification'] = {'question': 'Какой у него адрес?', 'reason': 'owner_selected_context_missing',
            'photo_sha256': row['photo_sha256'], 'generation': generation, 'scope': 'search_context_only'}
        research['owner_research_waiting_context'] = {'photo_sha256': row['photo_sha256'], 'generation': generation,
            'candidate_id': 'osm:way:1', 'command_id': 'owner-context-run', 'reason': 'owner_research_found_no_sources'}
        db.execute("UPDATE stories SET state='needs_review',research_json=? WHERE id=?", (json.dumps(research), story['id']))
        jobs_before = db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0]
    adapter, session = live(service, selected)
    args = {'candidate_id': 'osm:way:1'}
    if reply == 'does_not_know':
        begin_turn(session, 'Не знаю, какой там адрес', origin='audio')
        args['owner_does_not_know'] = True
        args['author_answer_quote'] = 'Не знаю, какой там адрес'
    else:
        address = 'Университетская улица 3' if reply == 'same_address' else 'Профессора Севастьянова 3'
        begin_turn(session, f'Это {address}', origin='audio')
        args['address'] = address
    result = await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'answer', 'args': args})
    current = service.story(story['id'])
    with service.store.connection() as db:
        jobs = db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0]
        research = json.loads(service._story_row(db, story['id'])['research_json'])
    assert not current['identity_clarification'] and 'owner_research_waiting_context' not in research
    assert current['visual_identity']['candidate_id'] == 'osm:way:1'
    if reply == 'new_address':
        assert jobs == jobs_before + 1 and current['state'] == 'researching'
    else:
        assert jobs == jobs_before and current['state'] == 'facts_ready'
        assert 'Новое исследование не запущено' in result['instruction']
    # Asked once: a further empty search for this house does not ask again.
    from street_story.fact_agents import _ask_owner_for_place_context
    assert not _ask_owner_for_place_context(research, {'photo_sha256': selected['photo_sha256'],
                                                        '_identity_generation': generation}, 'next-run')


@pytest.mark.asyncio
async def test_dont_know_for_a_nameless_selected_house_keeps_it_and_researches_from_the_map(tmp_path):
    # "Не знаю" answers the open name/address question; it never discards the house chosen on the map.
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:2', 'select'))
    assert selected['state'] == 'needs_review' and selected['identity_clarification']
    adapter, session = live(service, selected)
    begin_turn(session, 'Не знаю, как называется этот дом и какой у него адрес', origin='audio')
    with pytest.raises(ConflictError, match='Дом уже выбран'):
        await adapter.execute_tool(session, {'name': 'mark_place_unknown', 'id': 'unknown', 'args': {}})
    result = await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'dont-know',
        'args': {'candidate_id': 'osm:way:2', 'owner_does_not_know': True,
                 'author_answer_quote': 'Не знаю, как называется этот дом и какой у него адрес'}})
    current = service.story(story['id'])
    assert current['state'] == 'researching' and not current['identity_clarification']
    assert current['identity_generation'] == selected['identity_generation']
    assert current['visual_identity']['candidate_id'] == 'osm:way:2'
    assert current['visual_identity']['search_anchor']['owner_context']['owner_does_not_know'] is True
    assert 'исследование идёт по карте' in result['instruction']
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1


@pytest.mark.parametrize('before,after,same', [
    ('улица Мира 3', 'проспект Мира 3', False),
    ('улица Мира 3/5', 'улица Мира 5/3', False),
    ('улица Мира 3-5', 'улица Мира 3/5', False),
    ('улица Мира 3 корпус 2', 'улица Мира 2 корпус 3', False),
    ('  УЛИЦА Мира  3/5 ', 'улица мира 3/5', True),
    ('улица Мира 3', 'улица Мира 3', True),
])
def test_search_values_preserve_address_structure(before, after, same):
    from street_story.place_selection import search_terms
    def chosen(address):
        return {'name': address, 'search_anchor': {'address': address}}
    assert (search_terms(chosen(after)) <= search_terms(chosen(before))) is same


@pytest.mark.asyncio
@pytest.mark.parametrize('street,number,address', [
    ('улица Мира', '3', 'проспект Мира 3'),
    ('улица Мира', '3/5', 'улица Мира 5/3'),
    ('улица Мира', '3-5', 'улица Мира 3/5'),
])
async def test_corrected_address_with_same_words_restarts_research(tmp_path, street, number, address):
    class AddressTag(NoCity):
        async def _map_nearby(self, *args):
            raw = await super()._map_nearby(*args)
            raw['elements'][0]['tags'].update({'addr:street': street, 'addr:housenumber': number})
            return raw
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = AddressTag()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:1', 'select'))
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        from street_story.fact_agents import _ask_owner_for_place_context
        assert _ask_owner_for_place_context(research, {**dict(row), '_identity_generation': selected['identity_generation']}, 'empty')
        db.execute("UPDATE stories SET state='needs_review',research_json=? WHERE id=?", (json.dumps(research), story['id']))
        jobs_before = db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0]
    adapter, session = live(service, selected)
    begin_turn(session, address)
    await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'correct',
        'args': {'candidate_id': 'osm:way:1', 'address': address}})
    current = service.story(story['id'])
    assert current['state'] == 'researching'
    assert current['identity_generation'] == selected['identity_generation']
    assert current['place_selection']['candidate']['map_geometry'] == selected['place_selection']['candidate']['map_geometry']
    assert current['visual_identity']['search_anchor']['address'] == address
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == jobs_before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_quote', 'invented_quote', 'unrelated_question', 'no_question',
                                    'stale_question', 'wrong_waiting_house', 'stale_turn', 'consumed_turn', 'noise'])
async def test_unknown_answer_requires_fresh_quote_and_matching_pending_question(tmp_path, invalid):
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:2', 'select'))
    if invalid in {'unrelated_question', 'no_question', 'stale_question', 'wrong_waiting_house'}:
        with service.store.tx() as db:
            research = json.loads(service._story_row(db, story['id'])['research_json'])
            if invalid == 'unrelated_question':
                research['identity_clarification']['reason'] = 'choose_publication_platform'
            elif invalid == 'no_question':
                research.pop('identity_clarification')
            elif invalid == 'stale_question':
                research['identity_clarification']['generation'] -= 1
            else:
                research['owner_research_waiting_context']['candidate_id'] = 'osm:way:999'
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    adapter, session = live(service, selected)
    answer = 'Не знаю другого названия'
    begin_turn(session, 'Привет' if invalid == 'invented_quote' else answer)
    if invalid == 'stale_turn':
        session.state['author_turn']['at'] -= 31
    elif invalid == 'consumed_turn':
        session.state['author_turn']['consumed'] = True
    elif invalid == 'noise':
        session.state['author_turn']['suspected_noise'] = True
    args = {'candidate_id': 'osm:way:2', 'owner_does_not_know': True}
    if invalid != 'missing_quote':
        args['author_answer_quote'] = answer
    with service.store.connection() as db:
        before = dict(service._story_row(db, story['id']))
        jobs_before = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'invalid', 'args': args})
    with service.store.connection() as db:
        assert dict(service._story_row(db, story['id'])) == before
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == jobs_before
    assert session.state['author_turn']['consumed'] is (invalid == 'consumed_turn')


@pytest.mark.asyncio
async def test_unknown_answer_records_verbatim_evidence_without_language_classifier(tmp_path):
    # Meaning is the model's responsibility; the boundary verifies provenance,
    # not an allowlist of phrases. A truthful quote alone cannot prove intent.
    service, _ = make_service(tmp_path)
    story = manual(service)
    service.providers.osm = NoCity()
    snapshot = await place_map(service, story['id'])
    selected = confirm_place(service, story['id'], 'select', command(story, snapshot, 'osm:way:2', 'select'))
    adapter, session = live(service, selected)
    answer = "I cannot recall this building's address."
    begin_turn(session, answer)
    args = {'candidate_id': 'osm:way:2', 'owner_does_not_know': True, 'author_answer_quote': answer}
    await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'answer', 'args': args})
    await adapter.execute_tool(session, {'name': 'describe_owner_place', 'id': 'answer', 'args': args})
    current = service.story(story['id'])
    context = current['visual_identity']['search_anchor']['owner_context']
    assert context['author_answer_quote'] == answer and context['turn_serial'] == 1
    assert current['identity_generation'] == selected['identity_generation']
    assert current['state'] == 'researching'
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1
