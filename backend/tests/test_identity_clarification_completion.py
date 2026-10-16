import json

import pytest

from street_story.research_budget import ensure_budget, finish_requested_clarification
from street_story.service import canonical
from test_visual_search_continuation import prepared


@pytest.mark.parametrize('scope_change', [None, 'photo', 'generation', 'control', 'matched', 'answered', 'pending_work'])
def test_only_current_model_question_finishes_wait_without_clock_renewal(tmp_path, scope_change):
    service, _, story, _ = prepared(tmp_path)
    budget = ensure_budget(service, story['id'])
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'] or '{}')
        question = {'photo_sha256': row['photo_sha256'], 'generation': 0, 'control_revision': 0,
            'question': 'В каком городе сделан снимок?', 'reason': 'geographic_context_missing'}
        if scope_change == 'photo':
            question['photo_sha256'] = 'other-photo'
        if scope_change == 'generation':
            question['generation'] = 3
        if scope_change == 'control':
            question['control_revision'] = 3
        if scope_change == 'matched':
            research['visual_identity'] = {'status': 'match'}
        if scope_change == 'answered':
            question['answered'] = True
        if scope_change == 'pending_work':
            question['background_continuation_pending'] = True
        research['identity_clarification'] = question
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        finished = finish_requested_clarification(service, db, story['id'])
        row = service._story_row(db, story['id'])
        saved = json.loads(row['research_json'])
        assert finished is (scope_change is None)
        assert saved['research_budget'] == budget
        if finished:
            assert saved['automatic_research_outcome']['outcome'] == 'clarification_required'
            assert row['error_code'] == 'identity_clarification_required'
            assert row['error_message'] == question['question']
            assert saved['identity_progress']['finished'] is True
            assert not finish_requested_clarification(service, db, story['id'])
        else:
            assert 'automatic_research_outcome' not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize('new_model_question', ['', 'Какая улица указана на вывеске у входа?'])
async def test_missing_location_question_is_available_during_background_comparison(tmp_path, monkeypatch, new_model_question):
    from types import SimpleNamespace
    from street_story import identity_discovery
    from test_identity_lifecycle import make_service, create
    service, _ = make_service(tmp_path)
    story = create(service, client='pending-location', lat=None, lon=None)
    service.providers.research = SimpleNamespace(vision_available=True)
    async def recover(*args, **kwargs):
        return ({'status': 'uncertain', 'candidate_id': '', '_comparison_deferred': True},
                [{'candidate_id': 'osm:way:7', 'candidate_name': 'Unverified body'}])
    monkeypatch.setattr(identity_discovery, 'recover', recover)
    service.ensure_identity(story['id'])
    await service.run_once(claim_kind='identity')
    _, research = service._identity_snapshot(story['id'])
    assert research['identity_clarification']['question']
    assert research['identity_clarification']['background_continuation_pending'] is True
    assert not research.get('automatic_research_outcome')
    source_sha = service.story(story['id'])['photo_sha256']
    service.accept_identity_hint(story['id'], 'actual-owner-reply', 'Калининград, севернее Московского проспекта')
    _, answered = service._identity_snapshot(story['id'])
    assert answered['identity_clarification']['answered'] is True
    assert answered['identity_generation'] == 1
    assert service.story(story['id'])['photo_sha256'] == source_sha
    async def geocode(_text):
        return None
    service._resolve_place_query = geocode
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'])
        research['identity_article_discovery'] = {'search_plan': {'payload': {
            'clarification_question': new_model_question}}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    assert await service.run_once(claim_kind='identity', claim_story_id=story['id'])
    _, continued = service._identity_snapshot(story['id'])
    question = continued['identity_clarification']
    if new_model_question:
        assert question['question'] == new_model_question
        assert question['generation'] == 1 and not question.get('answered')
    else:
        assert question['answered'] is True
        assert question['answered_by'] == 'actual-owner-reply'
    assert continued['identity_owner_hint']['text'] == 'Калининград, севернее Московского проспекта'
    assert not continued.get('automatic_research_outcome')
    assert service.story(story['id'])['photo_sha256'] == source_sha
    assert not service.story(story['id'])['facts']
