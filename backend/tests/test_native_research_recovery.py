import copy
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.native_fact_research import NativeFactResearch
from street_story.service import ConflictError
from test_native_rollout_readback import INPUT, THREAD, TURN, journal


def setup(tmp_path, monkeypatch, *, receipt=None):
    thread, rows = journal(tmp_path, monkeypatch)
    def count(total):
        return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
            'input_tokens': total-19 if total else 0, 'cached_input_tokens': 0,
            'output_tokens': 19 if total else 0, 'reasoning_output_tokens': 0, 'total_tokens': total}}}}
    rows.insert(3, count(0))
    rows.insert(-1, count(119))
    from pathlib import Path
    Path(thread['path']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    calls, saved = [], []
    class NativeAppServerError(RuntimeError):
        code = -32601
    class Client:
        def _reader_loop(self, *args):
            pass

        def _record_notification(self, *args):
            pass

        async def request(self, method, params, timeout=30):
            calls.append((method, params))
            if method == 'thread/read':
                assert params['threadId'] == THREAD
                if params['includeTurns']:
                    raise NativeAppServerError('list_turns is not supported yet')
                return {'thread': thread}
            if method == 'turn/interrupt':
                return {}
            if method == 'config/read':
                return {'config': {}}
            raise AssertionError('No new native operation is authorized')

        def cached_status(self, thread_id):
            return None, {'id': TURN, 'status': 'completed'}

        def turn_token_usage(self, thread_id, turn_id):
            return {'totalTokens': 119}

    @asynccontextmanager
    async def no_new_admission(*args):
        raise AssertionError('Original UNKNOWN must only be observed')
        yield

    async def checkpoint(binding, value):
        saved.append(copy.deepcopy(value))
    service = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), store=SimpleNamespace(now=time.time))
    provider = NativeFactResearch(service, admission=no_new_admission,
        checkpoint=checkpoint, client_factory=Client)
    value = receipt if receipt is not None else {'phase': 'unknown', 'thread_id': THREAD,
        'turn_id': TURN, 'provider_send_state': 'possibly_sent'}
    return provider, value, calls, saved


async def invoke(provider, receipt, *, guard=lambda: None):
    async def no_tools(*args):
        raise AssertionError('Recovery must not search or fetch')
    return await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt,
        prompt=INPUT[0]['text'], schema={'type': 'object'}, tools=[], callback=no_tools, guard=guard)


@pytest.mark.asyncio
async def test_original_unknown_reads_closed_owned_turn_without_send_or_fresh_admission(tmp_path, monkeypatch):
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    result = await invoke(provider, receipt)
    assert result['text'] == '{"status":"match"}'
    assert receipt['phase'] == 'completed' and receipt['usage']['total_tokens'] == 119
    assert all(method == 'thread/read' for method, _ in calls)
    assert saved[-1]['turn_id'] == TURN


@pytest.mark.asyncio
@pytest.mark.parametrize('readable', [True, False])
async def test_native_content_retention_uses_owned_journal_without_quota_or_http(tmp_path, monkeypatch, readable):
    from pathlib import Path
    from street_story import native_fact_research as native
    from street_story.fact_agent_providers import native_tools
    from test_native_received_sources import URL, REF, RESULT
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch)
    thread = (await provider.client_factory().request('thread/read', {'threadId': THREAD, 'includeTurns': False}))['thread']
    path = Path(thread['path'])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-2:-2] = [
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'native-read',
            'input': 'const r=await tools.web__run({open:[{ref_id:"https://archive.example/issue"}]}); text(r)',
            'internal_chat_message_metadata_passthrough': {'turn_id': TURN}}},
        {'timestamp': '2026-10-14T00:00:00Z', 'type': 'response_item', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'native-read', 'id': 'native-result',
            'output': [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'},
                {'type': 'input_text', 'text': RESULT if readable else f'Archive ({URL})\n\ue200cite\ue202{REF}\ue201 summary only'}],
            'internal_chat_message_metadata_passthrough': {'turn_id': TURN}}}]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    callbacks, retained = [], []
    def attach(client, callback):
        callbacks.append(callback)
        return client
    async def no_quota(*args, **kwargs):
        raise AssertionError('Retention is local; existing received content must survive quota loss')
    async def retain(name, args, *, checkpoint_owner):
        assert readable and name == 'retain_native_source'
        proof = checkpoint_owner['native_source']
        assert proof['call_id'] == 'native-read' and proof['content'] == 'Historical house article.'
        retained.append(proof)
        return {'documents': [{'source_version_id': 'existing-writer-id'}]}
    original = native.native_turn_readback
    async def readback(*args):
        request = {'threadId': THREAD, 'turnId': TURN, 'callId': 'retain-call',
            'tool': 'retain_native_source', 'arguments': {'url': URL, 'source_ref': REF}}
        result = await callbacks[0](request)
        assert await callbacks[0](request) == result
        if readable:
            assert result['documents'][0]['source_version_id'] == 'existing-writer-id'
        else:
            assert not result['documents']
            assert result['read_attempts'][0]['error_code'] == 'native_source_document_content_not_returned'
        return await original(*args)
    provider.permission.ensure = no_quota
    monkeypatch.setattr(native, 'attach_research_tools', attach)
    monkeypatch.setattr(native, 'native_turn_readback', readback)
    await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=native_tools(), callback=retain, guard=lambda: None)
    assert len(retained) == int(readable)
    assert all(method == 'thread/read' for method, _ in calls)
    assert saved[-1]['native_tool_calls']['retain-call']['phase'] == 'completed'


@pytest.mark.asyncio
async def test_unaddressable_send_stays_unknown_and_never_starts_replacement(tmp_path, monkeypatch):
    provider, receipt, calls, _ = setup(tmp_path, monkeypatch,
        receipt={'phase': 'unknown', 'thread_id': THREAD, 'provider_send_state': 'possibly_sent'})
    with pytest.raises(RetryableProviderError, match='original_turn_unknown'):
        await invoke(provider, receipt)
    assert receipt['phase'] == 'unknown' and receipt['provider_send_state'] == 'possibly_sent'
    assert not calls


@pytest.mark.asyncio
async def test_owner_stop_interrupts_original_turn_but_does_not_fabricate_abort_ack(tmp_path, monkeypatch):
    provider, receipt, calls, _ = setup(tmp_path, monkeypatch)
    def stopped():
        raise ConflictError('selected_object_changed', 'Owner selected B or stopped A')
    with pytest.raises(ConflictError):
        await invoke(provider, receipt, guard=stopped)
    assert calls == [('turn/interrupt', {'threadId': THREAD, 'turnId': TURN})]
    assert receipt['abort_requested'] and receipt['phase'] == 'unknown'
    assert 'abort_acknowledged' not in receipt


def test_only_direct_owned_sol_child_can_use_parent_source_tools():
    NativeFactResearch._verify_thread({'id': 'child', 'source': {'subAgent': {
        'thread_spawn': {'parent_thread_id': 'root', 'depth': 1}}}}, 'root')
    for source in [None, 'external', {'subAgent': {'thread_spawn': {'parent_thread_id': 'other', 'depth': 1}}},
            {'subAgent': {'thread_spawn': {'parent_thread_id': 'root', 'depth': 2}}}]:
        with pytest.raises(ValueError, match='child_parent_unverified'):
            NativeFactResearch._verify_thread({'id': 'child', 'source': source}, 'root')


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_local_reply', [False, True])
async def test_owned_save_callback_recovers_same_call_without_inference_or_quota_discard(tmp_path, monkeypatch, lost_local_reply):
    from street_story import native_fact_research as native
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch, receipt={'phase': 'created'})
    callbacks, effects, quota_reads = [], [], []
    base = provider.client_factory
    class Client(base):
        async def request(self, method, params, timeout=30):
            if method == 'thread/start':
                calls.append((method, copy.deepcopy(params)))
                return {'thread': {'id': THREAD}, 'model': 'gpt-6-luna',
                    'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/start':
                calls.append((method, copy.deepcopy(params)))
                return {'turn': {'id': TURN}}
            return await super().request(method, params, timeout)
    @asynccontextmanager
    async def admission(*args):
        class Lease:
            async def before_send(self, metadata):
                pass
            async def finalize(self, *args):
                pass
        yield Lease()
    async def quota(*args, **kwargs):
        quota_reads.append(True)
        if len(quota_reads) > 1:
            raise RetryableProviderError('native_quota_below_reserve')
        return {'remaining_percent': 50}
    def attach(client, callback):
        callbacks.append(callback)
        return client
    local = {}
    async def persist(name, args, *, checkpoint_owner):
        assert name == 'save_facts' and args == {'facts': []}
        assert checkpoint_owner == {'call_id': 'save-current', 'thread_id': THREAD, 'turn_id': TURN}
        if not local:
            effects.append('existing atomic ledger outcome')
            local.update(saved_fact_ids=['already-committed'], partial=True)
            if lost_local_reply:
                raise RuntimeError('controlled loss after local commit')
        return copy.deepcopy(local)
    async def readback(client, tid, turn, input_parts):
        request = {'threadId': THREAD, 'turnId': TURN, 'callId': 'save-current',
            'tool': 'save_facts', 'arguments': {'facts': []}}
        if lost_local_reply:
            with pytest.raises(RuntimeError, match='after local commit'):
                await callbacks[0](request)
            assert receipt['native_tool_calls']['save-current']['phase'] == 'submitted'
        result = await callbacks[0](request)
        assert result['saved_fact_ids'] == ['already-committed']
        assert await callbacks[0](request) == result
        with pytest.raises(ValueError, match='call_changed'):
            await callbacks[0]({**request, 'arguments': {'facts': ['different payload']}})
        return {'thread': {'id': THREAD, 'turns': [{'id': TURN, 'status': 'completed',
            'items': [{'type': 'userMessage', 'content': input_parts},
                {'type': 'agentMessage', 'text': '{"status":"match"}'}]}]}}
    provider.client_factory, provider.admission = Client, admission
    provider.permission.ensure = quota
    monkeypatch.setattr(native, 'attach_research_tools', attach)
    monkeypatch.setattr(native, 'native_turn_readback', readback)
    from street_story.fact_agent_providers import native_tools
    await provider.run(binding={'attempt_id': 'original-owned'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=native_tools(), callback=persist, guard=lambda: None)
    assert len(effects) == 1 and len(quota_reads) == 1
    assert sum(method == 'turn/start' for method, _ in calls) == 1
    assert saved[-1]['native_tool_calls']['save-current']['phase'] == 'completed'


def test_native_v2_empty_annotation_projection_preserves_exact_input_binding():
    projected = [{**INPUT[0], 'text_elements': []}]
    assert NativeFactResearch._input_matches(projected, INPUT)
    assert not NativeFactResearch._input_matches([{**projected[0], 'text': 'Another operation'}], INPUT)
    assert not NativeFactResearch._input_matches([{**projected[0], 'text_elements': [{'label': 'foreign-file'}]}], INPUT)
    assert not NativeFactResearch._input_matches([*projected, projected[0]], INPUT)


def test_turn_usage_is_cumulative_delta_not_last_response_or_next_turn(tmp_path, monkeypatch):
    from pathlib import Path
    thread, rows = journal(tmp_path, monkeypatch)
    def count(total):
        return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
            'input_tokens': total-20, 'cached_input_tokens': 20, 'output_tokens': 20,
            'reasoning_output_tokens': 0, 'total_tokens': total}}}}
    rows.insert(3, count(100))
    rows.insert(-1, count(550))
    rows.insert(-1, count(550))  # A repeated notification is not extra usage.
    rows += [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'next'}}, count(9999)]
    Path(thread['path']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    usage = NativeFactResearch._journal_turn_usage(thread, TURN)
    assert usage['totalTokens'] == 450 and usage['inputTokens'] == 450
    rows = [row for row in rows if not (row.get('payload') or {}).get('type') == 'task_complete']
    Path(thread['path']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert NativeFactResearch._journal_turn_usage(thread, TURN) is None


@pytest.mark.asyncio
async def test_original_completed_turn_with_local_readback_failure_is_observed_without_send(tmp_path, monkeypatch):
    provider, receipt, calls, _ = setup(tmp_path, monkeypatch,
        receipt={'phase': 'failed', 'thread_id': THREAD, 'turn_id': TURN,
            'native_terminal_status': 'completed', 'provider_send_state': 'response_closed',
            'route_failure': {'code': 'native_research_input_unverified'}})
    await invoke(provider, receipt)
    assert receipt['phase'] == 'completed'
    assert receipt['closed_readback_recovery']['original_phase'] == 'failed'
    assert receipt['closed_readback_recovery']['new_inference_sent'] is False
    assert all(method == 'thread/read' for method, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['complete', 'stop', 'quota'])
async def test_owned_native_tools_continue_past_24_with_stop_quota_and_no_resend(tmp_path, monkeypatch, boundary):
    """Actual tool-owner callback, with a controlled provider turn and journal."""
    from street_story import native_fact_research as native
    provider, receipt, calls, saved = setup(tmp_path, monkeypatch, receipt={'phase': 'created'})
    original_factory = provider.client_factory
    effects, callbacks = [], []
    stopped = False

    class Client(original_factory):
        async def request(self, method, params, timeout=30):
            if method == 'thread/start':
                calls.append((method, copy.deepcopy(params)))
                assert params['config']['web_search'] == 'live'
                assert params['config']['features.shell_tool'] is False
                assert params['config']['features.apps'] is False
                assert params['sandbox'] == 'read-only'
                return {'thread': {'id': THREAD}, 'model': native.MODEL,
                        'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
            if method == 'turn/start':
                calls.append((method, copy.deepcopy(params)))
                assert params['sandboxPolicy'] == {'type': 'readOnly', 'networkAccess': False}
                return {'turn': {'id': TURN}}
            return await super().request(method, params, timeout)

    def attach(client, callback):
        callbacks.append(callback)
        return client

    @asynccontextmanager
    async def admission(*args):
        async def finalize(*args):
            pass
        yield SimpleNamespace(finalize=finalize, before_send=finalize)

    async def permission(*args, **kwargs):
        if boundary == 'quota' and len(effects) >= 25:
            raise RetryableProviderError('resource_daily_budget')
        return {'permitted': True}

    original_readback = native.native_turn_readback
    first = True
    async def readback(*args):
        nonlocal first, stopped
        if first:
            first = False
            for number in range(26):
                if boundary == 'stop' and number == 25:
                    stopped = True
                value = await callbacks[0]({'threadId': THREAD, 'turnId': TURN,
                    'callId': f'owned-{number}', 'tool': 'websearch', 'arguments': {'query': f'gap {number}'}})
                assert value['research_resources']['public_tool_call_limit'] is None
            # Duplicate delivery joins the exact completed owned call.
            replay = await callbacks[0]({'threadId': THREAD, 'turnId': TURN,
                'callId': 'owned-25', 'tool': 'websearch', 'arguments': {'query': 'gap 25'}})
            assert replay == receipt['native_tool_calls']['owned-25']['result']
        return await original_readback(*args)

    def guard():
        if stopped:
            raise ConflictError('fact_research_cancelled', 'Owner stopped this generation')

    async def tool(name, args):
        effects.append(args['query'])
        return {'sources': [{'url': 'https://example.org/' + str(len(effects))}]}

    provider.client_factory = Client
    provider.admission = admission
    monkeypatch.setattr(provider.permission, 'ensure', permission)
    monkeypatch.setattr(native, 'attach_research_tools', attach)
    monkeypatch.setattr(native, 'native_turn_readback', readback)
    kwargs = dict(binding={'attempt_id': 'owned-beyond-24'}, receipt=receipt, prompt=INPUT[0]['text'],
        schema={'type': 'object'}, tools=[], callback=tool, guard=guard)
    if boundary == 'complete':
        await provider.run(**kwargs)
        assert effects == [f'gap {number}' for number in range(26)]
        assert saved[-1]['phase'] == 'completed'
    else:
        expected = ConflictError if boundary == 'stop' else RetryableProviderError
        with pytest.raises(expected):
            await provider.run(**kwargs)
        assert effects == [f'gap {number}' for number in range(25)]
        assert receipt['phase'] == 'unknown'
        assert any(method == 'turn/interrupt' for method, _ in calls) == (boundary == 'stop')
    assert sum(method == 'thread/start' for method, _ in calls) == 1
    assert sum(method == 'turn/start' for method, _ in calls) == 1


@pytest.mark.asyncio
async def test_native_continuation_resumes_same_thread_and_accounts_only_new_turn(tmp_path, monkeypatch):
    from pathlib import Path
    from street_story import native_fact_research as native
    provider, original, calls, saved = setup(tmp_path, monkeypatch)
    base = provider.client_factory
    next_turn = 'new-owned-continuation'
    prompt = 'Continue the concrete historical query in this family'
    accounting = []
    class Client(base):
        instances = 0
        def __init__(self):
            Client.instances += 1
        async def request(self, method, params, timeout=30):
            if method == 'thread/resume':
                calls.append((method,copy.deepcopy(params)))
                assert params['threadId']==THREAD and params['config']['web_search']=='live'
                return {'thread':{'id':THREAD},'model':native.MODEL,'approvalPolicy':'never','sandbox':{'type':'readOnly'}}
            if method == 'turn/start':
                calls.append((method,copy.deepcopy(params)))
                assert params['threadId']==THREAD and params['input']==[{'type':'text','text':prompt}]
                thread = (await super().request('thread/read',{'threadId':THREAD,'includeTurns':False}))['thread']
                rows=[{'type':'event_msg','payload':{'type':'task_started','turn_id':next_turn}},
                    {'type':'turn_context','payload':{'turn_id':next_turn,'model':native.MODEL}},
                    {'type':'response_item','payload':{'type':'message','role':'user',
                        'content':[{'type':'input_text','text':prompt}]}},
                    {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer',
                        'content':[{'type':'output_text','text':'{"status":"continued"}'}]}},
                    {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{
                        'input_tokens':220,'cached_input_tokens':0,'output_tokens':30,'reasoning_output_tokens':0,'total_tokens':250}}}},
                    {'type':'event_msg','payload':{'type':'task_complete','turn_id':next_turn}}]
                with Path(thread['path']).open('a') as f:
                    f.write(''.join(json.dumps(row)+'\n' for row in rows))
                return {'turn':{'id':next_turn}}
            return await super().request(method,params,timeout)
        def cached_status(self, thread_id):
            return None, {'id':next_turn,'status':'completed'}
    @asynccontextmanager
    async def admission(*args):
        async def finalize(metadata,state):
            accounting.append((metadata,state))
        async def before_send(*args):
            pass
        yield SimpleNamespace(finalize=finalize,before_send=before_send)
    async def permission(*args,**kwargs):
        return {'permitted':True}
    provider.client_factory=Client
    await invoke(provider, original)
    receipt = {'phase':'created', 'thread_id':THREAD, 'native_threads':copy.deepcopy(original['native_threads']),
        'native_family':{'parent_attempt_id':'original-owned','parent_turn_id':TURN,'prior_family_total_tokens':119},
        'applied_controls':{'profile_overrides':{'web_search':'live','features.shell_tool':False}}}
    provider.admission=admission
    monkeypatch.setattr(provider.permission,'ensure',permission)
    async def unused(*args):
        raise AssertionError('No new tool effects for this controlled readback')
    result=await provider.run(binding={'attempt_id':'continued-owned'},receipt=receipt,prompt=prompt,
        schema={'type':'object'},tools=[],callback=unused,guard=lambda:None)
    assert result['text']=='{"status":"continued"}'
    assert receipt['thread_id']==THREAD and receipt['turn_id']==next_turn
    assert not any(method=='thread/start' for method,_ in calls)
    assert Client.instances==1 and list(provider.clients)==['continued-owned']
    assert sum(method=='thread/resume' for method,_ in calls)==1
    assert sum(method=='turn/start' for method,_ in calls)==1
    assert receipt['usage']['family_total_tokens']==250 and receipt['usage']['total_tokens']==131
    assert accounting[0][0]['actual_total_tokens']==131
    assert saved[-1]['phase']=='completed'


@pytest.mark.asyncio
async def test_adapter_recovers_only_addressed_completed_native_projection_failure(tmp_path):
    from street_story.native_fact_research import CONTRACT
    from street_story.research_adapter import ProductResearchAdapter
    from test_research_control import fixture
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = service
    story = {'id': sid, 'photo_sha256': photo}
    binding, _ = adapter.attempt(story, 'facts_agent_native', 'same-frozen-unit')
    receipt = {'binding': binding, 'phase': 'failed', 'provider_id': 'codex_native',
        'model_id': 'gpt-6-luna', 'native_contract': CONTRACT,
        'thread_id': THREAD, 'turn_id': TURN, 'native_terminal_status': 'completed',
        'provider_send_state': 'response_closed', 'frozen_prompt': 'original', 'frozen_schema': {},
        'route_failure': {'code': 'native_research_input_unverified'}}
    adapter._checkpoint_receipt(binding, receipt)
    resumed, saved = adapter.attempt(story, 'facts_agent_native', 'same-frozen-unit')
    assert saved is None and resumed['attempt_id'] == binding['attempt_id']
    assert resumed['thread_id'] == THREAD and resumed['turn_id'] == TURN
    for key, value in [('native_terminal_status', 'failed'), ('turn_id', None),
            ('provider_id', 'opencode'), ('route_failure', {'code': 'malformed_response'})]:
        changed = {**receipt, key: value}
        adapter._checkpoint_receipt(binding, changed)
        resumed, saved = adapter.attempt(story, 'facts_agent_native', 'same-frozen-unit')
        assert resumed is None and saved == changed
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM research_provider_attempts WHERE story_id=?', (sid,)).fetchone()[0] == 1
