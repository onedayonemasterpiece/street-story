import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

from street_story.native_research_tools import ResearchToolStream, attach_research_tools, ResearchToolArgumentsInvalid


def test_only_dynamic_requests_leave_original_platform_reader():
    messages = [{'id': 7, 'method': 'item/tool/call', 'params': {}},
        {'id': 8, 'method': 'item/commandExecution/requestApproval', 'params': {}},
        {'method': 'turn/completed', 'params': {}}, {'id': 9, 'result': {}}]
    calls = []
    lines = [json.dumps(message)+'\n' for message in messages]
    stream = ResearchToolStream(io.StringIO(''.join(lines)), calls.append)
    assert list(stream) == lines[1:]
    assert calls == [messages[0]]


@pytest.mark.asyncio
async def test_callback_wait_does_not_block_drain_and_keeps_original_process():
    entered, release, replied = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_process = type('Process', (), {})()
    request = {'id': 7, 'method': 'item/tool/call', 'params': {
        'threadId': 'owner', 'turnId': 'turn', 'callId': 'read', 'tool': 'webfetch', 'arguments': {'url': 'https://example.org'}}}
    original_process.stdout = io.StringIO(json.dumps(request)+'\n'+json.dumps({'method': 'turn/started'})+'\n')
    original_stream = original_process.stdout
    drained = threading.Event()
    notifications, replies = [], []
    loop = asyncio.get_running_loop()

    class Client:
        def _reader_loop(self, process, generation):
            notifications.extend(json.loads(line) for line in process.stdout)
            drained.set()

        def _write_for_process(self, process, response):
            replies.append((process, response))
            loop.call_soon_threadsafe(replied.set)

    async def callback(params):
        assert params == request['params']
        entered.set()
        await release.wait()
        return {'source_version_id': 'frozen'}

    client = attach_research_tools(Client(), callback)
    reading = asyncio.create_task(asyncio.to_thread(client._reader_loop, original_process, 1))
    await asyncio.wait_for(entered.wait(), 2)
    assert await asyncio.to_thread(drained.wait, 2)
    assert notifications == [{'method': 'turn/started'}] and not replies
    release.set()
    await asyncio.wait_for(replied.wait(), 2)
    await reading
    assert original_process.stdout is original_stream
    assert replies[0][0] is original_process and replies[0][1]['id'] == 7
    result = replies[0][1]['result']
    assert result['success'] and json.loads(result['contentItems'][0]['text']) == {'source_version_id': 'frozen'}


@pytest.mark.asyncio
@pytest.mark.parametrize('typed', [False, True])
async def test_tool_reply_reports_only_explicit_safe_schema_issues(typed):
    replies, replied = [], asyncio.Event()
    loop = asyncio.get_running_loop()
    process = type('Process', (), {})()
    request = {'id': 7, 'method': 'item/tool/call', 'params': {
        'threadId': 'owner', 'turnId': 'turn', 'callId': 'save', 'tool': 'save_facts',
        'arguments': {'private_source': 'DO_NOT_LEAK'}}}
    process.stdout = io.StringIO(json.dumps(request) + '\n')
    class Client:
        def _reader_loop(self, process, generation):
            list(process.stdout)
        def _write_for_process(self, original, response):
            assert original is process
            replies.append(response)
            loop.call_soon_threadsafe(replied.set)
    issues = [{'path': ['facts', 0], 'rule': 'required', 'missing_fields': ['source_date']}]
    async def callback(params):
        if typed:
            raise ResearchToolArgumentsInvalid(issues)
        raise ValueError('private exception message DO_NOT_LEAK')
    client = attach_research_tools(Client(), callback)
    await asyncio.to_thread(client._reader_loop, process, 1)
    await asyncio.wait_for(replied.wait(), 2)
    result = replies[0]['result']
    assert result['success'] is False and 'DO_NOT_LEAK' not in json.dumps(result)
    body = json.loads(result['contentItems'][0]['text'])
    if typed:
        assert body['validation_issues'] == issues and body['saved'] is False
    else:
        assert body == {'error_code': 'native_research_tool_failed', 'error_type': 'ValueError'}


def test_rejected_malformed_call_is_not_an_assessment_delivery():
    from street_story.native_fact_contract import delivered_candidates
    receipt = {'thread_id': 'own', 'turn_id': 'turn', 'raw_result': {'facts': []},
        'native_tool_calls': {'bad': {'phase': 'rejected', 'input': {'name': 'save_facts',
            'thread_id': 'own', 'turn_id': 'turn', 'arguments': {'facts': [{'missing': 'required fields'}]}}}}}
    assert delivered_candidates(receipt) == {'facts': []}
    assert receipt['native_tool_calls']['bad']['phase'] == 'rejected'


def test_model_visible_validation_contract_changes_native_profile(tmp_path, monkeypatch):
    from street_story.native_fact_research import NativeFactResearch
    from street_story import native_research_tools
    provider = NativeFactResearch(SimpleNamespace(store=None, settings=SimpleNamespace(data_dir=tmp_path)),
                                 admission=None, checkpoint=None)
    current = provider.profile_fingerprint
    monkeypatch.setattr(native_research_tools, 'TOOL_VALIDATION_CONTRACT', 'different-schema-feedback')
    assert provider.profile_fingerprint != current


@pytest.mark.asyncio
async def test_late_callback_from_replaced_process_has_no_product_effect():
    loop = asyncio.get_running_loop()
    replied, effects, replies = asyncio.Event(), [], []
    old = SimpleNamespace(stdout=io.StringIO(json.dumps({'id': 44, 'method': 'item/tool/call', 'params': {
        'threadId': 'owned', 'turnId': 'turn', 'callId': 'save', 'tool': 'save_facts',
        'arguments': {'facts': []}}}) + '\n'))
    class Client:
        _process = object()  # A different process already owns the transport.
        def _reader_loop(self, process, generation):
            list(process.stdout)
        def _write_for_process(self, process, response):
            replies.append((process, response))
            loop.call_soon_threadsafe(replied.set)
            raise ConnectionError('Original process cannot receive a late reply')
    async def callback(params):
        effects.append(params)
        return {'saved': True}
    client = attach_research_tools(Client(), callback)
    await asyncio.to_thread(client._reader_loop, old, 1)
    await asyncio.wait_for(replied.wait(), 2)
    assert effects == []
    assert replies[0][0] is old and replies[0][1]['id'] == 44
    assert replies[0][1]['result']['success'] is False
