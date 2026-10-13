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
