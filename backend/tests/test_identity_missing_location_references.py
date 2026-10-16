import json
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.research_budget import ensure_budget
from street_story.service import canonical
from test_identity_recovery_policy import create_photo, photo
from test_identity_lifecycle import make_service


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [None, 'article_media', 'comparison'])
@pytest.mark.parametrize('model_question', [False, True])
@pytest.mark.parametrize('vision_available', [False, True])
async def test_missing_location_preserves_ready_reference_work_in_original_wave(
        tmp_path, monkeypatch, pending, model_question, vision_available):
    service, _ = make_service(tmp_path)
    topic = create_photo(service, photo(gps=False))
    service.providers.research = SimpleNamespace(vision_available=vision_available)
    budget = ensure_budget(service, topic['id'])
    candidate = {'candidate_id': 'web:received-page', 'name': 'Unconfirmed church',
        'url': 'https://example.com/church',
        'reference_image_urls': ['https://example.com/church.jpg']}

    async def recover(svc, snapshot, *_):
        assert snapshot['latitude'] is None and snapshot['longitude'] is None
        if model_question:
            with svc.store.tx() as db:
                row = svc._story_row(db, topic['id'])
                research = json.loads(row['research_json'])
                research['identity_article_discovery'] = {'search_plan': {'payload': {
                    'clarification_question': 'На какой улице снято здание?'}}}
                db.execute('UPDATE stories SET research_json=? WHERE id=?',
                           (canonical(research), topic['id']))
        if pending is None:
            return None
        return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
            'observations': ['Received photographs still need comparison.'],
            '_article_media_pending' if pending == 'article_media' else '_comparison_deferred': True,
            '_references_sent': []}, [candidate]

    monkeypatch.setattr(identity_discovery, 'recover', recover)
    service.ensure_identity(topic['id'])
    assert await service.run_once(claim_kind='identity', claim_story_id=topic['id'])
    service._schedule_identity_visual()
    _, research = service._identity_snapshot(topic['id'])
    assert research['research_budget'] == budget
    assert research['visual_identity']['status'] == 'uncertain'
    assert not research['visual_identity']['identity_verified']
    assert not service.story(topic['id'])['facts']
    question = research['identity_clarification']
    assert question['photo_sha256'] == topic['photo_sha256']
    assert question['generation'] == 0
    if model_question:
        assert question['question'] == 'На какой улице снято здание?'
    else:
        assert question['question']
    if pending and vision_available:
        assert question['background_continuation_pending'] is True
        assert 'automatic_research_outcome' not in research
        with service.store.connection() as db:
            visual = db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity_visual'",
                                (topic['id'],)).fetchone()
        assert visual and visual['state'] == 'ready'
        assert json.loads(visual['payload_json'])['identity_generation'] == 0
    else:
        assert question['background_continuation_pending'] is False
        assert research['automatic_research_outcome']['outcome'] == 'clarification_required'
