"""Expanded hypotheses survive deferred inference into the shared visual queue."""
import hashlib
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.headless_identity import HeadlessIdentity
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


@pytest.mark.asyncio
@pytest.mark.parametrize('prior_status', ['uncertain', 'mismatch'])
async def test_deferred_physical_discovery_reaches_queue_without_confirming_identity(
        tmp_path, monkeypatch, prior_status):
    service, gemini = make_service(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True, vision_model='test-vision')
    photo = jpeg()
    story = service.create_story(key='deferred-discovery', client_story_id='deferred-discovery',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg', photo_bytes=photo,
        voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    discovered = {'candidate_id': 'wiki:9001', 'name': 'Recovered physical gate',
        'url': 'https://ru.wikipedia.org/wiki/Recovered_gate', 'identity_eligible': True,
        'reference_image_urls': ['https://upload.wikimedia.org/recovered-gate.jpg']}
    initial_ids = set()
    observations = []
    if prior_status == 'mismatch':
        observations = ['Initial references show different doorway shapes.']

        async def initial_mismatch(*_args):
            return {'status': 'mismatch', 'candidate_id': 'wiki:77', 'confidence': .86,
                'observations': observations, 'alternative_candidate_ids': [], '_references_sent': []}

        monkeypatch.setattr(service, '_identify_photo', initial_mismatch)

    async def recover(_service, snapshot, transcript, candidates, excluded):
        initial_ids.update(item['candidate_id'] for item in candidates)
        assert discovered['candidate_id'] not in {item['candidate_id'] for item in candidates}
        verdict = await _service._identify_photo_batch(snapshot, transcript, [discovered])
        assert verdict['_comparison_deferred'] is True
        assert verdict['candidate_id'] == ''
        return verdict, [discovered]

    monkeypatch.setattr(identity_discovery, 'recover', recover)
    await service.resolve_identity(story['id'])
    _, research = service._identity_snapshot(story['id'])
    identity = research['visual_identity']
    assert identity['status'] == 'uncertain'
    assert identity['candidate_id'] is None
    assert identity['visual_reference_verified'] is False
    assert identity['observations'] == observations
    assert identity['confidence'] == (.86 if prior_status == 'mismatch' else 0)
    assert initial_ids
    assert {item['candidate_id'] for item in identity['candidates']} == initial_ids | {'wiki:9001'}
    assert 'poi_id' not in research
    assert gemini.identity_calls == []
    assert gemini.research_calls == 0
    assert service.story(story['id'])['facts'] == []

    async def images(candidates, limit, *, story_id, evidence):
        candidate = candidates[0]
        if candidate['candidate_id'] != discovered['candidate_id']:
            return []
        evidence.append({'candidate_id': candidate['candidate_id'],
            'source_url': candidate['reference_image_urls'][0],
            'model_image_sha256': hashlib.sha256(photo).hexdigest()})
        return [(candidate['candidate_id'], 'image/jpeg', photo)]

    monkeypatch.setattr(service, '_candidate_reference_images', images)
    worker = HeadlessIdentity(service)
    session = SimpleNamespace(id='deferred-discovery-session', resource_id=story['id'],
        model='test-vision', state={})
    await worker._compare_place_images(session, {})
    pending = session.state['visual_comparison']['pending']
    assert pending['candidates'][0]['candidate_id'] == discovered['candidate_id']
    assert discovered['candidate_id'] in {
        item['candidate_id'] for item in pending['reply']['physical_candidates']}
    assert service.story(story['id'])['visual_identity']['status'] == 'uncertain'

    # A controlled verdict still has to pass the common pixel-reference gate.
    accepted = worker._record_place_comparison(session, 'deferred-discovery-verdict', {
        'comparison_id': pending['id'], 'status': 'match', 'candidate_id': discovered['candidate_id'],
        'confidence': .98, 'observations': ['Distinct arch and doorway correspondence.'],
        'alternative_candidate_ids': []})
    assert accepted['matched'] is True
    assert service.story(story['id'])['visual_identity']['candidate_id'] == discovered['candidate_id']
    assert service.story(story['id'])['state'] == 'identity_ready'
