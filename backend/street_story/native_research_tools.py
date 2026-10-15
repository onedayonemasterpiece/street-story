"""Public research callbacks on the installed, fully drained native transport.

The platform reader still owns RPC demultiplexing, lifecycle and notifications.
This stream adapter consumes only dynamic tool requests before that reader's
default rejection. It creates no transport, subprocess or independent queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

LOG = logging.getLogger(__name__)
TOOL_METHOD = 'item/tool/call'
TOOL_VALIDATION_CONTRACT = 'native-tool-schema-feedback-v1'
ORPHAN_CONTRACT = 'native-owned-callback-recovery-v1'


def process_identity(pid):
    """Nonsecret process incarnation; PID reuse/reboot must not prove liveness."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': stat[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except FileNotFoundError:
        return {'pid': pid, 'absent': True}
    except (OSError, IndexError):
        return None


def execution_owner(client):
    process = getattr(client, '_process', None)
    return {'backend': process_identity(os.getpid()),
        'transport': process_identity(getattr(process, 'pid', None))}


def lost_execution_owner(owner):
    for kind in ('backend', 'transport'):
        previous = (owner or {}).get(kind)
        if not isinstance(previous, dict) or not previous.get('start_ticks') or not previous.get('boot_id'):
            continue
        current = process_identity(previous.get('pid'))
        if current is not None and current != previous:
            return {'kind': kind, 'original': previous, 'observed': current}
    return None


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
    active = {}
    active_lock = threading.Lock()
    client._street_story_active_calls = active
    def has_active_call(address):
        with active_lock:
            return bool(active.get(address))
    client._street_story_has_active_call = has_active_call

    async def answer(process, message):
        params = message.get('params')
        safe_params = params if isinstance(params, dict) else {}
        try:
            if not isinstance(params, dict):
                raise ValueError('native_research_tool_request_invalid')
            if (getattr(client, '_process', process) is not process
                    or callable(getattr(process, 'poll', None)) and process.poll() is not None):
                raise ConnectionError('native_research_callback_process_lost')
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
            params = message.get('params') or {}
            address = tuple(params.get(key) if isinstance(params.get(key), str) else None
                for key in ('threadId', 'turnId', 'callId')) if isinstance(params, dict) else ()
            # Count duplicates: one finishing response must not hide a sibling.
            with active_lock:
                active[address] = active.get(address, 0) + 1
            future = asyncio.run_coroutine_threadsafe(answer(process, message), loop)
            pending.add(future)
            def done(future):
                pending.discard(future)
                with active_lock:
                    active[address] -= 1
                    if not active[address]:
                        active.pop(address)
            future.add_done_callback(done)

        process.stdout = ResearchToolStream(stream, dispatch)
        try:
            reader(process, generation)
        finally:
            process.stdout = stream

    client._reader_loop = read
    client._street_story_research_tools = True
    return client
