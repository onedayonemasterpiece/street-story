"""A restarted durable worker must observe the same addressed extraction."""
from contextvars import ContextVar
import json

import pytest

from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import ProductResearchAdapter
from test_headless_fact_pool import RUN, fixture
from test_verified_fact_pool import FactClient, Giga


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['mimo-v2.6-flash-free', 'nemotron-3-ultra-free'])
@pytest.mark.parametrize('fence', [None, 'future_due', 'missing_session', 'missing_message', 'google-live', 'gigachat', 'stop'])
async def test_due_original_fact_operation_survives_worker_restart(tmp_path, monkeypatch, model, fence):
    svc, job = fixture(tmp_path, count=1)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service, adapter.giga = svc, Giga()
    adapter._active_binding = ContextVar('pending-fact-test', default=None)
    primary = FactClient('mimo-v2.6-flash-free', adapter)
    extra = FactClient('nemotron-3-ultra-free', adapter)
    adapter.client = primary
    adapter._fact_extractor_clients = {'nemotron-3-ultra-free': extra}
    assigned = primary if model == primary.model_id else extra
    role = 'facts' if assigned is primary else 'facts_opencode_nemotron'
    adapter.order_fact_routes = lambda routes, ordinal=0, review=False: [r for r in routes if r['role'] == role]
    entries = [{'provider_id': 'opencode', 'model_id': model, 'endpoint': assigned.endpoint,
        'semantic_contract_verified': True, 'source_subject_negative_verified': True,
        'planned_modality_verified': True, 'known_claim_reuse_verified': True}]
    svc.store.cache_put('research-text-verification-v1', {'extractors': entries}, 3600)
    svc.providers.research = adapter
    harness = HeadlessFacts(svc)
    units = await harness._prepare_units(job, RUN, model, 0, limit=1)
    assert len(units) == 1
    story, _, _ = harness._snapshot(job, RUN, 0)
    context = {'coverage_goal': 'History', 'known_facts': []}
    assigned.mode = 'submitted'
    _, _, error = await harness._extract_unit(units[0], adapter, story, context, job)
    assert error is not None and len(assigned.calls) == 1
    original_id = assigned.calls[0]['attempt_id']
    if fence in {'missing_session', 'missing_message', 'google-live', 'gigachat'}:
        with svc.store.tx() as db:
            row = db.execute('SELECT * FROM research_provider_attempts WHERE attempt_id=?', (original_id,)).fetchone()
            receipt = json.loads(row['receipt_json'])
            if fence.startswith('missing_'):
                receipt.pop('session_id' if fence == 'missing_session' else 'message_id')
            else:
                receipt['provider_id'] = fence
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?', (json.dumps(receipt), original_id))
    clock = svc.store.now() + (1 if fence == 'future_due' else 301)
    monkeypatch.setattr(svc.store, 'now', lambda: clock)
    if fence == 'future_due':
        renewed = job
    else:
        svc.recover_jobs()
        renewed = svc._claim(claim_kind='research', claim_story_id=job['story_id'])
        assert renewed['id'] == job['id'] and renewed['attempts'] == job['attempts'] + 1
    if fence == 'stop':
        from street_story.research_control import stop_research
        stop_research(svc, job['story_id'], purpose='facts',
                      expected_photo_sha256=story['photo_sha256'], expected_identity_generation=0)
    # A new admission is no longer qualified; observing the original still is.
    svc.store.cache_put('research-text-verification-v1', {'extractors': []}, 3600)
    assigned.mode = 'completed'
    resumed = await harness._prepare_units(renewed, RUN, model, 0, limit=1)
    if fence:
        assert resumed == [] and len(assigned.calls) == 1
        assert not adapter.giga.calls and not (extra if assigned is primary else primary).calls
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0] == 1
        return
    assert len(resumed) == 1
    assert resumed[0]['page']['_unit_id'] == units[0]['page']['_unit_id']
    current, _, _ = harness._snapshot(renewed, RUN, 0)
    _, result, error = await harness._extract_unit(resumed[0], adapter, current, context, renewed)
    assert error is None and result['receipt']['readback_only']
    assert assigned.calls[-1]['attempt_id'] == original_id
    assert len(assigned.calls) == 2 and not adapter.giga.calls
    assert not (extra if assigned is primary else primary).calls
