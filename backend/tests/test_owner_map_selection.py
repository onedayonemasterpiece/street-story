import json

import httpx
import pytest

from street_story.app import create_app
from street_story.config import reveal
from street_story.identity_proof import accepted_identity
from street_story.place_selection import confirm_place, place_map
from street_story.research_control import stop_research
from street_story.service import ConflictError
from test_identity_lifecycle import create, make_service


class MapOSM:
    async def _map_nearby(self, client, lat, lon, radius):
        return {'elements': [
            {'type': 'way', 'id': i, 'tags': {'building': 'yes', 'name': f'House {i}',
             **({'addr:street': 'Street', 'addr:housenumber': str(i), 'addr:city': 'City'} if i == 1 else {})},
             'geometry': [{'lat': lat, 'lon': lon}, {'lat': lat+.0001, 'lon': lon},
                          {'lat': lat+.0001, 'lon': lon+.0001}, {'lat': lat, 'lon': lon}]}
            for i in (1, 2)]}


async def map_for(service, story):
    service.providers.osm = MapOSM()
    return await place_map(service, story['id'])


def command(story, snapshot, cid, key):
    return {'command_id': key, 'mode': 'exact', 'snapshot_id': snapshot['snapshot_id'], 'candidate_id': cid,
            'expected_photo_sha256': story['photo_sha256'], 'expected_identity_generation': story['identity_generation'],
            'expected_candidate_id': (story.get('visual_identity') or {}).get('candidate_id')}


@pytest.mark.asyncio
async def test_clean_owner_basis_enqueues_once_without_address_and_replay_returns_current(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service)
    with service.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (json.dumps({'visual_identity': {'status': 'uncertain', 'proof_kind': 'geometry'}}), story['id']))
    snapshot = await map_for(service, story)
    body = command(story, snapshot, 'osm:way:2', 'choose-a')
    result = confirm_place(service, story['id'], 'choose-a', body)
    identity = result['visual_identity']
    assert accepted_identity(identity, story['photo_sha256'], 1)
    assert 'proof_kind' not in identity
    assert identity['selection_basis'] == 'owner_exact'
    assert identity['candidate_id'] == 'osm:way:2'
    assert result['camera_point']['origin'] == 'exif'
    assert result['place_selection']['candidate']['map_geometry']['rings'][0]['closed']
    replay = confirm_place(service, story['id'], 'choose-a', body)
    assert replay['revision'] == result['revision']
    with service.store.connection() as db:
        jobs = list(db.execute("SELECT * FROM jobs WHERE story_id=?", (story['id'],)))
    assert len(jobs) == 1 and jobs[0]['kind'] == 'research'
    assert not gemini.identity_calls
    assert service.story(story['id'])['place_selection'] == result['place_selection']


@pytest.mark.asyncio
async def test_switch_clears_a_projection_fences_job_preserves_memory_and_stop_does_not_block_b(tmp_path):
    service, _ = make_service(tmp_path)
    a = create(service)
    snapshot = await map_for(service, a)
    body_a = command(a, snapshot, 'osm:way:1', 'a')
    a = confirm_place(service, a['id'], 'a', body_a)
    with service.store.tx() as db:
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?, 'fact_a', 'A fact', .9,1,1,'[]')", (a['id'],))
        db.execute("UPDATE stories SET draft_text='A draft',processed_image_url='A image',vibepublish_asset_ref='A asset' WHERE id=?", (a['id'],))
        memory_count = db.execute('SELECT count(*) FROM pois').fetchone()[0]
    stopped = stop_research(service, a['id'], purpose='facts')['story']
    snapshot_b = await map_for(service, stopped)
    b = confirm_place(service, a['id'], 'b', command(stopped, snapshot_b, 'osm:way:2', 'b'))
    assert b['identity_generation'] == 2 and b['facts'] == []
    assert b['draft_text'] is None and b['processed_image_url'] is None
    assert not b['research_controls']['facts']['stopped']
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM pois').fetchone()[0] >= memory_count
        jobs = list(db.execute('SELECT kind,state,payload_json FROM jobs WHERE story_id=? ORDER BY created_at', (a['id'],)))
    assert jobs[0]['state'] == 'cancelled' and jobs[-1]['state'] == 'ready'
    assert json.loads(jobs[-1]['payload_json'])['identity_generation'] == 2
    assert confirm_place(service, a['id'], 'a', body_a)['identity_generation'] == 2
    with pytest.raises(ConflictError, match='изменился'):
        confirm_place(service, a['id'], 'stale-new-command', {**body_a, 'command_id': 'stale-new-command'})


@pytest.mark.asyncio
async def test_snapshot_selection_rejects_unshown_ids_and_pan_is_bounded(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    with pytest.raises(ConflictError, match='показанный'):
        confirm_place(service, story['id'], 'bad', command(story, snapshot, 'osm:way:3', 'bad'))
    with pytest.raises(ConflictError):
        await place_map(service, story['id'], radius_m=5000)
    again = await place_map(service, story['id'])
    assert again['snapshot_id'] == snapshot['snapshot_id']
    for i in range(4):
        await place_map(service, story['id'], latitude=54.7+i*.0001, longitude=20.5)
    with service.store.connection() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
    assert len(research['place_map_snapshots']) == 3


@pytest.mark.asyncio
async def test_manual_http_intake_retains_exif_and_house_point_fallback(tmp_path):
    service, gemini = make_service(tmp_path)
    app = create_app(service.settings, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        client.headers['Authorization'] = 'Bearer ' + reveal(service.settings.device_token)
        response = await client.post('/v1/stories', headers={'Idempotency-Key': 'intake'},
            data={'client_story_id': 'manual', 'photo_sha256': 'token', 'voice_protocol': 'voice-chunks-v2', 'identity_mode': 'manual'},
            files={'photo': ('photo.jpg', __import__('test_identity_recovery_policy').photo(), 'image/jpeg')})
        assert response.status_code == 200, response.text
        story = response.json()
        assert story['identity_mode'] == 'manual' and story['state'] == 'photo_ready'
        body = {'command_id': 'manual-place', 'mode': 'exact', 'expected_photo_sha256': 'token',
            'expected_identity_generation': 0, 'expected_candidate_id': None,
            'fallback': {'latitude': 54.7, 'longitude': 20.5, 'address': 'Street 5', 'city': 'City'}}
        response = await client.post(f"/v1/stories/{story['id']}/place", json=body)
        assert response.status_code == 200, response.text
        assert response.json()['camera_point']['origin'] == 'exif'
        assert response.json()['visual_identity']['search_anchor']['address'] == 'Street 5'
        assert response.json()['identity_mode'] == 'manual'
    with service.store.connection() as db:
        assert not list(db.execute("SELECT id FROM jobs WHERE kind='identity'"))
    assert not gemini.identity_calls


@pytest.mark.asyncio
async def test_legacy_live_confirmation_replaces_geometry_proof_and_queues_one_job(tmp_path):
    from test_live_editor import make_service as live_service, mark_identity_ready
    service, adapter, session, _ = live_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    with service.store.tx() as db:
        row = service._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research['visual_identity']['proof_kind'] = 'geometry'
        research['visual_identity']['geometry_proof'] = {'validated': False}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    with pytest.raises(ConflictError, match='явное подтверждение'):
        await adapter._confirm_place(session, 'without-consent', {'candidate_id': 'wiki:77'})
    adapter.on_event(session, {'type': 'input_transcript', 'text': 'Это Бранденбургские ворота в Калининграде.'})
    result = await adapter.execute_tool(session, {'name': 'confirm_place', 'id': 'legacy-confirm',
        'args': {'candidate_id': 'wiki:77', 'candidate_name': 'Бранденбургские ворота'}})
    identity = service.story(session.resource_id)['visual_identity']
    assert result['visual_identity']['generation'] == 1
    assert identity['generation'] == 1 and identity['selection_basis'] == 'owner_live'
    assert 'proof_kind' not in identity and 'geometry_proof' not in identity
    assert accepted_identity(identity, generation=1)
    assert session.state['research_control_scope']['expected_identity_generation'] == 1
    next_tool = await adapter.execute_tool(session, {'name': 'get_facts', 'id': 'next-normal-tool', 'args': {}})
    assert 'facts' in next_tool
    from street_story.live_author_intent import begin_turn
    begin_turn(session, 'Останови исследование фактов.', origin='text')
    stopped = adapter._control_research(session, 'stop-b', {'research_action': 'stop', 'research_purpose': 'facts'})
    assert stopped['story']['identity_generation'] == 1
    assert stopped['story']['research_controls']['facts']['stopped']
    await adapter.execute_tool(session, {'name': 'confirm_place', 'id': 'legacy-confirm',
        'args': {'candidate_id': 'wiki:77', 'candidate_name': 'Бранденбургские ворота'}})
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_named_point_fallback_keeps_owner_hint_and_map_limit_is_explicit(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service, lat=None, lon=None)
    result = confirm_place(service, story['id'], 'named', {'mode': 'exact', 'expected_photo_sha256': story['photo_sha256'],
        'expected_identity_generation': 0, 'expected_candidate_id': None,
        'fallback': {'latitude': 54.7, 'longitude': 20.5, 'name': 'Named Gate', 'city': 'City'}})
    assert result['visual_identity']['candidate_name'] == 'Named Gate'
    assert result['visual_identity']['search_anchor']['names'] == ['Named Gate']
    service2, _ = make_service(tmp_path / 'many')
    story2 = create(service2)
    class ManyBuildings(MapOSM):
        async def _map_nearby(self, *args):
            raw = await super()._map_nearby(*args)
            return {'elements': [{**raw['elements'][0], 'id': i} for i in range(1, 252)]}
    service2.providers.osm = ManyBuildings()
    snapshot = await place_map(service2, story2['id'])
    assert snapshot['partial'] is True and snapshot['available_building_count'] == 251
    assert len(snapshot['buildings']['features']) == 250


@pytest.mark.asyncio
async def test_saved_owner_draft_survives_reopen_but_stale_generation_cannot_write(tmp_path):
    from street_story.fact_ledger import accept_fact_basis, persist_fact_candidates, refresh_review_status, set_owner_selection
    from street_story.place_selection import save_draft
    from test_fact_ledger import fact
    service, _ = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    selected = confirm_place(service, story['id'], 'draft-place', command(story, snapshot, 'osm:way:1', 'draft-place'))
    with service.store.tx() as db:
        fid = persist_fact_candidates(db, story_id=story['id'], poi_key='osm:way:1',
            facts=[fact('figures', 'Three figures.', 'https://own.example', 'Three figures.')],
            run_id='r', batch_id='b', model_name='test', prompt_version='test', now=service.store.now())[0]
        evidence = [row[0] for row in db.execute('SELECT evidence_id FROM fact_evidence_spans')]
        accept_fact_basis(db, story['id'], fid, evidence, operation={'contract': 'source-reading-assessment-v2'}, now=service.store.now())
        refresh_review_status(db, story['id'], service.store.now())
        set_owner_selection(db, story['id'], [fid], service.store.now())
    body = {'text': 'Three figures.', 'selected_fact_ids': [fid], 'expected_identity_generation': 1}
    saved = save_draft(service, story['id'], 'save-draft', body)
    assert saved['draft_text'] == 'Three figures.'
    assert save_draft(service, story['id'], 'save-draft', body)['revision'] == saved['revision']
    assert service.story(story['id'])['draft_text'] == 'Three figures.'
    with pytest.raises(ConflictError, match='изменился'):
        save_draft(service, story['id'], 'stale-draft', {**body, 'expected_identity_generation': 0})
    with service.store.connection() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
    assert research['draft_composed_by'] == 'owner_editor' and research['draft_fact_revisions']
    assert selected['identity_generation'] == 1


@pytest.mark.asyncio
async def test_map_adapter_reuses_overpass_when_map_route_is_unavailable_and_recovers_relation(tmp_path):
    from street_story.providers import RetryableProviderError
    service, _ = make_service(tmp_path)
    story = create(service)
    class RecoverOSM(MapOSM):
        def __init__(self):
            self.queries = []
            self.fail_map = True
        async def _map_nearby(self, *args):
            if self.fail_map:
                raise RetryableProviderError('map unavailable')
            return {'elements': [], 'incomplete_building_ids': ['osm:relation:9']}
        async def _overpass_query(self, client, query, bucket, preferred, paused):
            self.queries.append((query, bucket))
            raw = await super()._map_nearby(client, 54.7, 20.5, 350)
            if bucket == 'owner_map_incomplete':
                outer = raw['elements'][0]['geometry']
                return {'elements': [{'type': 'relation', 'id': 9, 'tags': {'building': 'yes', 'type': 'multipolygon'},
                    'members': [{'type': 'way', 'ref': 99, 'role': 'outer', 'geometry': outer}]}]}, 'route'
            return raw, 'route'
    service.providers.osm = RecoverOSM()
    first = await place_map(service, story['id'])
    assert first['map_source'] == 'overpass.bounded_geometry' and len(first['buildings']['features']) == 2
    service.providers.osm.fail_map = False
    second = await place_map(service, story['id'], longitude=20.501, latitude=54.7)
    assert second['buildings']['features'][0]['properties']['candidate_id'] == 'osm:relation:9'
    assert not second['partial']
    assert [q[1] for q in service.providers.osm.queries] == ['owner_map', 'owner_map_incomplete']


@pytest.mark.asyncio
@pytest.mark.parametrize('observed_name,with_address', [(None, False), (None, True), ('Observed OSM House', True)])
async def test_explicit_owner_name_replaces_only_unnamed_map_label(tmp_path, observed_name, with_address):
    service, _ = make_service(tmp_path)
    story = create(service)
    class UnnamedOSM(MapOSM):
        async def _map_nearby(self, *args):
            raw = await super()._map_nearby(*args)
            item = raw['elements'][0]
            item['tags'] = {'building': 'yes'}
            if observed_name:
                item['tags']['name'] = observed_name
            if with_address:
                item['tags'].update({'addr:street': 'Житомирская', 'addr:housenumber': '22–24'})
            return {'elements': [item]}
    service.providers.osm = UnnamedOSM()
    snapshot = await place_map(service, story['id'])
    owner_name = 'Калининград, Житомирская, 22–24'
    body = {**command(story, snapshot, 'osm:way:1', 'named-contour'),
            'owner_context': {'name': owner_name, 'city': 'Калининград'}}
    result = confirm_place(service, story['id'], 'named-contour', body)
    identity = result['visual_identity']
    expected = observed_name or owner_name
    assert identity['candidate_name'] == result['place_name'] == expected
    assert identity['candidates'][0]['name'] == result['place_selection']['candidate']['name'] == expected
    assert identity['candidate_name_provenance'] == ('osm.tags.name' if observed_name else 'owner.explicit_selection_hint')
    assert identity['candidate_id'] == 'osm:way:1'
    assert owner_name in identity['search_anchor']['names']
    assert identity['search_anchor']['owner_context_provenance'] == 'owner.explicit_selection_hint'
    assert (identity['candidates'][0]['map_object']['tags'].get('name') or None) == observed_name
    assert result['place_selection']['candidate']['map_geometry']['rings'][0]['closed']
    assert service.story(story['id'])['visual_identity']['candidate_name'] == expected


@pytest.mark.asyncio
async def test_contour_without_city_accepts_same_object_and_explicit_context_enriches_its_anchor(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    class StreetWithoutCity(MapOSM):
        async def _map_nearby(self, *args):
            raw = await super()._map_nearby(*args)
            tags = raw['elements'][0]['tags']
            tags.pop('addr:city')
            tags.pop('name')
            return {'elements': raw['elements'][:1]}
    service.providers.osm = StreetWithoutCity()
    snapshot = await place_map(service, story['id'])
    feature = snapshot['buildings']['features'][0]
    assert feature['properties']['search_anchor']['city'] == ''
    assert feature['properties']['tags']['addr:street'] == 'Street'
    first = confirm_place(service, story['id'], 'without-owner-hint', command(story, snapshot, 'osm:way:1', 'without-owner-hint'))
    assert first['visual_identity']['candidate_id'] == 'osm:way:1'
    assert first['visual_identity']['search_anchor']['city'] == ''
    assert first['visual_identity']['status'] == 'owner_confirmed'
    snapshot2 = await place_map(service, story['id'])
    explicit = {**command(first, snapshot2, 'osm:way:1', 'with-owner-hint'),
        'owner_context': {'city': 'Owner City', 'address': 'Owner City, Street 1', 'name': 'Owner City, Street 1'}}
    enriched = confirm_place(service, story['id'], 'with-owner-hint', explicit)
    chosen = enriched['visual_identity']['candidates'][0]
    assert enriched['visual_identity']['candidate_id'] == 'osm:way:1'
    assert enriched['visual_identity']['candidate_name'] == 'Owner City, Street 1'
    assert chosen['search_anchor']['city'] == ''
    assert chosen['search_anchor']['owner_context']['city'] == 'Owner City'
    assert chosen['search_anchor']['owner_context_provenance'] == 'owner.explicit_selection_hint'
    assert chosen['map_address'] == first['visual_identity']['candidates'][0]['map_address']
    assert chosen['map_address']['provenance'] == 'osm.tags'
    assert chosen['map_geometry'] == first['visual_identity']['candidates'][0]['map_geometry']
