"""A targeted reader policy must never replace an already submitted operation."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.research_adapter import ProductResearchAdapter
from test_verified_fact_pool import FactClient, page, setup


def prepared(tmp_path, *, owner=True):
    adapter, extra, story, entries = setup(tmp_path)
    live = FactClient('gemini-3.8-live', adapter)
    live.provider_id, live.endpoint, live.directory = 'google-live', 'live-interaction:street-story', None
    adapter.live_facts = live
    # Simulate the fast route that otherwise wins new-unit assignment.
    adapter.order_fact_routes = lambda routes, ordinal=0, **kwargs: sorted(routes, key=lambda r: r['role'] != 'facts_live')
    if owner:
        story['research_json'] = json.dumps({'visual_identity': {'status': 'owner_confirmed', 'selection_basis': 'owner_exact'}})
    return adapter, extra, live, story, entries


@pytest.mark.asyncio
async def test_new_owner_exact_units_use_available_qualified_pool_without_model_exclusivity(tmp_path):
    adapter, extra, live, story, _ = prepared(tmp_path)
    result = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Own subject'})
    assert result['receipt']['model_id'] == live.model_id
    assert len(live.calls) == 1
    assert not adapter.client.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
async def test_owner_exact_without_primary_text_proof_uses_other_qualified_route(tmp_path):
    adapter, extra, live, story, entries = prepared(tmp_path)
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': [e for e in entries
        if e['model_id'] != 'mimo-v2.6-flash-free']}, 3600)
    result = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Own subject'})
    assert result['receipt']['model_id'] == live.model_id and len(live.calls) == 1
    assert not adapter.client.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
async def test_unknown_original_live_observed_after_target_policy_without_primary_fallback(tmp_path):
    adapter, extra, live, story, entries = prepared(tmp_path, owner=False)
    live.mode = 'submitted'
    with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    original_id = live.calls[0]['attempt_id']
    story['research_json'] = json.dumps({'visual_identity': {'status': 'owner_confirmed', 'selection_basis': 'owner_exact'}})
    live.mode = 'completed'
    observed = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert observed['receipt']['readback_only']
    assert [call['attempt_id'] for call in live.calls] == [original_id, original_id]
    assert not adapter.client.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
async def test_completed_original_live_reused_after_target_policy_and_qualification_expiry(tmp_path):
    adapter, extra, live, story, _ = prepared(tmp_path, owner=False)
    first = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    story['research_json'] = json.dumps({'visual_identity': {'status': 'owner_confirmed', 'selection_basis': 'owner_exact'}})
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': []}, 3600)
    recovered = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert recovered == first and len(live.calls) == 1
    assert not adapter.client.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [False, True])
async def test_addressed_pending_owner_readback_has_separate_moderate_wait(tmp_path, owner):
    adapter, extra, live, story, _ = prepared(tmp_path, owner=owner)
    adapter.order_fact_routes = lambda routes, ordinal=0, **kwargs: sorted(routes, key=lambda r: r['role'] != 'facts')
    adapter.client.mode = 'submitted'
    now = adapter.service.store.now()
    with pytest.raises(RetryableProviderError) as first:
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert first.value.retry_at == pytest.approx(now + (30 if owner else 300), abs=1)
    original = adapter.client.calls[0]
    receipt = adapter._fact_pool_receipts(story, 'page-0')['facts']
    paid_retry_at = receipt['route_failure']['retry_at']
    assert paid_retry_at >= now + 299
    with pytest.raises(RetryableProviderError) as pending:
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert pending.value.retry_at == pytest.approx(adapter.service.store.now() + (30 if owner else 300), abs=1)
    assert all(call['attempt_id'] == original['attempt_id'] for call in adapter.client.calls)
    assert adapter.client.calls[-1]['session_id'] == 'ses_existing'
    assert adapter.client.calls[-1]['message_id'] == 'msg_existing'
    assert adapter._fact_pool_receipts(story, 'page-0')['facts']['route_failure']['retry_at'] >= paid_retry_at
    assert not live.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
async def test_addressed_readback_binding_failure_retains_original_guard(tmp_path):
    from street_story.opencode_research import ResearchUnavailable

    adapter, extra, live, story, _ = prepared(tmp_path)
    adapter.order_fact_routes = lambda routes, ordinal=0, **kwargs: sorted(routes, key=lambda r: r['role'] != 'facts')
    adapter.client.mode = 'submitted'
    with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    original = adapter._fact_pool_receipts(story, 'page-0')['facts']
    async def changed(capsule, binding):
        raise ResearchUnavailable('research_attempt_binding_changed', original)
    adapter.client.extract_facts = changed
    with pytest.raises(RetryableProviderError, match='binding_changed') as guarded:
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert guarded.value.retry_at >= adapter.service.store.now() + 3599
    assert adapter.fact_unit_readback_at(story, 'page-0') is None
    assert not live.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.parametrize('identity', [None, {'status': 'match', 'selection_basis': 'owner_exact'},
    {'status': 'owner_confirmed', 'selection_basis': 'owner_live'}])
@pytest.mark.asyncio
async def test_automatic_and_voice_owner_routes_keep_existing_assignment(tmp_path, identity):
    adapter, extra, live, story, _ = prepared(tmp_path, owner=False)
    story['research_json'] = json.dumps({'visual_identity': identity})
    result = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Original'})
    assert result['receipt']['model_id'] == 'gemini-3.8-live' and len(live.calls) == 1
    assert not adapter.client.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('observing', [False, True])
async def test_review_owner_packet_uses_qualified_pool_and_original_live_is_observed(tmp_path, observing):
    from test_headless_fact_review_qualification import original_fixture, proof
    svc, job, harness, reviewer, packet, unit, saved, live, _, args = await original_fixture(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, job['story_id'])
        research = json.loads(row['research_json'])
        research['visual_identity'].update(status='owner_confirmed', selection_basis='owner_exact')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), job['story_id']))
        if not observing:
            db.execute("DELETE FROM research_provider_attempts WHERE attempt_id='original-read'")
    primary_client = SimpleNamespace(model_id='mimo-v2.6-flash-free', provider_id='opencode',
        endpoint='http://127.0.0.1:4097', directory='/existing/research')
    primary = {**live, 'role': 'facts', 'provider_id': primary_client.provider_id,
        'model_id': primary_client.model_id, 'endpoint': primary_client.endpoint, 'client': primary_client}
    primary_proof = proof(primary)
    primary_proof['directory'] = primary_client.directory
    svc.store.cache_put('fact-semantic-verification-v1', {'routes': [proof(live), primary_proof]}, 3600)
    sends = []
    async def run(story, role, received_unit, invoke, *, client):
        sends.append((role, client.model_id))
        return {'result': copy.deepcopy(args)}
    selector = object.__new__(ProductResearchAdapter)
    selector.client = primary_client
    svc.providers.research = SimpleNamespace(_fact_pool_routes=lambda: [live, primary], run=run,
        new_fact_routes=selector.new_fact_routes, extract_fact_page=svc.providers.research.extract_fact_page)
    result = await reviewer._infer(packet, job, unit, {**saved, 'phase': 'observe_original'} if observing else {})
    assert result == args
    assert sends == [('facts_review_' + live['model_id'], live['model_id'])]
