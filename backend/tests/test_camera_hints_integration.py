import json
from types import SimpleNamespace

import pytest

from street_story.camera_hints import read_camera_hints
from test_camera_hints import jpeg
from test_identity_lifecycle import make_service
from test_identity_recovery_policy import create_photo


@pytest.mark.asyncio
async def test_real_provider_payload_gets_lens_hints_and_prioritized_references(tmp_path):
    service, _ = make_service(tmp_path)
    photo_bytes = jpeg(direction=0)
    story = create_photo(service, photo_bytes, client='camera-payload')
    with service.store.connection() as db:
        row = dict(db.execute('SELECT * FROM stories WHERE id=?', (story['id'],)).fetchone())
    row['_camera_hints'] = read_camera_hints(photo_bytes)
    seen = {}

    async def references(candidates, limit, *, story_id=None):
        seen['reference_order'] = [x['candidate_id'] for x in candidates]
        return [(x['candidate_id'], 'image/jpeg', photo_bytes) for x in candidates[:limit]]

    async def generate(_key, _timeout, parts, _config):
        text = next(x for x in parts if isinstance(x, str) and 'voice_context' in x)
        seen['context'] = json.loads(text.split('\n', 1)[1])
        return SimpleNamespace(text=json.dumps({'status': 'match', 'candidate_id': 'ahead', 'confidence': .96,
            'observations': ['Matching facade'], 'alternative_candidate_ids': []}))

    class Executor:
        async def execute(self, operation, call):
            assert operation == 'grounded_research'
            return await call('test-not-a-key', 2)

    service._candidate_reference_images = references
    service.providers.gemini = SimpleNamespace(_generate=generate, executor=Executor())
    candidates = [
        {'candidate_id': 'behind', 'distance_m': 100, 'camera_alignment': 'off_axis'},
        {'candidate_id': 'ahead', 'distance_m': 110, 'camera_alignment': 'ahead'},
    ]
    result = await service._identify_photo_batch(row, '', candidates, reference_limit=1)
    assert seen['reference_order'] == ['ahead', 'behind']
    assert [x['candidate_id'] for x in seen['context']['candidates']] == ['behind', 'ahead']
    assert seen['context']['capture_hints']['focal_length_35mm'] == 72
    assert result['_references_sent'] == ['ahead']
    with service.store.connection() as db:
        receipt = json.loads(db.execute("SELECT payload_json FROM live_diagnostics WHERE story_id=? AND event_type='identity_reference_priority'", (story['id'],)).fetchone()[0])
    assert receipt['priority_applied'] is True
    assert receipt['reference_ids_sent'] == ['ahead']


@pytest.mark.asyncio
async def test_explicit_camera_gps_is_required_before_alignment(tmp_path):
    service, gemini = make_service(tmp_path)
    async def osm(*_):
        return {'reverse': {}, 'nearby': [{'type': 'way', 'id': 7, 'center': {'lat': 54.71, 'lon': 20.5025},
                'tags': {'name': 'Other tower'}}]}
    async def wiki(*_):
        return [{'pageid': 77, 'title': 'Test object', 'lat': 54.71, 'lon': 20.5025, 'distance_m': 927,
                 'url': 'https://ru.wikipedia.org/wiki/Test'}]
    service.providers.osm.lookup = osm
    service.providers.wikipedia.nearby = wiki
    content = jpeg(direction=0)
    story = create_photo(service, content, client='alignment-with-origin')
    await service.resolve_identity(story['id'])
    assert any(x.get('camera_alignment') == 'ahead' for x in gemini.identity_calls[0])

    # A same-looking file whose GPS is redacted and a manually geocoded map
    # point may not borrow its camera bearing as if that point were the camera.
    second = create_photo(service, jpeg(direction=0, redacted=True), client='alignment-no-origin')
    with service.store.tx() as db:
        db.execute('UPDATE stories SET latitude=54.7,longitude=20.5025 WHERE id=?', (second['id'],))
    count = len(gemini.identity_calls)
    await service.resolve_identity(second['id'])
    assert all('camera_alignment' not in x for batch in gemini.identity_calls[count:] for x in batch)
