"""Public research callbacks on the installed, fully drained native transport.

The platform reader still owns RPC demultiplexing, lifecycle and notifications.
This stream adapter consumes only dynamic tool requests before that reader's
default rejection. It creates no transport, subprocess or independent queue.
"""
from __future__ import annotations

import asyncio
import json
import logging

LOG = logging.getLogger(__name__)
TOOL_METHOD = 'item/tool/call'
TOOL_VALIDATION_CONTRACT = 'native-tool-schema-feedback-v1'


class ResearchToolArgumentsInvalid(ValueError):
    """Public schema addresses only; never exception text or supplied values."""
    def __init__(self, issues):
        super().__init__('agent_tool_arguments_invalid')
        self.public_result = {'error_code': 'agent_tool_arguments_invalid', 'saved': False,
            'validation_issues': issues,
            'instruction': 'No facts from this call were saved. Correct the named schema fields '
                'from your evidence and retry in this same turn. Do not repeat unchanged arguments. '
                'The server does not fill dates, evidence or semantic decisions.'}


class ResearchToolStream:
    def __init__(self, stream, dispatch):
        self.stream, self.dispatch = stream, dispatch

    def __iter__(self):
        for line in self.stream:
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                yield line
                continue
            if (isinstance(message, dict) and message.get('id') is not None
                    and message.get('method') == TOOL_METHOD):
                self.dispatch(message)
            else:
                # Approvals, unknown requests and ordinary responses keep the
                # platform's original fail-closed behavior and demultiplexing.
                yield line

    def __getattr__(self, name):
        return getattr(self.stream, name)


def attach_research_tools(client, callback, *, loop=None):
    """Add an instance-local asynchronous callback; never patch global runtime.

    The callback must validate exact owner/thread/turn and tool arguments, freeze
    completed reads and checkpoint before returning. It receives the native call
    ID, making duplicate delivery addressable by the owning research receipt.
    """
    if getattr(client, '_street_story_research_tools', False):
        raise ValueError('native_research_tools_already_attached')
    loop = loop or asyncio.get_running_loop()
    reader = client._reader_loop
    pending = set()

    async def answer(process, message):
        params = message.get('params')
        safe_params = params if isinstance(params, dict) else {}
        try:
            if not isinstance(params, dict):
                raise ValueError('native_research_tool_request_invalid')
            result = await callback(params)
            response = {'success': True, 'contentItems': [{'type': 'inputText',
                'text': json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            # Private page bodies, inputs and exception messages are excluded.
            LOG.info('native_research_tool_failed thread_id=%s turn_id=%s call_id=%s error_type=%s',
                safe_params.get('threadId'), safe_params.get('turnId'),
                safe_params.get('callId'), type(exc).__name__)
            public = (exc.public_result if isinstance(exc, ResearchToolArgumentsInvalid) else
                      {'error_code': 'native_research_tool_failed', 'error_type': type(exc).__name__})
            response = {'success': False, 'contentItems': [{'type': 'inputText',
                'text': json.dumps(public)}]}
        try:
            # The exact original process is mandatory. A late callback cannot
            # write into a replacement app-server after a disconnect/restart.
            await asyncio.to_thread(client._write_for_process, process,
                {'jsonrpc': '2.0', 'id': message['id'], 'result': response})
        except (ConnectionError, OSError):
            LOG.info('native_research_tool_reply_unobserved call_id=%s', safe_params.get('callId'))

    def read(process, generation):
        stream = process.stdout

        def dispatch(message):
            future = asyncio.run_coroutine_threadsafe(answer(process, message), loop)
            pending.add(future)
            future.add_done_callback(pending.discard)

        process.stdout = ResearchToolStream(stream, dispatch)
        try:
            reader(process, generation)
        finally:
            process.stdout = stream

    client._reader_loop = read
    client._street_story_research_tools = True
    return client
