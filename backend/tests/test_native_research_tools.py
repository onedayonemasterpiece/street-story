import asyncio
import io
import json
import threading

import pytest

from street_story.native_research_tools import ResearchToolStream, attach_research_tools


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
