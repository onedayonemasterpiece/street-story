"""Ordinary runtime re-admits only its original candidates and judge receipts."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story.db import Store
from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agents import _persist_plan, run_fact_agents
from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import ProductResearchAdapter
from test_agent_evidence import CANDIDATE, setup


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['closed', 'closed_unverified', 'unknown', 'binding_changed', 'context_changed', 'stop', 'owner_B'])
async def test_runtime_replays_closed_candidates_without_agent_sdk_source_fetch_or_unknown_judge_send(
        tmp_path, monkeypatch, case):
    def interrupted(_service, _job):
        raise RetryableProviderError('controlled_interrupt_after_closed_judge')
    service, job, reader, fetches, judge_calls = await setup(tmp_path, after=interrupted)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    routes = [AgentRoute('google', 'original-model', 'https://example.org', object()),
        AgentRoute('gigachat', 'empty-model', 'https://example.org', object())]
    selected = routes[0].route_id
    monkeypatch.setattr('street_story.fact_agents.SystemRandom',
        lambda: SimpleNamespace(shuffle=lambda _rows: None))
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda _adapter: routes)
    agent_calls = []

    async def invoke(_adapter, route, _story, context, *, binding, guard):
        guard()
        agent_calls.append((route.route_id, copy.deepcopy(binding), copy.deepcopy(context)))
        result = {'facts': [CANDIDATE] if route.route_id == selected else []}
        receipt = {'phase': 'completed', 'binding': binding, 'result': result,
            'provider_id': route.provider_id, 'model_id': route.model_id}
        adapter._checkpoint_receipt(binding, receipt)
        return {'receipt': receipt, 'result': result}

    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    original_confirm = service.confirm_agent_fact
    if case == 'closed_unverified':
        async def unverified(*args):
            try:
                return await original_confirm(*args)
            except RetryableProviderError:
                return {'status': 'unverified', 'reason': 'controlled_commit_pending'}
        service.confirm_agent_fact = unverified
    if case == 'unknown':
        async def unknown(payload, *, checkpoint, guard):
            guard()
            judge_calls.append(payload)
            checkpoint({'attempt_id': 'verification-original', 'phase': 'unknown',
                'provider_id': 'google', 'model_id': 'verifier-model', 'provider_send_state': 'possibly_sent'})
            return {'status': 'unverified'}
        service.providers.gemini.verify_agent_evidence = unknown
    try:
        first = await run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        assert first['outcome'] == 'resource_blocked'
        assert len(agent_calls) == 2 and len(judge_calls) == 1 and len(fetches) == 1
        assert service.story(job['story_id'])['facts'] == []
        stage = 'fact_agent_plan:headless-run'
        before = service.store.checkpoint_get(job['id'], stage)
        state = before['routes'][selected]
        assert state['phase'] == ('completed' if case in {'unknown', 'closed_unverified'} else 'failed')
        original_attempt = state['attempt_id']
        if case == 'closed':
            for replacement in [{**state, 'phase': 'completed', 'attempt_id': 'different-attempt'},
                    {**state, 'phase': 'unknown'}]:
                assert _persist_plan(adapter, job, 'headless-run', HeadlessFacts(service), 0,
                    route_id=selected, state=replacement, recovery=True) == before
        with service.store.connection() as db:
            confirmation = db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='agent_evidence_confirmation'").fetchone()[0]
        if case in {'binding_changed', 'context_changed', 'stop', 'owner_B'}:
            with service.store.tx() as db:
                if case == 'binding_changed':
                    receipt = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                        (original_attempt,)).fetchone()[0])
                    receipt['binding']['fact_input_sha256'] = 'changed-input'
                    db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                        (json.dumps(receipt), original_attempt))
                elif case == 'context_changed':
                    changed = copy.deepcopy(before)
                    changed['context']['editorial_goal'] = 'Changed input'
                    db.execute('UPDATE research_checkpoints SET value_json=? WHERE job_id=? AND stage=?',
                        (json.dumps(changed), job['id'], stage))
                else:
                    story = service._story_row(db, job['story_id'])
                    research = json.loads(story['research_json'])
                    if case == 'stop':
                        research['fact_research_cancelled'] = True
                    else:
                        research['identity_generation'] = 1
                        research['visual_identity']['candidate_id'] = 'owner-B'
                    db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))

        async def forbidden(*_args, **_kwargs):
            raise AssertionError('Recovery must not send an agent, fetch a source, or send a judge')
        service.providers.gemini._fetch_page_documents = forbidden
        service.providers.gemini.verify_agent_evidence = forbidden
        service.confirm_agent_fact = original_confirm
        monkeypatch.setattr('street_story.fact_agents.run_tool_agent', forbidden)
        monkeypatch.setattr('street_story.fact_agents.route_plan', lambda _routes: pytest.fail('Original plan must not be rebuilt'))
        routes.clear()  # Loss of new-send qualification must not hide original closed evidence.
        service.store = Store(service.store.path)
        second = await run_fact_agents(adapter, job, 'headless-run', 'Changed goal')
        if case in {'closed', 'closed_unverified'}:
            assert second['outcome'] == 'useful_complete'
            after = service.store.checkpoint_get(job['id'], stage)
            assert after['context'] == before['context'] and after['route_ids'] == before['route_ids']
            assert after['routes'][selected]['phase'] == 'completed'
            assert after['routes'][selected]['attempt_id'] == original_attempt
            assert after['routes'][selected]['supported_count'] == 1
            fact = service.story(job['story_id'])['facts'][0]
            assert fact['eligibility'] == 'eligible'
            assert after['routes'][selected]['eligible_fact_ids'] == [fact['fact_id']]
        elif case in {'stop', 'owner_B'}:
            assert second is None
        else:
            assert second['outcome'] == 'resource_blocked'
            assert service.story(job['story_id'])['facts'] == []
            if case == 'unknown':
                with service.store.connection() as db:
                    assert db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='agent_evidence_confirmation'").fetchone()[0] == confirmation
        assert len(agent_calls) == 2 and len(judge_calls) == 1 and len(fetches) == 1
        with service.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM research_provider_attempts WHERE role LIKE 'facts_agent_%'").fetchone()[0] == 2
    finally:
        await reader.search_http.aclose()
