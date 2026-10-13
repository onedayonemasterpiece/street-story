"""Photo GPS is a hint; owner navigation and selected geometry remain authoritative."""
from types import SimpleNamespace

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
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
    assert 'describe_owner_place' in {f['name'] for f in initialized['configuration']['functions']}
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
