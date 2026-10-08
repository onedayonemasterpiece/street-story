import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from street_story.db import Store
from street_story.identity_map_context import geometry_camera_context
from street_story.identity_subject_binding import bind_reference_subject
from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.providers import OSMClient
from test_identity_subject_binding import article, image_evidence, subject, verdict


def footprint(object_id=900):
    return {'type': 'way', 'id': object_id, 'tags': {'building': 'yes'}, 'geometry': [
        {'lat': 54.7001, 'lon': 20.4999}, {'lat': 54.706, 'lon': 20.4999},
        {'lat': 54.706, 'lon': 20.5001}, {'lat': 54.7001, 'lon': 20.5001},
        {'lat': 54.7001, 'lon': 20.4999}]}


@pytest.mark.asyncio
async def test_late_way_survives_dense_node_order_and_far_center_after_geometry_ranking(tmp_path):
    noise = [{'type': 'node', 'id': i, 'lat': 54.701, 'lon': 20.5,
              'tags': {'name': f'Occupant {i}', 'shop': 'yes'}} for i in range(1, 301)]
    queries = []
    async def handler(request):
        if request.url.path == '/api/0.6/map.json':
            return httpx.Response(503)
        if request.method == 'GET':
            return httpx.Response(200, json={})
        query = parse_qs(request.content.decode())['data'][0]
        queries.append(query)
        return httpx.Response(200, json={'elements': [] if '[historic]' in query else [*noise, footprint()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OSMClient(Store(tmp_path / 'db.sqlite3'), 'fixture', client).lookup(54.7, 20.5)
    building = next(item for item in result['nearby'] if item['id'] == 900)
    assert result['nearby'][0]['id'] == 900
    assert building['distance_m'] < 12
    assert building['representative_distance_m'] > 300
    assert len(building['geometry']) == 5
    assert len(result['observed_pool']) == 301
    assert all('out geom' in query and 'out center' not in query for query in queries)
    active = MvpResearchStreetStoryService._candidate_catalog(result, [])
    observed = MvpResearchStreetStoryService._candidate_catalog(result, [], observed_pool=True)
    assert len(active) == 16 and len(observed) == 301
    assert next(c for c in active if c['candidate_id'] == 'osm:way:900')['map_geometry']['lines'][0] == building['geometry']


def test_bearing_interval_wraps_north_and_malformed_geometry_is_not_invented():
    context = geometry_camera_context(footprint(), 54.7, 20.5)
    interval = context['footprint_bearing_interval']
    assert interval['start_degrees'] > 300 and interval['end_degrees'] < 60
    assert 0 < interval['angular_span_degrees'] < 90
    assert geometry_camera_context({'geometry': [{'lat': 54.7}, {'lat': 54.8, 'lon': 20.5}]}, 54.7, 20.5) == {}


def test_article_can_promote_observed_building_outside_active_set_without_weakening_proof():
    candidate = MvpResearchStreetStoryService._candidate_catalog({'observed_pool': [footprint()]}, [], observed_pool=True)[0]
    active = [subject(f'wiki:{i}') for i in range(1, 17)]
    raw = verdict('web:article', reference_subject_candidate_id=candidate['candidate_id'], source_subject_scope='building')
    bound = bind_reference_subject(raw, [article()], active, [image_evidence()], observed_candidates=[candidate])
    assert bound['status'] == 'bound'
    assert active[-1]['candidate_id'] == 'osm:way:900'
    assert active[-1]['shortlist_bucket'] == 'observed_promotion'
    from street_story.identity_lifecycle import visual_match
    assert visual_match(bound['result'], [article()], active)
    active = [subject()]
    tenant = {**candidate, 'candidate_id': 'osm:node:999', 'map_object': {'provenance': 'osm.tags', 'tags': {'amenity': 'restaurant'}}}
    raw['reference_subject_candidate_id'] = tenant['candidate_id']
    failed = bind_reference_subject(raw, [article()], active, [image_evidence()], observed_candidates=[tenant])
    assert failed['reason'] == 'mapped_occupant_is_not_building_subject'
    assert active == [subject()]
    raw['reference_subject_candidate_id'] = candidate['candidate_id']
    failed = bind_reference_subject(raw, [article()], active, [], observed_candidates=[candidate])
    assert failed['reason'] == 'reference_provenance_missing' and active == [subject()]


@pytest.mark.asyncio
async def test_geometry_and_missing_compass_reach_real_headless_pair_worker(tmp_path):
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path, count=1)
    mapped = {**footprint(), **geometry_camera_context(footprint(), 54.7, 20.5), 'selection_bucket': 'nearby'}
    candidate = MvpResearchStreetStoryService._candidate_catalog({'nearby': [mapped]}, [])[0]
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['visual_identity']['candidates'].append(candidate)
        research['visual_identity'].update(observed_candidates=[candidate],
            camera_hints={'focal_length_35mm': 24, 'diagonal_fov_35mm_deg': 84, 'direction_status': 'missing'})
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    contexts = []
    async def pair(route, snapshot, item, schema, context):
        supplied = json.loads(context)
        contexts.append(supplied)
        mapped = next(c for c in supplied['physical_candidates'] if c['candidate_id'] == candidate['candidate_id'])
        assert mapped['map_geometry'] == candidate['map_geometry']
        assert mapped['boundary_distance_m'] < 12
        assert supplied['camera_hints']['focal_length_35mm'] == 24
        assert 'direction_degrees' not in supplied['camera_hints'] and 'camera_alignment' not in mapped
        assert len(item['_visual_image_parts']) == 2
        return response(item, 'offline-worker', 'match')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google',), visual_pair_route=pair,
        visual_verdict=lambda photo, snapshot, schema, context: pair('google', None, snapshot, schema, context))
    assert await svc.run_once(claim_kind='identity_visual')
    assert len(contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', [None, 0])
async def test_original_metadata_and_complete_observed_pool_survive_identity_persistence(tmp_path, direction):
    from test_camera_hints import jpeg
    from test_identity_lifecycle import make_service
    from test_identity_recovery_policy import create_photo
    svc, _ = make_service(tmp_path)
    story = create_photo(svc, jpeg(direction=direction), client='observed-pool')
    pool = [{**footprint(i), 'center': {'lat': 54.704, 'lon': 20.5025},
             'distance_m': i, 'selection_bucket': 'nearby'} for i in range(1, 41)]
    async def lookup(lat, lon):
        return {'nearby': pool[:20], 'observed_pool': pool}
    svc.providers.osm.lookup = lookup
    await svc.resolve_identity(story['id'])
    _, research = svc._identity_snapshot(story['id'])
    identity = research['visual_identity']
    assert len(identity['candidates']) <= 16
    assert len(identity['observed_candidates']) == 41  # All ways plus the Wikipedia hypothesis.
    assert identity['camera_hints']['focal_length_35mm'] == 72
    last = next(c for c in identity['observed_candidates'] if c['candidate_id'] == 'osm:way:40')
    assert last['map_geometry']['lines'][0] == pool[-1]['geometry']
    if direction is None:
        assert identity['camera_hints']['direction_status'] == 'missing'
        assert 'direction_degrees' not in identity['camera_hints'] and 'camera_alignment' not in last
    else:
        assert identity['camera_hints']['direction_degrees'] == 0
        assert last['camera_alignment'] == 'ahead'


@pytest.mark.asyncio
async def test_real_queue_prioritizes_camera_alignment_within_nearby_distance_band(tmp_path):
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path, count=2)
    with svc.store.tx() as db:
        row = svc._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        candidates = research['visual_identity']['candidates']
        candidates[0]['camera_alignment'] = 'off_axis'
        candidates[1]['camera_alignment'] = 'ahead'
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    sent = []
    async def pair(route, snapshot, item, schema, context):
        cid = item['_visual_reference_mapping'][0]['candidate_id']
        sent.append(cid)
        return response(item, 'offline-' + route, 'match' if cid == 'gate:b' else 'mismatch')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('google', 'opencode'), visual_pair_route=pair)
    assert await svc.run_once(claim_kind='identity_visual')
    assert sent == ['gate:b', 'gate:a']
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
