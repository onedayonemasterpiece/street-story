"""A model action advances the existing job; UNKNOWN only observes its original."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story.db import Store
from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute
from street_story.fact_agent_providers import native_tools
from street_story.fact_agents import GAP_CONTINUATIONS, run_fact_agents
from street_story.native_fact_research import CONTRACT
from street_story.research_adapter import ProductResearchAdapter
from test_headless_facts import fixture


def packet(action=None, *, sufficient=False, basis='unresolved_gap'):
    return {'facts': [], 'completion': {'research_sufficient': sufficient,
        'reason': 'Controlled researcher decision; not a fact-count test.',
        'remaining_gaps': [] if sufficient else ['Historical linkage remains'],
        'stop_basis': basis, 'next_action': action}}


def receipt(binding, context, raw, number):
    return {'binding': binding, 'phase': 'completed', 'provider_send_state': 'response_closed',
        'native_contract': CONTRACT, 'native_terminal_status': 'completed',
        'provider_id': 'codex_native', 'model_id': 'gpt-6-luna',
        'thread_id': 'same-owned-luna', 'turn_id': f'closed-{number}',
        'frozen_prompt': 'Frozen instructions\nINPUT=' + json.dumps(context, ensure_ascii=False),
        'provided_tools': native_tools(),
        'native_tool_calls': {}, 'native_threads': {'same-owned-luna': {'turn_id':f'closed-{number}'}},
        'raw_result': raw, 'result': {'facts': []}}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['complete', 'no_progress', 'unknown_second', 'closed_bad_basis'])
async def test_model_continuation_retains_one_job_frozen_parent_and_exact_unknown(tmp_path, monkeypatch, mode):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    route = AgentRoute('codex_native','gpt-6-luna','native',object())
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda adapter:[route])
    actions = [{'kind':'search','request':'Independent historical building query','reason':'Find historical linkage'},
        {'kind':'read','request':'https://example.org/own-history','reason':'Read discovered original'}]
    calls, readbacks, parents = [], [], []
    if mode == 'closed_bad_basis':
        async def rejected_reference(*args):
            return {'status': 'unverified', 'reason': 'native_fact_source_not_owned_read'}
        service.confirm_agent_fact = rejected_reference
    async def invoke(adapter, current, story, context, *, binding, guard):
        guard()
        if binding.get('phase') == 'unknown':
            readbacks.append(binding['attempt_id'])
            assert binding['thread_id'] == 'same-owned-luna' and binding['turn_id'] == 'closed-2'
            number, raw = 2, packet(sufficient=True,basis='material_exhausted')
        else:
            calls.append((binding['attempt_id'],copy.deepcopy(context)))
            number=len(calls)
            if number>1:
                pointer=context['native_continuation']
                assert pointer['original_attempt_id']==calls[-2][0]
                assert pointer['thread_id']=='same-owned-luna'
                with service.store.connection() as db:
                    parent=json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                        (pointer['original_attempt_id'],)).fetchone()[0])
                assert parent['phase']=='completed' and parent['provider_send_state']=='response_closed'
                parents.append(parent)
                if mode == 'closed_bad_basis' and number == 2:
                    diagnostic = context['native_continuation_diagnostics']
                    assert diagnostic['original_attempt_id'] == calls[0][0]
                    assert diagnostic['outcomes'] == [{'status': 'unverified', 'reason': 'native_fact_source_not_owned_read'}]
            if mode=='unknown_second' and number==2:
                value={'binding':binding,'phase':'unknown','provider_send_state':'possibly_sent',
                    'provider_id':'codex_native','model_id':'gpt-6-luna','thread_id':'same-owned-luna','turn_id':'closed-2'}
                adapter._checkpoint_receipt(binding,value)
                raise RetryableProviderError('controlled_native_unknown')
            raw=(packet(actions[0]) if mode=='no_progress' else packet(actions[number-1]) if number<3
                else packet(sufficient=True,basis='material_exhausted'))
        value=receipt(binding,context,raw,number)
        if mode == 'closed_bad_basis' and number == 1:
            value['result']['facts'] = [{'text': 'Controlled candidate with a rejected reference'}]
        adapter._checkpoint_receipt(binding,value)
        return {'result':value['result'],'receipt':value}
    monkeypatch.setattr('street_story.fact_agents.run_tool_agent',invoke)
    try:
        with service.store.connection() as db:
            jobs_before=[tuple(row) for row in db.execute('SELECT id,kind FROM jobs ORDER BY id')]
        if mode=='unknown_second':
            with pytest.raises(RetryableProviderError,match='original_pending'):
                await run_fact_agents(adapter,job,'headless-run','Original research')
            assert len(calls)==2 and not readbacks
            service.store=Store(service.store.path)
            await run_fact_agents(adapter,job,'headless-run','Ignored replacement goal')
            assert len(calls)==2 and readbacks==[calls[1][0]]
        else:
            await run_fact_agents(adapter,job,'headless-run','Original research')
        saved=service.store.checkpoint_get(job['id'],'fact_agent_plan:headless-run')
        state=saved['routes'][route.route_id]
        if mode=='no_progress':
            assert state['continuation_blocker']=='same_action_without_new_input'
            assert state['native_completion']['stop_basis']=='unresolved_gap'
            assert len(calls)==2
        elif mode in {'complete', 'closed_bad_basis'}:
            assert state['native_completion']['research_sufficient'] and len(calls)==3
            assert len(state['history'])==2
        with service.store.connection() as db:
            assert [tuple(row) for row in db.execute('SELECT id,kind FROM jobs ORDER BY id')]==jobs_before
        if mode!='no_progress':
            service.store=Store(service.store.path)
            await run_fact_agents(adapter,job,'headless-run','No new independent investigation')
            assert len(calls)==(3 if mode in {'complete', 'closed_bad_basis'} else 2)
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('native_phase', ['completed_resource', 'completed_resource_with_action', 'failed', 'unknown'])
async def test_closed_native_resource_failure_uses_pool_but_unknown_never_starts_alternative(tmp_path, monkeypatch, native_phase):
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter=ProductResearchAdapter(service,admission=SimpleNamespace())
    routes=[AgentRoute('codex_native','gpt-6-luna','native',object()),
        AgentRoute('gigachat','qualified-existing','https://example.org',object())]
    monkeypatch.setattr('street_story.fact_agents.configured_routes',lambda adapter:routes)
    calls=[]
    async def invoke(adapter, route, story, context, *, binding, guard):
        guard()
        calls.append(route.provider_id)
        action = ({'kind': 'search', 'request': 'Blocked owned action', 'reason': 'Provider resource unavailable'}
                  if native_phase == 'completed_resource_with_action' else None)
        value=receipt(binding,context,packet(action, basis='resource_unavailable'),1)
        if route.provider_id=='codex_native' and not native_phase.startswith('completed_resource'):
            value.update(phase=native_phase,provider_send_state='possibly_sent' if native_phase=='unknown' else 'response_closed')
            adapter._checkpoint_receipt(binding,value)
            raise RetryableProviderError('controlled_native_failure')
        if route.provider_id!='codex_native':
            value.update(provider_id=route.provider_id,model_id=route.model_id,raw_result={'facts':[]})
        adapter._checkpoint_receipt(binding,value)
        return {'result':{'facts':[]},'receipt':value}
    monkeypatch.setattr('street_story.fact_agents.run_tool_agent',invoke)
    try:
        if native_phase=='unknown':
            with pytest.raises(RetryableProviderError,match='original_pending'):
                await run_fact_agents(adapter,job,'headless-run','Original research')
            assert calls==['codex_native']
        else:
            await run_fact_agents(adapter,job,'headless-run','Original research')
            assert calls==['codex_native','gigachat']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['then_sufficient', 'no_new_knowledge', 'limit'])
async def test_reported_gaps_without_own_next_step_continue_same_thread_bounded(tmp_path, monkeypatch, mode):
    # Ending with unresolved gaps while resources remain is not "done": the
    # model's own gaps go back to the same thread, a bounded number of times.
    service, job, _, reader, _ = await fixture(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    route = AgentRoute('codex_native','gpt-6-luna','native',object())
    monkeypatch.setattr('street_story.fact_agents.configured_routes', lambda adapter:[route])
    calls = []
    async def invoke(adapter, current, story, context, *, binding, guard):
        guard()
        calls.append(copy.deepcopy(context))
        number = len(calls)
        if number > 1:
            pointer = context['native_continuation']
            assert pointer['next_action'] is None and pointer['unresolved_gaps'] == ['Historical linkage remains']
            assert 'unresolved_gaps' in pointer['meaning']  # guidance travels as input, not contract text
            assert pointer['thread_id'] == 'same-owned-luna' and pointer['parent_turn_id'] == f'closed-{number-1}'
        raw = packet(sufficient=True, basis='material_exhausted') if mode == 'then_sufficient' and number == 2 else packet()
        value = receipt(binding, context, raw, number)
        if mode == 'limit':  # each gap turn reads something new but never closes the gap
            value['native_tool_calls'] = {f'read-{number}': {'phase': 'completed', 'result': {'documents': [
                {'source_version_id': f'srcv_{number}', 'content_sha256': 'a'*64, 'chunks': [{'chunk_id': f'c{number}'}]}]}}}
        adapter._checkpoint_receipt(binding, value)
        return {'result': value['result'], 'receipt': value}
    monkeypatch.setattr('street_story.fact_agents.run_tool_agent', invoke)
    try:
        await run_fact_agents(adapter, job, 'headless-run', 'Original research')
        state = service.store.checkpoint_get(job['id'], 'fact_agent_plan:headless-run')['routes'][route.route_id]
        if mode == 'then_sufficient':
            assert len(calls) == 2 and state['native_completion']['research_sufficient']
        elif mode == 'no_new_knowledge':
            assert len(calls) == 2 and state['continuation_blocker'] == 'gap_continuation_without_new_knowledge'
        else:
            assert len(calls) == 1 + GAP_CONTINUATIONS and state['continuation_blocker'] == 'gap_continuation_limit'
        with service.store.connection() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] <= 1
    finally:
        await reader.search_http.aclose()


def test_native_research_profile_still_matches_the_deployed_qualification():
    # The installer refuses a release whose research profile differs from the
    # qualified one. Changing contract text, schema or tools needs a new agent
    # qualification first; update this pin together with that receipt.
    from types import SimpleNamespace
    from street_story.native_fact_research import NativeFactResearch
    assert NativeFactResearch.profile_fingerprint.fget(SimpleNamespace(timeout=None)) == (
        '220bef4b0d176d7d3d86d842a0215f8f3bf7d5609bd24340b82e2777c3827414')
