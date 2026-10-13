import json
from pathlib import Path

import pytest

from street_story import native_vision


THREAD, TURN = 'owned-thread', 'owned-turn'
INPUT = [{'type': 'text', 'text': 'Original owned request'}]


def journal(tmp_path, monkeypatch, *, model='gpt-6-luna', complete=True):
    monkeypatch.setattr(native_vision, 'NATIVE_SESSION_ROOT', tmp_path)
    path = tmp_path / f'rollout-{THREAD}.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': THREAD}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'previous'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'Wrong previous result'}]}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': TURN}},
        {'type': 'turn_context', 'payload': {'turn_id': TURN, 'model': model}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': INPUT[0]['text']}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': '{"status":"match"}'}]}},
    ]
    if complete:
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': TURN}})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return {'id': THREAD, 'path': str(path)}, rows


def test_read_original_completed_turn_without_borrowing_previous_result(tmp_path, monkeypatch):
    thread, _ = journal(tmp_path, monkeypatch)
    result = native_vision.owned_native_rollout_turn(thread, TURN, INPUT)
    assert result['id'] == TURN and result['status'] == 'completed'
    assert result['items'][0]['content'] == INPUT
    assert result['items'][1]['text'] == '{"status":"match"}'


@pytest.mark.parametrize('change', ['input', 'thread', 'model', 'missing_context', 'duplicate_input'])
def test_other_input_thread_or_model_never_becomes_owned_proof(tmp_path, monkeypatch, change):
    thread, rows = journal(tmp_path, monkeypatch)
    if change == 'input':
        rows[5]['payload']['content'][0]['text'] = 'A different command'
    elif change == 'thread':
        rows[0]['payload']['id'] = 'foreign-thread'
    elif change == 'model':
        rows[4]['payload']['model'] = 'gpt-6.1-sol'
    elif change == 'missing_context':
        rows.pop(4)
    else:
        rows.insert(6, rows[5])
    Path(thread['path']).write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(ValueError, match='native_rollout_'):
        native_vision.owned_native_rollout_turn(thread, TURN, INPUT)


def test_pending_turn_stays_unknown_even_if_partial_text_exists(tmp_path, monkeypatch):
    thread, _ = journal(tmp_path, monkeypatch, complete=False)
    assert native_vision.owned_native_rollout_turn(thread, TURN, INPUT) is None


def test_forbidden_tool_activity_is_preserved_for_visual_policy(tmp_path, monkeypatch):
    thread, rows = journal(tmp_path, monkeypatch)
    rows.insert(6, {'type': 'response_item', 'payload': {'type': 'function_call',
        'name': 'shell', 'call_id': 'unauthorized'}})
    Path(thread['path']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    result = native_vision.owned_native_rollout_turn(thread, TURN, INPUT)
    assert any(item.get('type') == 'nativeToolCall' and item['name'] == 'shell' for item in result['items'])


def test_later_steering_does_not_match_original_immutable_input(tmp_path, monkeypatch):
    thread, rows = journal(tmp_path, monkeypatch)
    rows.insert(6, {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': 'Change the requested operation'}]}})
    Path(thread['path']).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with pytest.raises(ValueError, match='input_steered'):
        native_vision.owned_native_rollout_turn(thread, TURN, INPUT)


def test_symlink_or_outside_session_path_is_rejected(tmp_path, monkeypatch):
    thread, _ = journal(tmp_path, monkeypatch)
    link = tmp_path / 'other' / f'rollout-{THREAD}.jsonl'
    link.parent.mkdir()
    link.symlink_to(thread['path'])
    with pytest.raises(ValueError, match='path_unverified'):
        native_vision.owned_native_rollout_turn({**thread, 'path': str(link)}, TURN, INPUT)


@pytest.mark.asyncio
async def test_paginated_reader_fallback_only_reads_original_journal(tmp_path, monkeypatch):
    thread, _ = journal(tmp_path, monkeypatch)
    calls = []
    class NativeAppServerError(RuntimeError):
        code = -32601
    class Client:
        async def request(self, method, params, timeout):
            calls.append((method, params))
            assert method == 'thread/read' and params['threadId'] == THREAD
            if params['includeTurns']:
                raise NativeAppServerError('list_turns is not supported yet')
            return {'thread': thread}
    result = await native_vision.native_turn_readback(Client(), THREAD, TURN, INPUT)
    assert result['thread']['turns'][0]['status'] == 'completed'
    assert len(calls) == 2 and result['readback_transport'] == 'owned_native_rollout_original_turn'


@pytest.mark.asyncio
async def test_unrelated_rpc_rejection_does_not_enable_local_fallback():
    class NativeAppServerError(RuntimeError):
        code = -32601
    class Client:
        async def request(self, method, params, timeout):
            raise NativeAppServerError('unrelated method error')
    with pytest.raises(NativeAppServerError):
        await native_vision.native_turn_readback(Client(), THREAD, TURN, INPUT)
