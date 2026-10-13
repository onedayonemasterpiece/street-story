"""Research continuation follows evidence gaps, not a universal fact quota."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agents import QUALIFICATION_KEY, _persist_plan, agent_context, run_fact_agents
from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import ProductResearchAdapter
from test_headless_facts import fixture


def test_failed_native_reads_are_resource_gaps_and_not_source_poverty():
    from street_story.fact_agents import _source_coverage, _coverage_gaps
    failed = {'requested_url': 'https://example.org/history', 'status': 'failed', 'error_code': 'http_403'}
    receipt = {'provider_id': 'codex_native', 'phase': 'completed', 'result': {'facts': []},
        'native_tool_calls': {'original-read': {'phase': 'completed', 'input': {'name': 'webfetch'},
            'result': {'documents': [], 'read_attempts': [failed]}}}}
    coverage = _source_coverage(receipt)
    assert coverage['source_read_failures'] == [failed]
    gaps = _coverage_gaps({'phase': 'completed', 'candidate_count': 0, **coverage})
    assert 'source_read_unavailable' in gaps
    # A usable independent alternative is not invalidated by an earlier HTTP failure.
    assert 'source_read_unavailable' not in _coverage_gaps({'phase': 'completed', 'candidate_count': 1, **coverage})


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['single_clean', 'many_rejected', 'many_unverified', 'empty_first', 'unknown', 'empty_pool'])
async def test_qualified_work_and_reserves_follow_closed_evidence_gaps(tmp_path, monkeypatch, mode):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [AgentRoute('google', name, 'https://example.org', object()) for name in ('first', 'reserve', 'unused')]
    if mode == 'empty_pool':
        routes = []
    monkeypatch.setattr('street_story.fact_agents.SystemRandom',
        lambda: SimpleNamespace(shuffle=lambda _rows: None))
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', lambda *_args, **_kwargs: None)
    calls = []

    async def invoke(_adapter, current, _story, _context, *, binding, guard):
        guard()
        calls.append(current.model_id)
        first = current.model_id == 'first'
        count = (0 if mode == 'empty_first' and first else 4 if mode == 'many_unverified' and first
            else 3 if mode == 'many_rejected' and first else 1)
        candidates = [{'text': f'{current.model_id}-{index}', 'url': 'https://source.example/article'}
            for index in range(count)]
        receipt = {'binding': binding, 'phase': 'unknown' if mode == 'unknown' and first else 'completed',
            'result': {'facts': [] if mode == 'unknown' and first else candidates},
            'rejected_candidates': [{'reason': 'agent_quote_too_long'}] if mode == 'many_rejected' and first else []}
        adapter._checkpoint_receipt(binding, receipt)
        if receipt['phase'] == 'unknown':
            raise RetryableProviderError('controlled_response_unknown')
        return {'receipt': receipt, 'result': receipt['result']}

    async def admit(_job, _run, candidate, _agent):
        # Admission is controlled here; exercise the coordinator's actual
        # distinct eligible-ID lookup, not candidate count masquerading as it.
        fact_id = candidate['text']
        if mode == 'many_unverified' and fact_id == 'first-3':
            return {'status': 'unverified', 'reason': 'controlled_confirmation_pending'}
        with service.store.tx() as db:
            db.execute('INSERT OR IGNORE INTO fact_assertions(story_id,assertion_id,display_text,'
                "review_status,eligibility,created_at,updated_at) VALUES(?,?,?,'eligible','eligible',?,?)",
                (job['story_id'], fact_id, fact_id, service.store.now(), service.store.now()))
        return {'status': 'supported', 'fact_id': fact_id}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    service.confirm_agent_fact = admit
    try:
        outcome = await run_fact_agents(adapter, job, 'headless-run', 'History')
        if mode == 'empty_pool':
            assert calls == []
            assert outcome == {'outcome': 'resource_blocked', 'reason': 'research_routes_unavailable'}
            return
        saved = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')
        assert saved['primary_count'] == 1 and saved['provider_availability'] == 'reduced'
        assert outcome['provider_availability'] == 'reduced'
        if mode == 'single_clean':
            assert calls == ['first'] and outcome['eligible_count'] == 1
        else:
            expected_calls = ['first'] if mode == 'unknown' else ['first', 'reserve']
            assert calls == expected_calls
            assert outcome['eligible_count'] == (4 if mode in {'many_rejected', 'many_unverified'}
                else 1 if mode == 'empty_first' else 0)
            if mode == 'many_rejected':
                assert outcome['content_gaps'] == ['candidate_format_rejections']
            elif mode == 'unknown':
                assert saved['routes'][routes[0].route_id]['phase'] == 'unknown'
                original = copy.deepcopy(saved)
                await run_fact_agents(adapter, job, 'headless-run', 'Changed goal')
                assert calls == expected_calls
                assert service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run') == original
            elif mode == 'empty_first':
                assert outcome['content_gaps'] == ['no_source_backed_candidates']
            else:
                assert outcome['content_gaps'] == ['candidate_admission_unresolved']
                assert outcome['coverage_complete'] is False
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unknown_keeps_neighbor_running_and_uses_bounded_unused_independent_route(tmp_path, monkeypatch):
    import asyncio
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [AgentRoute(provider, 'controlled', 'https://example.org', object())
        for provider in ('google', 'gigachat', 'opencode')]
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)
    monkeypatch.setattr('street_story.fact_agents.SystemRandom', lambda: SimpleNamespace(shuffle=lambda _rows: None))
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', lambda *_args, **_kwargs: None)
    active, peak, calls, closed = 0, 0, [], []
    neighbor_ready, reserve_ready = asyncio.Event(), asyncio.Event()

    async def invoke(_adapter, route, _story, _context, *, binding, guard):
        nonlocal active, peak
        guard()
        calls.append(route.provider_id)
        active += 1
        peak = max(peak, active)
        try:
            if route.provider_id == 'google':
                await neighbor_ready.wait()
                receipt = {'binding': binding, 'phase': 'unknown'}
                adapter._checkpoint_receipt(binding, receipt)
                raise RetryableProviderError('controlled_response_unknown')
            if route.provider_id == 'gigachat':
                neighbor_ready.set()
                await reserve_ready.wait()
            else:
                reserve_ready.set()
            receipt = {'binding': binding, 'phase': 'completed', 'result': {'facts': []}}
            adapter._checkpoint_receipt(binding, receipt)
            closed.append(route.provider_id)
            return {'receipt': receipt, 'result': receipt['result']}
        finally:
            active -= 1

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    try:
        await asyncio.wait_for(run_fact_agents(adapter, job, 'headless-run', 'History'), 5)
        assert calls == ['google', 'gigachat', 'opencode']
        assert peak == 2 and set(closed) == {'gigachat', 'opencode'}
        original = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')
        assert original['routes'][routes[0].route_id]['phase'] == 'unknown'
        await run_fact_agents(adapter, job, 'headless-run', 'Changed goal')
        assert len(calls) == 3
        assert service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run') == original
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('parent_unknown', [False, True, 'cancelled', 'closed_failed'])
async def test_owned_read_continues_before_reserve_and_reopen_reuses_same_extraction(tmp_path, monkeypatch, parent_unknown):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [AgentRoute('google', name, 'https://example.org', object()) for name in ('first', 'unused')]
    independent = parent_unknown in {True, 'cancelled'}
    failed = parent_unknown in {'cancelled', 'closed_failed'}
    if independent:
        routes.append(AgentRoute('gigachat', 'independent-extractor', 'https://example.org', object()))
        # A frozen one-provider plan may acquire a healthy independent source
        # extractor later; original discovery inputs and route_ids stay frozen.
        monkeypatch.setattr('street_story.fact_agents.route_plan', lambda _routes: routes[:1])
    monkeypatch.setattr('street_story.fact_agents.SystemRandom', lambda: SimpleNamespace(shuffle=lambda _rows: None))
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)
    calls, extractions, agents = [], [], []
    source = {'source_version_id': 'owned-version', 'content_sha256': 'b' * 64,
        'requested_url': 'https://source.example/article', 'final_url': 'https://source.example/article'}

    async def discover(_adapter, current, _story, _context, *, binding, guard):
        guard()
        calls.append(current.model_id)
        receipt = {'binding': binding, 'phase': 'failed' if failed else 'unknown' if parent_unknown else 'completed',
            'provider_id': current.provider_id, 'result': {'facts': []}, 'sources': [source]}
        if failed:
            receipt.update(last_request_sent=True, error_code='CancelledError' if independent else 'controlled_closed_http',
                provider_send_state='possibly_sent' if independent else 'response_closed')
        adapter._checkpoint_receipt(binding, receipt)
        if parent_unknown:
            raise RetryableProviderError('controlled_response_unknown')
        return {'receipt': receipt, 'result': receipt['result']}

    def cohort(_adapter, original, *, story, context, guard):
        guard()
        return {'original_attempt_id': original['binding']['attempt_id'], 'source_cohort_sha256': 'c' * 64,
            'sources': [source]}

    async def extract(_adapter, _route, _story, _context, owned, *, original_receipt, binding, guard):
        guard()
        assert _route.provider_id == ('gigachat' if independent else 'google')
        extractions.append(binding['attempt_id'])
        assert original_receipt['result'] == {'facts': []} and owned['sources'] == [source]
        receipt = {'binding': binding, 'phase': 'completed', 'sources': [source],
            'result': {'facts': [{'text': 'Controlled useful finding', 'url': source['final_url']}]}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': receipt['result']}

    async def admit(_job, _run, _candidate, agent):
        agents.append(agent)
        with service.store.tx() as db:
            db.execute('INSERT OR IGNORE INTO fact_assertions(story_id,assertion_id,display_text,'
                "review_status,eligibility,created_at,updated_at) VALUES(?,?,?,'eligible','eligible',?,?)",
                (job['story_id'], 'controlled-finding', _candidate['text'], service.store.now(), service.store.now()))
        return {'status': 'supported', 'fact_id': 'controlled-finding'}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', discover)
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', cohort)
    monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', extract)
    service.confirm_agent_fact = admit
    try:
        outcome = await run_fact_agents(adapter, job, 'headless-run', 'History')
        saved = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')
        state = saved['routes'][routes[0].route_id]
        assert state['unproposed_source_version_ids'] == ['owned-version']
        assert state['retained']['phase'] == 'completed'
        assert state['phase'] == ('failed' if failed else 'unknown' if parent_unknown else 'completed')
        assert outcome['outcome'] == ('useful_partial' if parent_unknown else 'useful_complete')
        assert calls == ['first'] and len(extractions) == 1
        assert agents[0]['retained_source_cohort_sha256'] == 'c' * 64
        assert agents[0]['original_attempt_id'] == state['attempt_id']
        await run_fact_agents(adapter, job, 'headless-run', 'Changed goal')
        assert calls == ['first'] and len(extractions) == 1
        with service.store.connection() as db:
            rows = list(db.execute('SELECT role,receipt_json FROM research_provider_attempts'))
        assert len(rows) == 2 and sum('retained' in row['role'] for row in rows) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_legacy_completed_state_recovers_closed_read_metadata_without_resending(tmp_path, monkeypatch):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    route = AgentRoute('gigachat', 'controlled', 'https://example.org', object())
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: [route])
    official = {'source_version_id': 'official-read', 'content_sha256': 'b' * 64,
        'requested_url': 'https://official.example/article', 'final_url': 'https://official.example/article'}
    calls, extractions = [], []

    async def discover(_adapter, _route, _story, _context, *, binding, guard):
        guard()
        calls.append(binding['attempt_id'])
        other = {'source_version_id': 'other-read', 'content_sha256': 'd' * 64,
            'requested_url': 'https://other-read.example/article', 'final_url': 'https://other-read.example/article'}
        receipt = {'binding': binding, 'phase': 'completed', 'sources': [official, other],
            'result': {'facts': [{'text': f'Original-{index}', 'url': 'https://other-read.example/article'}
                for index in range(3)]}, 'rejected_candidates': [{'reason': 'agent_source_not_read'}]}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': receipt['result']}

    async def admit(_job, _run, candidate, _agent):
        fact_id = candidate['text']
        with service.store.tx() as db:
            db.execute('INSERT OR IGNORE INTO fact_assertions(story_id,assertion_id,display_text,'
                "review_status,eligibility,created_at,updated_at) VALUES(?,?,?,'eligible','eligible',?,?)",
                (job['story_id'], fact_id, fact_id, service.store.now(), service.store.now()))
        return {'status': 'supported', 'fact_id': fact_id}

    def cohort(_adapter, original, *, story, context, guard):
        guard()
        return {'original_attempt_id': original['binding']['attempt_id'],
            'source_cohort_sha256': 'c' * 64, 'sources': [official]}

    async def extract(_adapter, _route, _story, _context, owned, *, original_receipt, binding, guard):
        guard()
        extractions.append(binding['attempt_id'])
        receipt = {'binding': binding, 'phase': 'completed', 'sources': [official],
            'result': {'facts': [{'text': 'Additional official finding', 'url': official['final_url']}]}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': receipt['result']}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', discover)
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', lambda *_args, **_kwargs: None)
    service.confirm_agent_fact = admit
    try:
        await run_fact_agents(adapter, job, 'headless-run', 'History')
        stage = 'fact_agent_plan:headless-run'
        legacy = service.store.checkpoint_get(job['id'], stage)
        old = legacy['routes'][route.route_id]
        assert old['supported_count'] == 3
        old.pop('rejected_candidate_count')
        old.pop('unproposed_source_version_ids')
        service.store.checkpoint_put(job['id'], stage, legacy)
        with service.store.connection() as db:
            original_receipt = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (old['attempt_id'],)).fetchone()[0]
        monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', cohort)
        monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', extract)
        outcome = await run_fact_agents(adapter, job, 'headless-run', 'History')
        state = service.store.checkpoint_get(job['id'], stage)['routes'][route.route_id]
        assert state['rejected_candidate_count'] == 1
        assert state['unproposed_source_version_ids'] == ['official-read']
        assert state['retained']['phase'] == 'completed'
        assert len(calls) == len(extractions) == 1 and outcome['eligible_count'] == 4
        with service.store.connection() as db:
            assert db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (old['attempt_id'],)).fetchone()[0] == original_receipt
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['failed_cancelled', 'unknown', 'created_not_sent'])
async def test_missing_route_state_recovers_original_journal_before_any_discovery_send(tmp_path, monkeypatch, phase):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    google = AgentRoute('google', 'original', 'https://example.org', object())
    giga = AgentRoute('gigachat', 'retained-extractor', 'https://example.org', object())
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: [google, giga])
    headless = HeadlessFacts(service)
    story, research, _ = headless._snapshot(job, 'headless-run')
    context = agent_context(research['visual_identity'], 'Original goal', 'headless-run', service.store.now())
    _persist_plan(adapter, job, 'headless-run', headless, story['_fact_research_control_revision'], initial={
        'pipeline': QUALIFICATION_KEY, 'route_ids': [google.route_id], 'primary_count': 1,
        'provider_availability': 'reduced', 'context': context, 'routes': {}})
    unit = {'run_id': 'headless-run', 'context': context, 'route_id': google.route_id}
    digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    binding, _ = adapter.attempt({**story, '_fact_pool_unit_id': 'agent_' + digest[:24],
        '_fact_pool_input_sha256': digest}, 'facts_agent_' + google.route_id, unit)
    source = {'source_version_id': 'completed-original-read', 'content_sha256': 'd' * 64,
        'requested_url': 'https://source.example/article', 'final_url': 'https://source.example/article'}
    original = {'binding': binding, 'provider_id': 'google', 'phase': 'failed' if phase == 'failed_cancelled'
        else 'unknown' if phase == 'unknown' else 'created', 'last_request_sent': phase != 'created_not_sent',
        'provider_send_state': 'not_sent' if phase == 'created_not_sent' else 'possibly_sent',
        'sources': [] if phase == 'created_not_sent' else [source],
        **({'error_code': 'CancelledError'} if phase == 'failed_cancelled' else {})}
    adapter._checkpoint_receipt(binding, original)
    with service.store.connection() as db:
        original_json = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
            (binding['attempt_id'],)).fetchone()[0]
    discoveries, extractions = [], []

    async def discover(_adapter, route, _story, _context, *, binding, guard):
        assert phase == 'created_not_sent', 'An original uncertain send must not be repeated'
        guard()
        discoveries.append(binding['attempt_id'])
        receipt = {'binding': binding, 'phase': 'completed', 'result': {'facts': []}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': receipt['result']}

    def cohort(_adapter, receipt, *, story, context, guard):
        guard()
        if not receipt.get('sources'):
            return None
        return {'original_attempt_id': receipt['binding']['attempt_id'], 'source_cohort_sha256': 'e' * 64,
            'sources': [source]}

    async def extract(_adapter, route, _story, _context, _cohort, *, original_receipt, binding, guard):
        guard()
        assert route.provider_id == 'gigachat'
        extractions.append(binding['attempt_id'])
        receipt = {'binding': binding, 'phase': 'completed', 'sources': [source],
            'result': {'facts': [{'text': 'Controlled retained finding', 'url': source['final_url']}]}}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': receipt['result']}

    async def admit(_job, _run, _candidate, agent):
        assert agent['original_attempt_id'] == binding['attempt_id']
        return {'status': 'supported'}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', discover)
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort', cohort)
    monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', extract)
    service.confirm_agent_fact = admit
    try:
        await run_fact_agents(adapter, job, 'headless-run', 'Changed goal must not replace original')
        state = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')['routes'][google.route_id]
        assert state['attempt_id'] == binding['attempt_id']
        with service.store.connection() as db:
            rows = list(db.execute('SELECT attempt_id,role,receipt_json FROM research_provider_attempts'))
        if phase == 'created_not_sent':
            assert discoveries == [binding['attempt_id']] and extractions == [] and len(rows) == 1
        else:
            assert discoveries == [] and len(extractions) == 1 and len(rows) == 2
            assert state['phase'] == original['phase'] and state['original_send_unresolved'] is True
            assert state['retained']['extraction_route_id'] == giga.route_id
            assert next(row['receipt_json'] for row in rows if row['attempt_id'] == binding['attempt_id']) == original_json
            await run_fact_agents(adapter, job, 'headless-run', 'Original goal')
            assert discoveries == [] and len(extractions) == 1
    finally:
        await reader.search_http.aclose()
