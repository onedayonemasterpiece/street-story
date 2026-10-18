"""Model comparison operations are owned by story/generation/ref IDs, never pixels."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from direct_visual_fixture import visual_args
from street_story.research_adapter import ProductResearchAdapter
from street_story.research_control import stop_research
from street_story.service import ConflictError, canonical
from test_research_control import fixture


def setup(tmp_path):
    service, sid, _photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    primary = AsyncMock(return_value={'result': {'status': 'uncertain'}, 'receipt': {'provider': 'google'}})
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=primary)
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=AsyncMock(side_effect=AssertionError('primary is qualified')))
    _, story, _, context = visual_args(b'original-source', {'id': sid}, {},
        {'comparison_id': 'operation-1', 'references': [{'candidate_id': 'wiki:1'}]})
    return adapter, service, story, context


@pytest.mark.asyncio
async def test_same_operation_reads_its_closed_result_and_new_operation_calls_model(tmp_path):
    adapter, service, story, context = setup(tmp_path)
    for _ in range(2):
        result = await adapter.visual_verdict(None, story, {}, context)
    assert adapter.primary_vision.compare_visual.await_count == 1
    assert result['receipt']['provider_send_state'] == 'response_closed'
    await adapter.visual_verdict(None, story, {}, {**context, 'comparison_id': 'operation-2'})
    assert adapter.primary_vision.compare_visual.await_count == 2
    adapter.native_vision.compare_visual.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_reference_id_is_a_new_model_operation(tmp_path):
    adapter, service, story, context = setup(tmp_path)
    await adapter.visual_verdict(None, story, {}, context)
    ref = {**context['references'][0], 'reference_id': 'another-reference'}
    story = {**story, '_visual_reference_mapping': [ref]}
    await adapter.visual_verdict(None, story, {}, {**context, 'references': [ref]})
    assert adapter.primary_vision.compare_visual.await_count == 2


@pytest.mark.asyncio
async def test_prior_story_completed_verdict_cannot_satisfy_current_story_comparison(tmp_path):
    adapter, service, story, context = setup(tmp_path)
    # A prior retained provider receipt with identical source bytes has no
    # authority over the new operation; no image hash lookup exists.
    with service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   ('old', 'another-story-operation', story['id'], 'vision_native',
                    canonical({'phase': 'completed', 'result': {'status': 'match'}, 'binding': {'story_id': 'other-story', 'generation': 0}}), 1, 1))
    result = await adapter.visual_verdict(None, story, {}, context)
    assert result['result']['status'] == 'uncertain'
    adapter.primary_vision.compare_visual.assert_awaited_once()
    assert not hasattr(adapter, 'reuse_native_verdict')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['stopped', 'stale_generation', 'missing_worker'])
async def test_scope_fences_before_any_provider_send(tmp_path, fault):
    adapter, service, story, context = setup(tmp_path)
    if fault == 'stopped':
        stop_research(service, story['id'], purpose='identity')
    elif fault == 'stale_generation':
        story['_identity_generation'] = 1
    else:
        story.update(_research_job_id='missing', _research_job_attempt=1)
    with pytest.raises(ConflictError):
        await adapter.visual_verdict(None, story, {}, context)
    adapter.primary_vision.compare_visual.assert_not_awaited()
    adapter.native_vision.compare_visual.assert_not_awaited()
