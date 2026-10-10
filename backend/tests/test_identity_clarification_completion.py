import json

import pytest

from street_story.research_budget import ensure_budget, finish_requested_clarification
from street_story.service import canonical
from test_visual_search_continuation import prepared


@pytest.mark.parametrize('scope_change', [None, 'photo', 'generation', 'control', 'matched', 'answered'])
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
