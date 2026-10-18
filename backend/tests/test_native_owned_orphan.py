"""Synthetic crash equivalents only: never open historical or personal sessions."""
import copy
import json
from pathlib import Path

import pytest

from street_story import native_fact_research as native
from street_story import native_research_tools as transport
from street_story.errors import RetryableProviderError
from test_native_research_recovery import setup
from test_native_rollout_readback import INPUT, THREAD, TURN


TOOLS = [{'name': 'save_facts'}, {'name': 'webfetch'}]
OWNER = {'backend': {'pid': 999999, 'start_ticks': '10', 'boot_id': 'synthetic-boot'}}
LOST = {'kind': 'backend', 'original': OWNER['backend'], 'observed': {'pid': 999999, 'absent': True}}


def add_call(thread, *, arguments=None, answered=False, terminal=False, duplicate=False):
    path = Path(thread['path'])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [row for row in rows if not (row.get('payload') or {}).get('type') == 'task_complete']
    call = {'type': 'response_item', 'timestamp': '2026-10-14T00:00:00Z',
        'payload': {'type': 'function_call', 'name': 'save_facts',
        'call_id': 'save-1', 'arguments': json.dumps(arguments or {'facts': []}),
        'internal_chat_message_metadata_passthrough': {'turn_id': TURN}}}
    rows.append(call)
    if duplicate:
        rows.append(copy.deepcopy(call))
    if answered:
        rows.append({'type': 'response_item', 'payload': {'type': 'function_call_output',
            'call_id': 'save-1', 'output': 'closed original response'}})
    if terminal:
        rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': TURN}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def test_process_incarnation_is_required_not_an_empty_cache(monkeypatch):
    assert transport.lost_execution_owner(None) is None
    monkeypatch.setattr(transport, 'process_identity', lambda pid: OWNER['backend'])
    assert transport.lost_execution_owner(OWNER) is None
    monkeypatch.setattr(transport, 'process_identity', lambda pid: None)
    assert transport.lost_execution_owner(OWNER) is None  # Permission/error is not death.
    monkeypatch.setattr(transport, 'process_identity', lambda pid: {'pid': pid, 'absent': True})
    assert transport.lost_execution_owner(OWNER) == LOST
    monkeypatch.setattr(transport, 'process_identity', lambda pid: {**OWNER['backend'], 'start_ticks': '11'})
    assert transport.lost_execution_owner(OWNER)['kind'] == 'backend'  # Reused PID.


@pytest.mark.parametrize('change', ['input', 'model', 'steering', 'advanced', 'changed_call', 'foreign_turn'])
def test_journal_rejects_foreign_or_changed_ownership(tmp_path, monkeypatch, change):
    from test_native_rollout_readback import journal
    thread, _ = journal(tmp_path, monkeypatch)
    path = add_call(thread)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if change == 'input':
        rows[5]['payload']['content'][0]['text'] = 'another owner'
    elif change == 'model':
        rows[4]['payload']['model'] = 'another-model'
    elif change == 'steering':
        rows.insert(-1, copy.deepcopy(rows[5]))
    elif change == 'advanced':
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'new-owner-turn'}})
    elif change == 'changed_call':
        rows.append(copy.deepcopy(rows[-1]))
        rows[-1]['payload']['arguments'] = '{"facts":["changed"]}'
    else:
        rows[-1]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'foreign'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(ValueError, match='native_'):
        native.NativeFactResearch._callback_journal(thread, TURN, INPUT[0]['text'], TOOLS)


ENV_CONTEXT = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
    'content': [{'type': 'input_text', 'text': '<environment_context>\n  <cwd>/data</cwd>\n</environment_context>'}],
    'internal_chat_message_metadata_passthrough': {'turn_id': TURN,
        'content_item_kinds': ['environments.environment_context']}}}


@pytest.mark.parametrize('variant', ['codex_first_turn', 'unlabelled', 'after_input', 'extra_text'])
def test_journal_skips_only_codex_environment_context_before_owned_input(tmp_path, monkeypatch, variant):
    """Codex prepends <environment_context> to a thread's first turn; it is not other owner input."""
    from test_native_rollout_readback import journal
    thread, _ = journal(tmp_path, monkeypatch)
    path = add_call(thread)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    context = copy.deepcopy(ENV_CONTEXT)
    if variant == 'unlabelled':
        context['payload'].pop('internal_chat_message_metadata_passthrough')
    elif variant == 'extra_text':
        context['payload']['content'].append({'type': 'input_text', 'text': 'another owner'})
    rows.insert(6 if variant == 'after_input' else 5, context)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    if variant == 'codex_first_turn':
        calls, terminal = native.NativeFactResearch._callback_journal(thread, TURN, INPUT[0]['text'], TOOLS)
        assert [call['call_id'] for call in calls] == ['save-1'] and terminal is None
    else:
        with pytest.raises(ValueError, match='native_orphan_input_unverified'):
            native.NativeFactResearch._callback_journal(thread, TURN, INPUT[0]['text'], TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['before_callback', 'submitted', 'committed', 'completed_before_reply', 'load_closed'])
async def test_restart_reconciles_exact_save_before_interrupt_without_double_effect(tmp_path, monkeypatch, boundary):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = add_call(thread, duplicate=True)
    frozen = {'thread_id': THREAD, 'turn_id': TURN, 'name': 'save_facts', 'arguments': {'facts': []},
        'execution_contexts': [{'turn_id': TURN, 'model': native.MODEL, 'effort': None}]}
    value = {'saved_fact_ids': ['same-fact'], 'source_version_id': 'unchanged-source'}
    durable = {'save-1': value} if boundary in {'committed', 'completed_before_reply'} else {}
    effects = [] if not durable else ['original-commit']
    if boundary != 'before_callback':
        receipt['native_tool_calls'] = {'save-1': {'input': frozen, 'phase': 'submitted', 'submitted_at': 1}}
    if boundary == 'completed_before_reply':
        receipt['native_tool_calls']['save-1'].update(phase='completed', result=value)
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/resume':
                calls.append((method, params))
                if boundary == 'load_closed':
                    with path.open('a') as stream:
                        stream.write(json.dumps({'type': 'event_msg', 'payload': {
                            'type': 'turn_aborted', 'turn_id': TURN}}) + '\n')
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/interrupt':
                assert any(method == 'thread/resume' for method, _ in calls)
                assert durable == {'save-1': value}
                assert saved[-1]['orphan_recovery']['phase'] == 'interrupt_intent'
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'event_msg', 'payload': {
                        'type': 'turn_aborted', 'turn_id': TURN}}) + '\n')
            return await super().request(method, params, timeout)
    async def save(name, args, *, checkpoint_owner):
        assert name == 'save_facts' and args == {'facts': []}
        assert checkpoint_owner == {'thread_id': THREAD, 'turn_id': TURN, 'call_id': 'save-1'}
        if 'save-1' not in durable:
            effects.append('recovered-commit')
            durable['save-1'] = copy.deepcopy(value)
        return copy.deepcopy(durable['save-1'])
    async def no_quota(*args, **kwargs):
        raise AssertionError('Local save must survive quota loss')
    provider.client_factory = Client
    provider.permission.ensure = no_quota
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    kwargs = dict(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    with pytest.raises(RetryableProviderError, match='owned_orphan_terminal'):
        await provider.run(**kwargs)
    assert len(effects) == 1
    assert receipt['phase'] == 'failed' and receipt['native_terminal_status'] == 'interrupted'
    assert native.orphan_pointer(receipt)['parent_turn_id'] == TURN
    assert receipt['native_tool_calls']['save-1']['result']['saved_fact_ids'] == ['same-fact']
    # Repeated observation of that terminal never interrupts or saves again.
    with pytest.raises(RetryableProviderError, match='owned_orphan_terminal'):
        await provider.run(**kwargs)
    assert sum(method == 'turn/interrupt' for method, _ in calls) == int(boundary != 'load_closed')
    assert all(method in {'thread/read', 'thread/resume', 'turn/interrupt'} for method, _ in calls)
    assert len(effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', ['ack_only', 'alive', 'unknown_process', 'active_callback', 'changed_owner'])
async def test_no_continuation_from_ack_or_unproved_or_changed_owner(tmp_path, monkeypatch, blocker):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    add_call(thread)
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/resume':
                calls.append((method, params))
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            return await super().request(method, params, timeout)
    provider.client_factory = Client
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST if blocker not in {'alive', 'unknown_process'} else None)
    original_readback = native.native_turn_readback
    async def pending(client, *args):
        if blocker == 'active_callback':
            client._street_story_active_calls[(THREAD, TURN, 'save-1')] = 1
        await original_readback(client, *args)
        return {'thread': {'id': THREAD, 'turns': []}}
    monkeypatch.setattr(native, 'native_turn_readback', pending)
    provider.timeout = 0.001
    effects = []
    async def save(*args, **kwargs):
        effects.append(True)
        return {'saved_fact_ids': ['one']}
    def guard():
        if blocker == 'changed_owner':
            from street_story.service import ConflictError
            raise ConflictError('selected_object_changed', 'Changed owner')
    # Register an already active callback before the first recovery probe.
    if blocker == 'active_callback':
        from street_story.native_research_tools import attach_research_tools
        def attach(client, callback):
            client = attach_research_tools(client, callback)
            client._street_story_active_calls[(THREAD, TURN, 'save-1')] = 1
            return client
        monkeypatch.setattr(native, 'attach_research_tools', attach)
    expected = Exception if blocker == 'changed_owner' else RetryableProviderError
    kwargs = dict(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=TOOLS, callback=save, guard=guard)
    with pytest.raises(expected):
        await provider.run(**kwargs)
    assert native.orphan_pointer(receipt) is None
    assert not any(method in {'turn/start', 'thread/start'} for method, _ in calls)
    if blocker == 'ack_only':
        assert receipt['orphan_recovery']['phase'] == 'interrupt_intent' and len(effects) == 1
        with pytest.raises(RetryableProviderError):
            await provider.run(**kwargs)
        assert sum(method == 'turn/interrupt' for method, _ in calls) == 2
        assert all(params == {'threadId': THREAD, 'turnId': TURN}
            for method, params in calls if method == 'turn/interrupt')
    else:
        assert not effects
        if blocker != 'changed_owner':
            assert not any(method == 'turn/interrupt' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'ack_only', 'photo_sha256', 'generation', 'control_revision', 'job_id', 'selected_object'])
async def test_closed_orphan_parent_retains_facts_and_rejects_changed_scope(tmp_path, change):
    from test_native_fact_basis import setup as basis_setup
    service, job, reader, fetches, agent, parent, persist = await basis_setup(tmp_path)
    parent['binding']['attempt_id'] = agent['attempt_id']
    context = {'run_id': 'headless-run', 'selected_object': {'candidate_id': 'selected-building'}}
    parent.update(phase='failed', native_terminal_status='interrupted', provider_send_state='response_closed',
        frozen_prompt='Original\nINPUT=' + json.dumps(context), orphan_recovery={
            'contract': transport.ORPHAN_CONTRACT, 'phase': 'terminal', 'lost_owner': LOST,
            'thread_id': parent['thread_id'], 'turn_id': parent['turn_id'], 'call_ids': ['save-1']})
    pointer = native.orphan_pointer(parent)
    before = copy.deepcopy(parent)
    binding = copy.deepcopy(parent['binding'])
    next_context = copy.deepcopy(context)
    if change == 'ack_only':
        parent['orphan_recovery']['phase'] = 'interrupt_intent'
    elif change == 'selected_object':
        next_context['selected_object']['candidate_id'] = 'other-building'
    elif change:
        binding[change] = 'changed'
    persist()
    try:
        with service.store.connection() as db:
            if change:
                with pytest.raises(ValueError, match='orphan_parent_unverified'):
                    native.load_orphan_parent(db, pointer, binding=binding, context=next_context)
            else:
                assert native.load_orphan_parent(db, pointer, binding=binding, context=next_context) == before
        assert len(fetches) == 1
        assert parent['native_tool_calls']['original-read'] == before['native_tool_calls']['original-read']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_save_joins_original_local_effect(tmp_path, monkeypatch):
    import asyncio
    from street_story import native_fact_research as module
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    effects, callbacks = [], []
    def attach(client, callback):
        callbacks.append(callback)
        return client
    original = module.native_turn_readback
    async def readback(*args):
        request = {'threadId': THREAD, 'turnId': TURN, 'callId': 'same-save',
            'tool': 'save_facts', 'arguments': {'facts': []}}
        first, second = await asyncio.gather(callbacks[0](request), callbacks[0](request))
        assert first == second
        return await original(*args)
    async def save(name, args, *, checkpoint_owner):
        effects.append(checkpoint_owner)
        await asyncio.sleep(0)
        return {'saved_fact_ids': ['one']}
    monkeypatch.setattr(module, 'attach_research_tools', attach)
    monkeypatch.setattr(module, 'native_turn_readback', readback)
    await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt,
        prompt=INPUT[0]['text'], schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    assert len(effects) == 1 and effects[0]['call_id'] == 'same-save'
    assert not any(method == 'turn/start' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('deny_quota', [False, True])
async def test_real_atomic_commit_survives_orphan_and_continues_same_job_thread_once(tmp_path, monkeypatch, deny_quota):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from street_story import fact_agents, native_vision
    from street_story.fact_agent_providers import AgentRoute, native_tools
    from street_story.headless_facts import HeadlessFacts
    from street_story.native_fact_contract import RESULT_SCHEMA, source_candidates
    from street_story.research_adapter import ProductResearchAdapter
    from test_native_fact_basis import setup as basis_setup

    service, job, reader, fetches, agent, parent, persist = await basis_setup(tmp_path)
    adapter = ProductResearchAdapter(service, admission=SimpleNamespace())
    story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
    context = fact_agents.native_agent_context(service, story, research['visual_identity'], 'Original goal', 'headless-run')
    parent['binding'].update(attempt_id=agent['attempt_id'])
    prompt = 'Original\nINPUT=' + json.dumps(context, ensure_ascii=False)
    parent.update(phase='unknown', provider_send_state='possibly_sent', frozen_prompt=prompt,
        frozen_schema=copy.deepcopy(RESULT_SCHEMA), provided_tools=native_tools(), native_execution_owner=OWNER)
    parent.pop('native_terminal_status')
    parent['native_tool_calls']['original-read']['completed_at'] = service.store.now() - 1
    packet = {'facts': copy.deepcopy(parent['raw_result']['facts'])}
    parent['native_tool_calls']['save-1'] = {'phase': 'submitted', 'submitted_at': service.store.now(), 'input': {
        'thread_id': parent['thread_id'], 'turn_id': parent['turn_id'], 'name': 'save_facts', 'arguments': packet,
        'execution_contexts': [{'model': native.MODEL, 'turn_id': parent['turn_id'], 'effort': 'medium'}]}}
    persist()
    candidate = source_candidates(packet)['facts'][0]
    candidate['native_checkpoint_call_id'] = 'save-1'
    # Real production writer, including POI projection and atomic replay outcome.
    committed = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
    assert committed['status'] == 'supported', committed
    with service.store.connection() as db:
        observations = db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0]
    fact_before = service.story(job['story_id'])['facts']
    root, old_turn = parent['thread_id'], parent['turn_id']
    path = tmp_path / f'rollout-{root}.jsonl'
    rows = [{'type': 'session_meta', 'payload': {'id': root}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': old_turn}},
        {'type': 'turn_context', 'payload': {'turn_id': old_turn, 'model': native.MODEL, 'effort': 'medium'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': prompt}]}},
        {'type': 'response_item', 'timestamp': '2026-10-14T00:00:00Z', 'payload': {
            'type': 'function_call', 'name': 'save_facts', 'call_id': 'save-1', 'arguments': json.dumps(packet)}}]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    monkeypatch.setattr(native_vision, 'NATIVE_SESSION_ROOT', tmp_path)
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST if owner == OWNER else None)
    calls, sends, admitted = [], [], []
    new_turn = 'recovered-new-turn'
    final = {'facts': [], 'completion': {'research_sufficient': True, 'reason': 'Synthetic completed continuation',
        'remaining_gaps': [], 'stop_basis': 'material_exhausted', 'next_action': None, 'strongest_candidate': None}}
    class Client:
        def _reader_loop(self, *args):
            pass
        def _record_notification(self, *args):
            pass
        def cached_status(self, thread_id):
            return None, None
        def turn_token_usage(self, *args):
            return None
        async def request(self, method, params, timeout=30):
            calls.append((method, copy.deepcopy(params)))
            if method == 'thread/read':
                metadata = {'id': root, 'path': str(path)}
                if params['includeTurns']:
                    turn = native_vision.owned_native_rollout_turn(metadata, params.get('turnId',
                        new_turn if sends else old_turn), [{'type': 'text', 'text': sends[-1] if sends else prompt}])
                    metadata['turns'] = [turn] if turn else []
                return {'thread': metadata}
            if method == 'turn/interrupt':
                assert params == {'threadId': root, 'turnId': old_turn}
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'event_msg', 'payload': {
                        'type': 'turn_aborted', 'turn_id': old_turn}}) + '\n')
                return {}  # ACK alone is never the terminal proof.
            if method == 'thread/resume':
                assert params['threadId'] == root
                return {'thread': {'id': root}, 'model': native.MODEL, 'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/start':
                assert params['threadId'] == root and len(sends) == 0
                assert parent['orphan_recovery']['phase'] == 'terminal'
                sends.append(params['input'][0]['text'])
                new_rows = [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': new_turn}},
                    {'type': 'turn_context', 'payload': {'turn_id': new_turn, 'model': native.MODEL, 'effort': 'medium'}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text', 'text': sends[-1]}]}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                        'content': [{'type': 'output_text', 'text': json.dumps(final)}]}},
                    {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': new_turn}}]
                with path.open('a') as stream:
                    stream.write(''.join(json.dumps(row) + '\n' for row in new_rows))
                return {'turn': {'id': new_turn}}
            raise AssertionError('No other transport or provider operation permitted: ' + method)
    @asynccontextmanager
    async def admission(*args):
        async def before_send(metadata):
            admitted.append(metadata)
        async def finalize(*args):
            pass
        yield SimpleNamespace(before_send=before_send, finalize=finalize)
    provider = native.NativeFactResearch(service, admission=admission, checkpoint=adapter.checkpoint, client_factory=Client)
    async def permission(*args, **kwargs):
        if deny_quota:
            raise RetryableProviderError('native_quota_below_reserve')
        return {'permitted': True}
    provider.permission.ensure = permission
    async def recover_save(name, args, *, checkpoint_owner):
        assert name == 'save_facts' and checkpoint_owner['call_id'] == 'save-1'
        replayed = await service.confirm_agent_fact(job, 'headless-run', candidate, agent)
        assert replayed == committed
        return {'saved_fact_ids': [replayed['fact_id']]}
    try:
        with pytest.raises(RetryableProviderError, match='owned_orphan_terminal'):
            await provider.run(binding=parent['binding'], receipt=parent, prompt=prompt, schema=RESULT_SCHEMA,
                tools=native_tools(), callback=recover_save, guard=lambda: None)
        original_snapshot = copy.deepcopy(parent)
        route = AgentRoute('codex_native', native.MODEL, native.TRANSPORT, provider)
        monkeypatch.setattr(fact_agents, 'configured_routes', lambda adapter: [route])
        plan = {'context': context, 'route_ids': [route.route_id], 'primary_count': 1, 'routes': {route.route_id: {
            'phase': 'failed', 'provider_id': 'codex_native', 'attempt_id': agent['attempt_id'],
            **fact_agents._source_coverage(parent)}}}
        fact_agents._persist_plan(adapter, job, 'headless-run', HeadlessFacts(service),
            story['_fact_research_control_revision'], initial=plan)
        await fact_agents.run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        if deny_quota:
            assert not sends and not admitted
            deny_quota = False
            await fact_agents.run_fact_agents(adapter, job, 'headless-run', 'Original goal')
            assert not sends and not admitted  # Durable retry_at is still authoritative.
            clock = service.store.now()
            monkeypatch.setattr(service.store, 'now', lambda: clock + 7200)
            await fact_agents.run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        assert len(sends) == len(admitted) == 1
        assert sum(method == 'turn/interrupt' for method, _ in calls) == 1
        assert not any(method == 'thread/start' for method, _ in calls)
        # Reopening the same saved work cannot send again or duplicate a fact.
        await fact_agents.run_fact_agents(adapter, job, 'headless-run', 'Original goal')
        assert len(sends) == 1
        with service.store.connection() as db:
            original = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (agent['attempt_id'],)).fetchone()[0])
            assert original == original_snapshot
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == observations
        current = service.story(job['story_id'])['facts']
        assert [(x['fact_id'], x['text'], x['sources']) for x in current] == [(x['fact_id'], x['text'], x['sources']) for x in fact_before]
        assert len(fetches) == 1  # Original fixture fetch only; recovery did not fetch.
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('submitted', [False, True])
async def test_orphan_retention_recovers_same_received_version_without_http_or_quota(tmp_path, monkeypatch, submitted):
    from test_native_received_sources import URL, REF, RESULT
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(provided_tools=[{'name': 'retain_native_source'}], native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = Path(thread['path'])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    args = {'url': URL, 'source_ref': REF}
    rows[-1:-1] = [
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'read-native',
            'input': 'const r=await tools.web__run({open:[{ref_id:"https://archive.example/issue"}]}); text(r)',
            'internal_chat_message_metadata_passthrough': {'turn_id': TURN}}},
        {'timestamp': '2026-10-14T00:00:00Z', 'type': 'response_item', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'read-native', 'id': 'result-native',
            'output': [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'}, {'type': 'input_text', 'text': RESULT}],
            'internal_chat_message_metadata_passthrough': {'turn_id': TURN}}},
        {'timestamp': '2026-10-14T00:00:01Z', 'type': 'response_item', 'payload': {
            'type': 'function_call', 'name': 'retain_native_source', 'call_id': 'retain-one', 'arguments': json.dumps(args)}}]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    if submitted:
        receipt['native_tool_calls'] = {'retain-one': {'phase': 'submitted', 'submitted_at': 1791936001,
            'input': {'thread_id': THREAD, 'turn_id': TURN, 'name': 'retain_native_source', 'arguments': args}}}
    retained = []
    async def retain(name, args, *, checkpoint_owner):
        proof = checkpoint_owner['native_source']
        assert proof['call_id'] == 'read-native' and proof['content'] == 'Historical house article.'
        retained.append(proof['content_sha256'])
        return {'documents': [{'source_version_id': 'same-immutable-version', 'content_sha256': proof['content_sha256']}]}
    async def no_quota(*args, **kwargs):
        raise AssertionError('Attaching existing received content sends no request')
    provider.permission.ensure = no_quota
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=receipt['provided_tools'], callback=retain, guard=lambda: None)
    assert len(retained) == 1 and receipt['phase'] == 'completed'
    assert receipt['native_tool_calls']['retain-one']['result']['documents'][0]['source_version_id'] == 'same-immutable-version'
    assert all(method == 'thread/read' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('crash', ['before_interrupt_send', 'interrupt_response_lost', 'interrupt_response_lost_pending'])
async def test_interrupt_crash_window_reconciles_same_turn_on_repeated_recovery(tmp_path, monkeypatch, crash):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = add_call(thread)
    checkpoint = provider.checkpoint
    failed = False
    async def durable(binding, value):
        nonlocal failed
        await checkpoint(binding, value)
        if crash == 'before_interrupt_send' and not failed and (value.get('orphan_recovery') or {}).get('phase') == 'interrupt_intent':
            failed = True  # Intent was saved, but no RPC was sent.
            raise RuntimeError('synthetic crash after durable interrupt intent')
    provider.checkpoint = durable
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/resume':
                calls.append((method, params))
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/interrupt':
                calls.append((method, params))
                assert params == {'threadId': THREAD, 'turnId': TURN}
                sent = sum(method == 'turn/interrupt' for method, _ in calls)
                if crash != 'interrupt_response_lost_pending' or sent > 1:
                    with path.open('a') as stream:
                        stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': TURN}}) + '\n')
                if crash in {'interrupt_response_lost', 'interrupt_response_lost_pending'} and sent == 1:
                    raise TimeoutError('synthetic lost interrupt response')
                return {}
            return await super().request(method, params, timeout)
    provider.client_factory = Client
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    effects = []
    async def save(*args, **kwargs):
        effects.append(True)
        return {'saved_fact_ids': ['one']}
    kwargs = dict(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    with pytest.raises((RuntimeError, TimeoutError)):
        await provider.run(**kwargs)
    assert receipt['orphan_recovery']['phase'] == 'interrupt_intent'
    assert native.orphan_pointer(receipt) is None
    assert receipt['orphan_recovery']['interrupt_rpc_status'] == (
        'possibly_sent' if crash == 'before_interrupt_send' else 'outcome_unknown')
    provider.timeout = 0.01
    pointers = []
    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='owned_orphan_terminal'):
            await provider.run(**kwargs)
        pointers.append(native.orphan_pointer(receipt))
    assert pointers[0] is not None and pointers[0] == pointers[1]
    assert len(effects) == 1
    assert sum(method == 'turn/interrupt' for method, _ in calls) == (2 if crash == 'interrupt_response_lost_pending' else 1)
    assert not any(method in {'turn/start', 'thread/start'} for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['closed', 'pending', 'advanced', 'foreign_parent', 'wrong_model', 'unrecorded', 'nested_delegation'])
async def test_completed_owned_sol_child_allows_recovery_but_unresolved_or_foreign_child_does_not(tmp_path, monkeypatch, outcome):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER,
        native_threads={THREAD: {'model_id': native.MODEL, 'turn_id': TURN},
            'owned-child': {'model_id': 'gpt-6.1-sol', 'turn_id': 'child-turn'}},
        native_events=[{'type': 'subAgentActivity', 'kind': 'started', 'id': 'spawn-child', 'agentThreadId': 'owned-child'}])
    if outcome == 'unrecorded':
        receipt['native_threads'].pop('owned-child')
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = add_call(thread)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows.insert(-1, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'spawn_agent',
        'call_id': 'spawn-child', 'arguments': json.dumps({'model': 'gpt-6.1-sol', 'fork_turns': 'none',
            'task_name': 'bounded-help', 'message': 'Synthetic owned child evidence task'})}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    child_path = tmp_path / 'rollout-owned-child.jsonl'
    child_rows = [{'type': 'session_meta', 'payload': {'id': 'owned-child'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'child-turn'}},
        {'type': 'turn_context', 'payload': {'turn_id': 'child-turn',
            'model': 'foreign-model' if outcome == 'wrong_model' else 'gpt-6.1-sol'}}]
    if outcome != 'pending':
        child_rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'child-turn'}})
    if outcome == 'nested_delegation':
        child_rows.append({'type': 'response_item', 'payload': {'type': 'function_call',
            'name': 'spawn_agent', 'call_id': 'forbidden-grandchild', 'arguments': json.dumps({})}})
    if outcome == 'advanced':
        child_rows.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'other-child-turn'}})
    child_path.write_text(''.join(json.dumps(row) + '\n' for row in child_rows))
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/read' and params['threadId'] == 'owned-child':
                return {'thread': {'id': 'owned-child', 'path': str(child_path), 'source': {'subAgent': {
                    'thread_spawn': {'parent_thread_id': 'foreign-root' if outcome == 'foreign_parent' else THREAD, 'depth': 1}}}}}
            if method == 'thread/resume':
                calls.append((method, params))
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/interrupt':
                with path.open('a') as stream:
                    stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': TURN}}) + '\n')
            return await super().request(method, params, timeout)
    provider.client_factory = Client
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    effects = []
    async def save(*args, **kwargs):
        effects.append(True)
        return {'saved_fact_ids': ['partial-fact']}
    kwargs = dict(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    expected = RetryableProviderError if outcome in {'closed', 'pending', 'advanced'} else ValueError
    with pytest.raises(expected):
        await provider.run(**kwargs)
    assert effects == [True]  # A child uncertainty does not discard local partial value.
    assert sum(method == 'turn/interrupt' for method, _ in calls) == int(outcome == 'closed')
    assert (native.orphan_pointer(receipt) is not None) == (outcome == 'closed')
    if outcome == 'closed':
        assert receipt['orphan_recovery']['closed_children'] == {
            'owned-child': {'turn_id': 'child-turn', 'terminal_event': 'task_complete'}}
    else:
        assert receipt['phase'] == 'unknown'
    assert not any(method in {'turn/start', 'thread/start'} for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', [False, True])
async def test_legacy_missing_process_proof_uses_only_exact_terminal_readback(tmp_path, monkeypatch, terminal):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    add_call(thread, terminal=terminal)
    provider.timeout = 0.01
    effects = []
    async def save(*args, **kwargs):
        effects.append(True)
        return {'saved_fact_ids': ['legacy-partial']}
    kwargs = dict(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='owned_orphan_terminal' if terminal else 'original_pending'):
            await provider.run(**kwargs)
    assert not receipt.get('native_execution_owner')  # No invented historical process.
    assert not any(method != 'thread/read' for method, _ in calls)
    if terminal:
        assert effects == [True]
        assert receipt['orphan_recovery']['lost_owner'] is None
        assert receipt['orphan_recovery']['terminal_readback'] == 'interrupted'
        assert native.orphan_pointer(receipt) is not None
    else:
        assert not effects and native.orphan_pointer(receipt) is None
        assert receipt['orphan_recovery_blocker'] == 'native_orphan_owner_process_unverified'


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong', ['thread', 'input'])
async def test_legacy_terminal_proof_cannot_be_borrowed_from_another_owner(tmp_path, monkeypatch, wrong):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(provided_tools=TOOLS)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    add_call(thread, terminal=True)
    async def foreign_readback(*args):
        return {'thread': {'id': 'foreign-thread' if wrong == 'thread' else THREAD,
            'turns': [{'id': TURN, 'status': 'interrupted', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'foreign input'}]}]}]}}
    monkeypatch.setattr(native, 'native_turn_readback', foreign_readback)
    async def no_effects(*args, **kwargs):
        raise AssertionError('Unverified terminal proof cannot authorize local writes')
    with pytest.raises(ValueError, match='native_orphan_'):
        await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
            schema={'type': 'object'}, tools=TOOLS, callback=no_effects, guard=lambda: None)
    assert native.orphan_pointer(receipt) is None
    assert not any(method != 'thread/read' for method, _ in calls)


@pytest.mark.asyncio
async def test_terminal_marker_waits_for_successful_execution_graph_validation(tmp_path, monkeypatch):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    add_call(thread, terminal=True)
    execution_readback = provider._execution_readback
    async def invalid_graph(client, value):
        if value.get('native_terminal_status') == 'interrupted':
            raise ValueError('native_research_explicit_delegation_unverified')
        await execution_readback(client, value)
    provider._execution_readback = invalid_graph
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    async def save(*args, **kwargs):
        return {'saved_fact_ids': ['retained-partial']}
    with pytest.raises(ValueError, match='delegation_unverified'):
        await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
            schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    assert receipt['native_tool_calls']['save-1']['phase'] == 'completed'
    assert native.orphan_pointer(receipt) is None
    assert receipt['orphan_recovery']['phase'] != 'terminal'
    assert not any(method != 'thread/read' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('callback_state', ['active', 'replied'])
@pytest.mark.parametrize('delivered_at', ['resume', 'intent_saved'])
async def test_callback_redelivered_after_resume_invalidates_orphan_before_interrupt(
        tmp_path, monkeypatch, callback_state, delivered_at):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    receipt.update(binding={'attempt_id': 'original-owned'}, provided_tools=TOOLS, native_execution_owner=OWNER)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = add_call(thread)
    delivered = False
    def redeliver(client):
        nonlocal delivered
        if delivered:
            return
        delivered = True
        if callback_state == 'active':
            client._street_story_active_calls[(THREAD, TURN, 'save-1')] = 1
        else:
            with path.open('a') as stream:
                stream.write(json.dumps({'type': 'response_item', 'payload': {
                    'type': 'function_call_output', 'call_id': 'save-1', 'output': 'original callback replied'}}) + '\n')
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/resume':
                calls.append((method, params))
                if delivered_at == 'resume':
                    redeliver(self)
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/interrupt':
                raise AssertionError('Resume revived the callback; the old orphan proof is stale')
            return await super().request(method, params, timeout)
    provider.client_factory = Client
    checkpoint = provider.checkpoint
    async def save_intent(binding, value):
        await checkpoint(binding, value)
        if delivered_at == 'intent_saved' and (value.get('orphan_recovery') or {}).get('phase') == 'interrupt_intent':
            redeliver(provider.clients['original-owned'])
    provider.checkpoint = save_intent
    monkeypatch.setattr(native, 'lost_execution_owner', lambda owner: LOST)
    provider.timeout = 0.01
    effects = []
    async def save(*args, **kwargs):
        effects.append(True)
        return {'saved_fact_ids': ['same-partial-fact']}
    with pytest.raises(RetryableProviderError, match='original_pending'):
        await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
            schema={'type': 'object'}, tools=TOOLS, callback=save, guard=lambda: None)
    assert delivered and effects == [True]
    assert receipt['native_tool_calls']['save-1']['phase'] == 'completed'
    assert receipt['orphan_recovery']['interrupt_rpc_status'] == 'not_sent'
    assert native.orphan_pointer(receipt) is None
    assert not any(method in {'turn/start', 'thread/start', 'turn/interrupt'} for method, _ in calls)
