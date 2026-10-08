"""Model-owned next actions, per-source coverage, and old-input preservation."""
import json
from types import SimpleNamespace

import pytest

from street_story.headless_identity import HeadlessIdentity, planned_verdict_schema
from street_story.service import canonical
from test_parallel_identity_pairs import prepare


@pytest.mark.asyncio
async def test_interior_feedback_preserves_building_and_prioritizes_new_external_view_query(tmp_path):
    svc, story, _ = prepare(tmp_path, count=2)
    adapter = HeadlessIdentity(svc)
    session = SimpleNamespace(id='headless:feedback:1', resource_id=story['id'], state={}, model='fixture', closed=False)
    session.visual_reference_limit = 1
    reply = await adapter._compare_place_images(session, {})
    adapter._record_place_comparison(session, 'feedback-result', {
        'comparison_id': reply['comparison_id'], 'status': 'uncertain', 'candidate_id': '',
        'confidence': .2, 'observations': ['REF shows a bedroom; SOURCE shows an external facade.'],
        'alternative_candidate_ids': [],
        'search_feedback': {'reference_kind': 'interior', 'next_action': 'find_external_view',
            'reason': 'An interior cannot resolve the facade.', 'next_query': 'Mapped street external view',
            'candidate_ids': ['gate:b', 'invented:unknown']}})
    _, research = svc._identity_snapshot(story['id'])
    state = research['visual_search_operation']
    assert state['verdict_history'][-1]['status'] == 'uncertain'
    assert state['verdict_history'][-1]['search_feedback']['candidate_ids'] == ['gate:b']
    assert state['planned_queries'][0] == 'Mapped street external view'
    assert research['identity_article_discovery']['planned_queries'] == state['planned_queries']
    assert research['visual_identity']['status'] == 'uncertain'
    assert state['hypotheses']['gate:a']['comparisons'][0]['reference_kind'] == 'interior'
    assert 'gate:b' not in state['hypotheses']  # Alternative was never compared.
    assert state['units_since_planned_query'] >= 2
    assert state['units_since_acquisition'] >= 2


@pytest.mark.asyncio
async def test_unread_source_precedes_more_frames_from_reviewed_nearer_gallery(tmp_path):
    svc, story, _ = prepare(tmp_path, count=2)
    adapter = HeadlessIdentity(svc)
    session = SimpleNamespace(id='headless:coverage:1', resource_id=story['id'], state={}, model='fixture', closed=False)
    session.visual_reference_limit = 1
    first = await adapter._compare_place_images(session, {})
    adapter._record_place_comparison(session, 'negative', {
        'comparison_id': first['comparison_id'], 'status': 'mismatch', 'candidate_id': '',
        'confidence': 1, 'observations': ['A different facade.'], 'alternative_candidate_ids': []})
    # Simulate an acquired gallery tail from the same closer page.
    _, research = svc._identity_snapshot(story['id'])
    state = research['visual_search_operation']
    extra = {**first['references'][0], 'candidate_id': 'gate:a', 'name': 'Gate a',
             'url': 'https://example.com/a', 'reference_image_urls': ['https://example.com/a-other.jpg'],
             'reference_id': 'ref-more-a'}
    state['queue'].insert(0, extra)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
    session.state = {}
    next_reply = await adapter._compare_place_images(session, {}, search_budget=0, page_budget=0)
    assert next_reply['references'][0]['candidate_id'] == 'gate:b'


def test_historical_comparison_preserves_exact_original_verdict_schema():
    old = planned_verdict_schema({'comparison_id': 'original-unknown'})
    new = planned_verdict_schema({'search_feedback_instruction': 'plan'})
    assert 'search_feedback' not in old['properties']
    assert 'search_feedback' in new['properties']
    assert 'search_feedback' in new['required']
    assert json.loads(json.dumps(old)) == old
