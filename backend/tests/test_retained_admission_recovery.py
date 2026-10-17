"""Ordinary same-job recovery distinguishes refused admission from inference."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agents import agent_context, run_fact_agents
from street_story.headless_facts import HeadlessFacts
from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter
from street_story.service import ConflictError, canonical
from test_headless_facts import fixture


async def saved_refusals(tmp_path, monkeypatch, *, count=1, legacy=False):
    service, job, _, reader, _ = await fixture(tmp_path)
    now = [1000.]
    monkeypatch.setattr(service.store, 'now', lambda: now[0])
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    google = AgentRoute('google', 'controlled-google', 'https://example.org', object())
    giga = AgentRoute('gigachat', 'controlled-giga', 'https://example.org', object())
    routes = [google, giga][:count]
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: [google, giga])
    story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
    context = agent_context(research['visual_identity'], 'History', 'headless-run', now[0])
    plan = {'context': context, 'route_ids': [route.route_id for route in routes],
        'primary_count': count, 'routes': {}}
    originals, refusals, cohorts = {}, {}, {}
    for index, route in enumerate(routes):
        unit = {'run_id': 'headless-run', 'context': context, 'route_id': route.route_id}
        digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        owned = {**story, '_fact_pool_unit_id': 'agent_'+digest[:24], '_fact_pool_input_sha256': digest}
        original, _ = adapter.attempt(owned, 'facts_agent_'+route.route_id, unit)
        source = {'source_version_id': 'source-'+str(index), 'content_sha256': 'b'*64,
            'requested_url': f'https://source.example/article{index}', 'final_url': f'https://source.example/article{index}'}
        receipt = {'binding': original, 'phase': 'completed', 'provider_id': route.provider_id,
            'provider_send_state': 'response_closed', 'result': {'facts': []}, 'sources': [source]}
        adapter._checkpoint_receipt(original, receipt)
        originals[original['attempt_id']] = canonical(receipt)
        cohort = {'original_attempt_id': original['attempt_id'], 'source_cohort_sha256': str(index+1)*64,
            'sources': [source]}
        cohorts[original['attempt_id']] = cohort
        retained_unit = {'run_id': 'headless-run', 'context': context, 'route_id': giga.route_id,
            'original_route_id': route.route_id, 'original_attempt_id': original['attempt_id'],
            'source_cohort_sha256': cohort['source_cohort_sha256']}
        retained_digest = hashlib.sha256(json.dumps(retained_unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        retained_story = {**story, '_fact_pool_unit_id': 'agent_retained_'+retained_digest[:24],
            '_fact_pool_input_sha256': retained_digest}
        binding, _ = adapter.attempt(retained_story, 'facts_agent_retained_'+giga.route_id, retained_unit)
        refusal = {'binding': binding, 'phase': 'failed', 'contract': 'research-agent-owned-source-extraction-v1',
            'provider_send_state': 'not_sent', 'model_attempts': [], 'retained_source_packet': cohort,
            'source_cohort_sha256': cohort['source_cohort_sha256'], 'original_attempt_id': original['attempt_id'],
            'sources': [source], 'error_code': 'RESOURCE_NO_CAPACITY',
            'route_failure': {'code': 'RESOURCE_NO_CAPACITY', 'retry_at': 1100.+index*100}}
        adapter._checkpoint_receipt(binding, refusal)
        refusals[binding['attempt_id']] = canonical(refusal)
        child, _ = adapter.attempt(retained_story, 'facts_agent_turn_'+giga.route_id,
            {'parent_attempt_id': binding['attempt_id'], 'turn': 0})
        child_receipt = {'binding': child, 'phase': 'failed', 'parent_attempt_id': binding['attempt_id'],
            'provider_send_state': 'not_sent', 'model_attempts': []}
        adapter._checkpoint_receipt(child, child_receipt)
        refusals[child['attempt_id']] = canonical(child_receipt)
        plan['routes'][route.route_id] = {'phase': 'completed', 'attempt_id': original['attempt_id'],
            'candidate_count': 0, 'unproposed_source_version_ids': [source['source_version_id']],
            'retained': {'phase': 'failed', 'attempt_id': binding['attempt_id'],
                'source_cohort_sha256': cohort['source_cohort_sha256'], 'extraction_route_id': giga.route_id}}
    payload = json.loads(job['payload_json'])
    payload.update(input_revision='frozen-author-input', resume_run_id='headless-run', voice_session_ids=[])
    with service.store.tx() as db:
        research['research_budget']['work_units'] = {'pages': ['already-read']}
        research['automatic_research_outcome'] = {'outcome': 'useful_partial', 'reason': 'resource_refused'}
        if not legacy:
            research['input_revision'] = payload['input_revision']
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), job['story_id']))
        db.execute("UPDATE jobs SET payload_json=?,state='done' WHERE id=?", (canonical(payload), job['id']))
        db.execute("UPDATE research_runs SET state='partial' WHERE run_id='headless-run'")
        db.execute('INSERT INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?)',
            (job['id'], 'fact_agent_plan:headless-run', canonical(plan), now[0]))
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
    monkeypatch.setattr('street_story.fact_agent_providers.retained_source_cohort',
        lambda _adapter, original, **_kwargs: copy.deepcopy(cohorts[original['binding']['attempt_id']]))
    return service, job, adapter, reader, now, plan, originals, refusals


def request(job):
    return {'action': 'research', 'candidate_id': 'wiki:77',
        'expected_identity_generation': 0, 'expected_research_control_revision': 0}


def state(service, job):
    with service.store.connection() as db:
        row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        research = json.loads(service._story_row(db, job['story_id'])['research_json'])
        journals = dict(db.execute('SELECT attempt_id,receipt_json FROM research_provider_attempts'))
    return row, research, journals


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_normal_same_scope_request_resumes_same_job_and_closed_source_unit(tmp_path, monkeypatch, legacy):
    service, job, adapter, reader, now, plan, originals, refusals = await saved_refusals(
        tmp_path, monkeypatch, count=2, legacy=legacy)
    calls = []
    before = state(service, job)
    try:
        service.mutate_facts(job['story_id'], 'resume', request(job))
        after = state(service, job)
        assert after[0]['state'] == 'retry' and after[0]['available_at'] == 1200
        assert after[0]['payload_json'] == job['payload_json'] and after[0]['attempts'] == 1
        assert after[1]['research_budget'] == before[1]['research_budget']
        assert after[1]['input_revision'] == 'frozen-author-input'
        assert 'automatic_research_outcome' not in after[1]
        assert after[2] == before[2]
        service.mutate_facts(job['story_id'], 'resume', request(job))
        service.mutate_facts(job['story_id'], 'join-same-job', request(job))
        assert state(service, job) == after
        assert service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')['context'] == plan['context']

        async def forbidden(*_args, **_kwargs):
            raise AssertionError('Cold discovery must not run')
        monkeypatch.setattr('street_story.fact_agents.run_tool_agent', forbidden)
        async def extract(_adapter, _route, _story, _context, cohort, *, original_receipt, binding, guard):
            guard()
            calls.append(binding)
            assert binding['prior_not_sent_attempt_id'] in refusals
            source = cohort['sources'][0]
            receipt = {'binding': binding, 'phase': 'completed', 'provider_send_state': 'response_closed',
                'sources': [source], 'result': {'facts': [{'text': source['source_version_id'], 'url': source['final_url']}]}}
            adapter._checkpoint_receipt(binding, receipt)
            return {'receipt': receipt, 'result': receipt['result']}
        monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', extract)
        async def confirm(_job, _run, candidate, _agent):
            with service.store.tx() as db:
                db.execute('INSERT INTO fact_assertions(story_id,assertion_id,display_text,review_status,eligibility,created_at,updated_at) '
                    "VALUES(?,?,?,'eligible','eligible',?,?)", (job['story_id'], candidate['text'], candidate['text'], now[0], now[0]))
            return {'status': 'supported', 'fact_id': candidate['text']}
        service.confirm_agent_fact = confirm
        adapter.research_agent_facts = lambda current, run, goal: run_fact_agents(adapter, current, run, goal)
        service.providers.research = adapter
        monkeypatch.setattr(service, '_observe_terminal_attempts', lambda **_kwargs: None)
        assert await service.run_once(claim_kind='research', claim_story_id=job['story_id']) is False
        assert not calls
        now[0] = 1201.
        assert await service.run_once(claim_kind='research', claim_story_id=job['story_id']) is True
        done = state(service, job)
        assert done[0]['state'] == 'done' and done[0]['attempts'] == 2
        assert done[1]['research_outcome']['eligible_count'] == 2
        for key, value in before[1]['research_budget'].items():
            if key != 'last_progress_at':
                assert done[1]['research_budget'][key] == value
        assert len(calls) == 2 and all(binding['job_id'] == job['id'] for binding in calls)
        assert {key: done[2][key] for key in originals | refusals} == originals | refusals
        assert len(done[2]) == len(before[2])+2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['unknown', 'closed', 'child_unknown', 'stop', 'input', 'generation', 'epoch', 'new_goal'])
async def test_same_job_retry_never_bypasses_input_owner_stop_or_send_uncertainty(tmp_path, monkeypatch, boundary):
    service, job, _, reader, _, plan, _, _ = await saved_refusals(tmp_path, monkeypatch)
    retained_id = next(iter(plan['routes'].values()))['retained']['attempt_id']
    body = request(job)
    try:
        with service.store.tx() as db:
            research = json.loads(service._story_row(db, job['story_id'])['research_json'])
            if boundary == 'input':
                research['input_revision'] = 'different-author-input'
            elif boundary == 'stop':
                research['fact_research_cancelled'] = True
            elif boundary == 'generation':
                body['expected_identity_generation'] = 1
            elif boundary == 'epoch':
                body['expected_research_control_revision'] = 1
            elif boundary == 'new_goal':
                body['goal'] = 'A different requested aspect'
            elif boundary == 'child_unknown':
                db.execute("UPDATE research_provider_attempts SET receipt_json=json_set(receipt_json,'$.phase','unknown',"
                    "'$.provider_send_state','possibly_sent') WHERE json_extract(receipt_json,'$.parent_attempt_id')=?", (retained_id,))
            else:
                db.execute("UPDATE research_provider_attempts SET receipt_json=json_set(receipt_json,'$.phase',?,"
                    "'$.provider_send_state',?) WHERE attempt_id=?", ('unknown' if boundary == 'unknown' else 'failed',
                    'possibly_sent' if boundary == 'unknown' else 'response_closed', retained_id))
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), job['story_id']))
        before = state(service, job)
        if boundary in {'input', 'generation', 'epoch'}:
            with pytest.raises(ConflictError):
                service.mutate_facts(job['story_id'], 'blocked-request', body)
        else:
            service.mutate_facts(job['story_id'], 'blocked-request', body)
        after = state(service, job)
        assert after[0] == before[0] and after[2] == before[2]
        with service.store.connection() as db:
            jobs = list(db.execute("SELECT id FROM jobs WHERE story_id=? AND kind='research'", (job['story_id'],)))
        assert len(jobs) == (2 if boundary == 'new_goal' else 1)
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_real_scoped_stop_keeps_cancelled_run_closed_under_new_owner_key(tmp_path, monkeypatch):
    from street_story.research_control import stop_research
    service, job, _, reader, _, _, _, _ = await saved_refusals(tmp_path, monkeypatch)
    try:
        stop_research(service, job['story_id'], purpose='facts')
        before = state(service, job)
        body = {**request(job), 'expected_research_control_revision': 1}
        service.mutate_facts(job['story_id'], 'research-after-stop', body)
        assert state(service, job) == before
        with service.store.connection() as db:
            assert db.execute("SELECT state FROM research_runs WHERE run_id='headless-run'").fetchone()[0] == 'cancelled'
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='research' AND story_id=?", (job['story_id'],)).fetchone()[0] == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unsent_retry_limit_preserves_refusals_and_forbids_fourth_admission(tmp_path, monkeypatch):
    service, job, adapter, reader, now, _, originals, refusals = await saved_refusals(tmp_path, monkeypatch)
    calls = []
    try:
        async def refused(_adapter, _route, _story, _context, cohort, *, original_receipt, binding, guard):
            guard()
            calls.append(binding['attempt_id'])
            receipt = {'binding': binding, 'phase': 'failed', 'contract': 'research-agent-owned-source-extraction-v1',
                'provider_send_state': 'not_sent', 'model_attempts': [], 'retained_source_packet': cohort,
                'source_cohort_sha256': cohort['source_cohort_sha256'], 'original_attempt_id': cohort['original_attempt_id'],
                'sources': cohort['sources'], 'error_code': 'RESOURCE_NO_CAPACITY'}
            adapter._checkpoint_receipt(binding, receipt)
            raise ResearchUnavailable('RESOURCE_NO_CAPACITY', receipt)
        monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', refused)
        async def forbidden(*_args, **_kwargs):
            raise AssertionError('Original discovery must never repeat')
        monkeypatch.setattr('street_story.fact_agents.run_tool_agent', forbidden)
        service.mutate_facts(job['story_id'], 'resume-cap', request(job))
        now[0] = 1101.
        with service.store.tx() as db:
            db.execute("UPDATE jobs SET state='running',attempts=2 WHERE id=?", (job['id'],))
            current = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        with pytest.raises(RetryableProviderError) as waiting:
            await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert waiting.value.retry_at == 1401.
        assert state(service, job)[0]['state'] == 'running'
        before_due = state(service, job)[2]
        with pytest.raises(RetryableProviderError):
            await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert len(calls) == 1 and state(service, job)[2] == before_due
        now[0] = 1402.
        outcome = await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert outcome['reason'] == 'research_agent_retained_admission_exhausted'
        assert len(calls) == 2
        now[0] = 1800.
        outcome = await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert outcome['reason'] == 'research_agent_retained_admission_exhausted'
        before_fourth = state(service, job)[2]
        await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert state(service, job)[2] == before_fourth and len(calls) == 2
        assert {key: before_fourth[key] for key in originals | refusals} == originals | refusals
        from street_story.research_budget import finish_attempt
        with service.store.tx() as db:
            finish_attempt(service, db, job['story_id'], outcome=outcome['outcome'], reason=outcome['reason'], job=current)
        service.mutate_facts(job['story_id'], 'exhausted-new-key', request(job))
        assert state(service, job)[0]['state'] == 'done' and state(service, job)[2] == before_fourth
        with service.store.connection() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='research' AND story_id=?", (job['story_id'],)).fetchone()[0] == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_active_worker_keeps_same_job_retry_at_last_unsent_cooldown(tmp_path, monkeypatch):
    service, job, adapter, reader, now, _, originals, refusals = await saved_refusals(tmp_path, monkeypatch, count=2)
    try:
        async def refused(_adapter, _route, _story, _context, cohort, *, original_receipt, binding, guard):
            guard()
            receipt = {'binding': binding, 'phase': 'failed', 'contract': 'research-agent-owned-source-extraction-v1',
                'provider_send_state': 'not_sent', 'model_attempts': [], 'retained_source_packet': cohort,
                'source_cohort_sha256': cohort['source_cohort_sha256'], 'original_attempt_id': cohort['original_attempt_id'],
                'sources': cohort['sources'], 'error_code': 'RESOURCE_NO_CAPACITY',
                'provider_retry_after': 600 if cohort['source_cohort_sha256'][0] == '2' else 300}
            adapter._checkpoint_receipt(binding, receipt)
            raise ResearchUnavailable('RESOURCE_NO_CAPACITY', receipt)
        monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', refused)
        async def forbidden(*_args, **_kwargs):
            raise AssertionError('Cold discovery is outside this continuation')
        monkeypatch.setattr('street_story.fact_agents.run_tool_agent', forbidden)
        service.providers.research = adapter
        adapter.research_agent_facts = lambda current, run, goal: run_fact_agents(adapter, current, run, goal)
        monkeypatch.setattr(service, '_observe_terminal_attempts', lambda **_kwargs: None)
        service.mutate_facts(job['story_id'], 'active-resume', request(job))
        now[0] = 1201.
        assert await service.run_once(claim_kind='research', claim_story_id=job['story_id']) is True
        waiting = state(service, job)
        assert waiting[0]['state'] == 'retry' and waiting[0]['available_at'] == 1801.
        assert waiting[0]['payload_json'] == job['payload_json']
        assert 'automatic_research_outcome' not in waiting[1]
        assert waiting[1]['research_budget']['work_units'] == {'pages': ['already-read']}
        assert {key: waiting[2][key] for key in originals | refusals} == originals | refusals
        assert service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')['context']['run_id'] == 'headless-run'
        service.mutate_facts(job['story_id'], 'active-new-key-join', request(job))
        assert state(service, job) == waiting
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_giga_oauth_failure_records_typed_metadata_without_inference_or_private_body(monkeypatch):
    from contextlib import asynccontextmanager
    from types import ModuleType
    import sys
    from street_story.fact_agent_providers import run_tool_agent
    from street_story.gigachat_research import GigaChatProviderError
    sdk = ModuleType('ai_resource_control.client')
    sdk.estimate_input_tokens = lambda _body: 100
    monkeypatch.setitem(sys.modules, 'ai_resource_control.client', sdk)
    saved, callbacks = [], []
    @asynccontextmanager
    async def admission(_binding, workload):
        assert workload['max_provider_sends'] == 2
        async def finalize(*_args):
            pass
        yield SimpleNamespace(finalize=finalize)
    async def completion(_lease, _body, _receipts, _purpose, _output, before):
        callbacks.append('OAuth failed before inference callback')
        error = GigaChatProviderError('credential_failure', status=401, retry_at=9000)
        error.args = ('private OAuth response and credential must not be stored',)
        raise error
    client = SimpleNamespace(binding={'provider': 'gigachat', 'model': 'controlled'},
        admission=admission, _completion=completion)
    adapter = SimpleNamespace(service=SimpleNamespace(store=SimpleNamespace(now=lambda: 1000)),
        attempt=lambda *_args: ({'attempt_id': 'child'}, None),
        _checkpoint_receipt=lambda _binding, receipt: saved.append(copy.deepcopy(receipt)))
    route = AgentRoute('gigachat', 'controlled', 'https://api.example', client)
    with pytest.raises(ResearchUnavailable) as failed:
        await run_tool_agent(adapter, route, {}, {'run_id': 'run'}, binding={'attempt_id': 'parent'}, guard=lambda: None)
    receipt = failed.value.receipt
    assert receipt['phase'] == 'failed' and receipt['provider_send_state'] == 'not_sent'
    assert receipt['model_attempts'] == [] and 'last_request_sent' not in receipt
    assert receipt['provider_category'] == 'credential_failure' and receipt['provider_status'] == 401
    assert receipt['provider_retry_after'] == 8000 and receipt['error_code'] == 'gigachat_credential_failure'
    assert len(callbacks) == 1 and 'private OAuth' not in canonical(saved)
    child = next(row for row in reversed(saved) if row['binding']['attempt_id'] == 'child')
    assert child['provider_send_state'] == 'not_sent' and child['provider_category'] == 'credential_failure'


@pytest.mark.asyncio
@pytest.mark.parametrize('active_phase', ['retry', 'running_unknown'])
async def test_automatic_active_same_scope_request_joins_without_explicit_resume_marker(tmp_path, monkeypatch, active_phase):
    service, job, _, reader, _, plan, _, _ = await saved_refusals(tmp_path, monkeypatch)
    try:
        with service.store.tx() as db:
            research = json.loads(service._story_row(db, job['story_id'])['research_json'])
            research.pop('automatic_research_outcome')
            assert 'retained_admission_continuation' not in research
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), job['story_id']))
            db.execute('UPDATE jobs SET state=? WHERE id=?', ('running' if active_phase.endswith('unknown') else 'retry', job['id']))
            if active_phase.endswith('unknown'):
                retained_id = next(iter(plan['routes'].values()))['retained']['attempt_id']
                db.execute("UPDATE research_provider_attempts SET receipt_json=json_set(receipt_json,'$.phase','unknown',"
                    "'$.provider_send_state','possibly_sent') WHERE attempt_id=?", (retained_id,))
        before = state(service, job)
        service.mutate_facts(job['story_id'], 'no-marker-join', request(job))
        assert state(service, job) == before
        with service.store.connection() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='research' AND story_id=?", (job['story_id'],)).fetchone()[0] == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_expired_refusal_with_disappeared_qualification_does_not_spin_or_send(tmp_path, monkeypatch):
    service, job, adapter, reader, now, _, _, _ = await saved_refusals(tmp_path, monkeypatch)
    try:
        service.mutate_facts(job['story_id'], 'resume-with-proof', request(job))
        now[0] = 1201.
        with service.store.tx() as db:
            db.execute("UPDATE jobs SET state='running',attempts=2 WHERE id=?", (job['id'],))
            current = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: [])
        async def forbidden(*_args, **_kwargs):
            raise AssertionError('Unavailable proof cannot authorize any provider call')
        monkeypatch.setattr('street_story.fact_agent_providers.run_retained_source_agent', forbidden)
        monkeypatch.setattr('street_story.fact_agents.run_tool_agent', forbidden)
        before = state(service, job)[2]
        result = await run_fact_agents(adapter, current, 'headless-run', 'History')
        assert result['coverage_complete'] is False
        assert state(service, job)[2] == before
    finally:
        await reader.search_http.aclose()
