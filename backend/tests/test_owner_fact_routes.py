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
async def test_new_owner_exact_units_use_only_qualified_primary_mimo(tmp_path):
    adapter, extra, live, story, _ = prepared(tmp_path)
    result = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Own subject'})
    assert result['receipt']['model_id'] == 'mimo-v2.6-flash-free'
    assert len(adapter.client.calls) == 1
    assert not live.calls and not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
async def test_owner_exact_without_qualified_mimo_waits_without_other_send(tmp_path):
    adapter, extra, live, story, entries = prepared(tmp_path)
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': [e for e in entries
        if e['model_id'] != 'mimo-v2.6-flash-free']}, 3600)
    with pytest.raises(RetryableProviderError, match='pool_unverified'):
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Own subject'})
    assert not adapter.client.calls and not live.calls and not adapter.giga.calls and not extra.calls


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
async def test_review_new_owner_packet_uses_mimo_but_original_live_is_observed(tmp_path, observing):
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
    assert sends == [('facts_review_' + (live['model_id'] if observing else 'mimo-v2.6-flash-free'),
                      live['model_id'] if observing else 'mimo-v2.6-flash-free')]
