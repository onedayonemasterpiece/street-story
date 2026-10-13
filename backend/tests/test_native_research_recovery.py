import copy
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.native_fact_research import NativeFactResearch
from street_story.service import ConflictError
from test_native_rollout_readback import INPUT, THREAD, TURN, journal


def setup(tmp_path, monkeypatch, *, receipt=None):
    thread, _ = journal(tmp_path, monkeypatch)
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
