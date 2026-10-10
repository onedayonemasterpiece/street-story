import copy
import json
from types import SimpleNamespace

import pytest

from street_story.identity_proof import geometry_result_valid, verified_physical_identity
from street_story.research_control import stop_research
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload
from test_identity_scene import render_scene


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_during_call', [False, True])
async def test_joint_geometry_commits_normal_identity_and_poi_without_wikipedia_or_ref(tmp_path, stop_during_call):
    service, snapshot, active = geometry_setup(tmp_path)
    osm = snapshot['_identity_map_snapshot']
    calls = []
    async def lookup(*args):
        return osm
    async def nearby(*args):
        return []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert len(contents) == 3 and contents[0].inline_data and contents[1].inline_data
        if stop_during_call:
            stop_research(service, snapshot['id'], purpose='identity')
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def forbidden(*args, **kwargs):
        pytest.fail('Accepted spatial decision must not call any reference comparison')
    service.providers.osm.lookup = lookup
    service.providers.wikipedia.nearby = nearby
    service.providers.gemini = SimpleNamespace(_generate=generate, executor=Executor())
    service._identify_photo = service._identify_photo_batch = forbidden
    service.ensure_identity(snapshot['id'])
    await service.run_once(claim_kind='identity')
    row, research = service._identity_snapshot(snapshot['id'])
    identity = research['visual_identity']
    assert len(calls) == 1
    if stop_during_call:
        assert identity['status'] != 'match' and not research.get('poi_id')
        return
    assert row['state'] == 'identity_ready'
    assert identity['candidate_id'] == 'osm:way:2' and identity['status'] == 'match'
    assert identity['proof_kind'] == 'geometry' and identity['visual_reference_verified'] is False
    assert identity['reference_evidence'] == [] and identity['confidence'] is None
    assert verified_physical_identity(identity, photo_sha256=row['photo_sha256'], generation=0)
    assert research['poi_id']
    original_proof = copy.deepcopy(identity['geometry_proof'])
    assert verified_physical_identity(identity, photo_sha256=row['photo_sha256'], generation=0, control_revision=5)
    assert identity['geometry_proof'] == original_proof  # accepted evidence is not relabelled after Stop/resume
    with service.store.connection() as db:
        assert db.execute("SELECT poi_id FROM poi_aliases WHERE value='osm:way:2'").fetchone()[0] == research['poi_id']
        assert not db.execute("SELECT 1 FROM poi_aliases WHERE value='osm:way:3'").fetchone()
    service.providers.research = SimpleNamespace(facts_available=True)
    service._schedule_confirmed_facts()
    with service.store.connection() as db:
        assert db.execute("SELECT 1 FROM jobs WHERE story_id=? AND kind='research' AND state='ready'", (snapshot['id'],)).fetchone()


def test_geometry_receipt_rejects_modified_current_map_scope_and_unobserved_features(tmp_path):
    from street_story.identity_proof import freeze_geometry_proof
    service, story, active = geometry_setup(tmp_path)
    scene = render_scene(story, active)
    receipt = {'joint_image_input': True, 'source_photo_sha256': story['photo_sha256'],
        'original_source_sha256': story['photo_sha256'], 'model_source_sha256': story['photo_sha256'],
        'map_image_sha256': scene['manifest']['image_sha256'], 'manifest': scene['manifest']}
    proof = freeze_geometry_proof(story, geometry_decision(), receipt, active)
    assert proof
    raw = {'status': 'match', 'candidate_id': 'osm:way:2', 'proof_kind': 'geometry', 'geometry_proof': proof}
    assert geometry_result_valid(raw, active, story)
    changed = copy.deepcopy(story)
    changed['_identity_map_snapshot']['observed_pool'][0]['geometry'][1]['lon'] += .0001
    assert not geometry_result_valid(raw, active, changed)
    for key, value in (('photo_sha256', 'f'*64), ('_identity_generation', 1), ('_identity_research_control_revision', 1)):
        assert not geometry_result_valid(raw, active, {**story, key: value})
    for feature in ({'candidate_id': 'osm:way:404', 'kind': 'contour'},
                    {'candidate_id': 'osm:way:2', 'kind': 'attribute', 'key': 'tunnel'}):
        decision = geometry_decision()
        decision['decisive_relations'][0]['map_features'] = [feature]
        assert freeze_geometry_proof(story, decision, receipt, active) is None
    decision = geometry_decision()
    decision['candidate_id'] = 'osm:way:9'  # road context is not an eligible physical identity
    assert freeze_geometry_proof(story, decision, receipt, active) is None


def test_owner_approx_camera_upload_is_distinct_from_device_or_exif_coordinates(tmp_path):
    from test_identity_lifecycle import make_service
    from test_reference_image_codec import jpeg
    from street_story.identity_scene import scene_camera_context
    from street_story.service import ConflictError
    import hashlib
    service, _ = make_service(tmp_path)
    photo = jpeg()
    args = {'key': 'approx', 'client_story_id': 'approx', 'photo_sha256': hashlib.sha256(photo).hexdigest(),
        'photo_mime_type': 'image/jpeg', 'photo_bytes': photo, 'voice_protocol': 'voice-chunks-v2',
        'lat': 55.074106, 'lon': 21.903648}
    created = service.create_story(**args, location_provenance={'kind': 'owner_approx_camera'})
    row, research = service._identity_snapshot(created['id'])
    camera = scene_camera_context(row)
    assert camera['position_status'] == 'owner_approximate' and camera['position_verified'] is False
    assert camera['accuracy_m'] is None and camera['heading_status'] == 'missing'
    assert research['location_provenance']['source'] == 'owner_supplied'
    with pytest.raises(ConflictError, match='Approximate'):
        service.create_story(**{**args, 'key': 'wrong', 'client_story_id': 'wrong'},
            location_provenance={'kind': 'device_current_location'})
