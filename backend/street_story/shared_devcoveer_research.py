"""Directory-scoped research on the existing DevCoveer OpenCode service.

No server, dependencies or configuration are created here. Install the small
profile in a dedicated capsule directory, then attest its effective readback.
An attested pre-execution guard enforces the permitted tool boundary.
OpenCode's agent.steps is a reminder, not a hard execution allowance.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from contextvars import ContextVar
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode

import httpx

from .opencode_research import OpenCodeResearch, ResearchLimits, ResearchUnavailable

GUARD_SOURCE = Path(__file__).resolve().parents[1] / 'deploy' / 'research_guard.mjs'
NATIVE_TOOL_IDS = {'invalid', 'question', 'bash', 'read', 'glob', 'grep', 'edit', 'write',
                   'task', 'webfetch', 'todowrite', 'websearch', 'skill', 'apply_patch'}


def guard_profile(directory: Path, limits: ResearchLimits):
    digest = hashlib.sha256(GUARD_SOURCE.read_bytes()).hexdigest()
    guard = directory / ('research-guard-' + digest + '.mjs')
    return [guard.as_uri(), {'marker': 'street-story-research-guard:' + digest}]


def scoped_research_config(model_id: str, *, provider_id: str = 'opencode',
                           limits: ResearchLimits | None = None, directory: Path | None = None) -> dict:
    """Data-only profile for a dedicated directory on the SAME existing server.

    Do not install at a repository root or in global config. Existing provider
    credentials remain selected by the shared server. Inherited plugins/MCP or
    instruction overrides fail runtime attestation rather than being replaced.
    """
    limits = limits or ResearchLimits()
    config = {'$schema': 'https://opencode.ai/config.json', 'share': 'disabled', 'snapshot': False,
            'tool_output': {'max_bytes': limits.max_search_context_chars, 'max_lines': 300},
            'agent': {'plan': {'steps': min(3, limits.max_steps)},
                      **{name: {'disable': True} for name in ('title', 'summary', 'compaction')}},
            'provider': {provider_id: {'models': {model_id: {'limit': {
                'context': 200000, 'output': min(2048, limits.max_output_tokens)}}}}}}
    if directory is not None:
        config['plugin'] = [guard_profile(directory, limits)]
    return config


class SharedDevCoveerResearch(OpenCodeResearch):
    """Thin scoped transport; base class owns parsing, admission and recovery.

    The worker deadline includes attestation and dispatch. Cancellation allows
    a separately bounded abort/readback checkpoint. Concurrent attempts keep
    deadline and receipt state in separate task contexts.
    """

    def __init__(self, directory: str, *, backend=None, abort_timeout_seconds: float = 10,
                 require_guard: bool = True, **kwargs):
        path = Path(directory)
        if not path.is_absolute() or '..' in path.parts or path.resolve() != path or not path.is_dir():
            raise ValueError('research_capsule_directory_invalid')
        if any((ancestor / '.git').exists() for ancestor in (path, *path.parents)):
            raise ValueError('research_capsule_must_be_outside_repository')
        if not 0 < abort_timeout_seconds <= 30:
            raise ValueError('research_abort_timeout_invalid')
        if 'client' in kwargs or 'agent_names' in kwargs:
            raise ValueError('research_shared_transport_required')
        super().__init__('http://127.0.0.1:4097', agent_names={role: 'plan' for role in ('search', 'vision', 'facts')},
                         **kwargs)
        if (not 0 < self.limits.timeout_seconds <= 120 or not 0 < self.limits.max_steps <= 3
                or not 0 < self.limits.max_output_tokens <= 2048):
            raise ValueError('research_shared_limits_invalid')
        self.directory = str(path)
        self.require_guard = require_guard
        self.shared_backend = backend
        self.abort_timeout_seconds = abort_timeout_seconds
        self._deadline: ContextVar[float | None] = ContextVar('research_deadline', default=None)
        self._receipt: ContextVar[dict | None] = ContextVar('research_receipt', default=None)

    @property
    def profile_fingerprint(self):
        return hashlib.sha256(json.dumps(scoped_research_config(self.model_id, provider_id=self.provider_id,
            limits=self.limits, directory=Path(self.directory) if self.require_guard else None), sort_keys=True).encode()).hexdigest()

    async def _request(self, client, method, path, **kwargs):
        allowed = ((method == 'GET' and path in {'/global/health', '/config', '/agent', '/experimental/tool/ids'})
                   or (method == 'POST' and path == '/session')
                   or re.fullmatch(r'/session/ses[A-Za-z0-9_-]+/(message|prompt_async|abort)', path)
                   and ((method == 'GET' and path.endswith('/message'))
                        or (method == 'POST' and path.endswith(('/prompt_async', '/abort')))))
        if not allowed or client is not None:
            raise ResearchUnavailable('research_api_operation_denied')
        payload = kwargs.get('json')
        if path.endswith('/prompt_async') and isinstance(payload, dict) and 'tools' in payload:
            raise ResearchUnavailable('research_permission_replacement_denied')
        if self.shared_backend is None:
            import sys
            platform_path = '/home/dev/.local/libexec/openai-codex-mcp'
            if platform_path not in sys.path:
                sys.path.insert(0, platform_path)
            from opencode_backend import OpenCodeBackend
            self.shared_backend = OpenCodeBackend()
        deadline = self._deadline.get()
        timeout = (self.abort_timeout_seconds if path.endswith('/abort') else
                   min(30, max(.001, deadline - time.monotonic())) if deadline is not None else 30)
        if method == 'GET' and path.endswith('/message'):
            # Check session policy before any new prompt, and again on recovery.
            session_path = path.removesuffix('/message')
            session = await self._invoke('GET', session_path, timeout=timeout)
            expected = self._session_permissions(((self._receipt.get() or {}).get('value') or {}).get('role'))
            if session.get('permission') != expected or session.get('directory') != self.directory:
                raise ResearchUnavailable('research_session_scope_unverified')
        if kwargs.get('params'):
            path += '?' + urlencode(kwargs['params'])
        return await self._invoke(method, path, payload=payload, timeout=timeout)

    async def _invoke(self, method, path, *, timeout, payload=None):
        try:
            result = await asyncio.wait_for(self.shared_backend.request(
                method, path, directory=self.directory, payload=payload, timeout=timeout), timeout=timeout)
            if len(json.dumps(result, ensure_ascii=False).encode()) > self.limits.max_response_bytes:
                raise ResearchUnavailable('research_response_too_large')
            return result
        except TimeoutError as exc:
            raise httpx.ReadTimeout('Shared OpenCode request exceeded its bound') from exc
        except Exception as exc:
            # Platform transport uses a typed error instead of httpx; normalize
            # only that boundary so the existing durable recovery handles it.
            if type(exc).__name__ != 'OpenCodeError':
                raise
            raise httpx.TransportError('Shared OpenCode transport failed') from exc

    def _session_permissions(self, role):
        if self.require_guard:
            return [{'permission': name, 'pattern': '*', 'action': 'deny'}
                    for name in ('question', 'plan_enter', 'plan_exit')]
        return [{'permission': '*', 'pattern': '*', 'action': 'deny'},
                *([{'permission': 'websearch', 'pattern': '*', 'action': 'allow'}] if role == 'search' else [])]

    async def _attest(self, client, role):
        health = await self._request(client, 'GET', '/global/health')
        if not isinstance(health, dict) or health.get('healthy') is not True:
            raise ResearchUnavailable('research_shared_unhealthy')
        guard_verified = False
        if self.require_guard:
            if role != 'search':
                raise ResearchUnavailable('research_shared_search_only')
            profile = guard_profile(Path(self.directory), self.limits)
            guard = Path(self.directory) / Path(profile[0]).name
            if not guard.is_file() or guard.is_symlink() or guard.read_bytes() != GUARD_SOURCE.read_bytes():
                raise ResearchUnavailable('research_guard_file_unverified')
            local = json.loads((Path(self.directory) / 'opencode.json').read_text())
            if local.get('username') == profile[1]['marker'] or local.get('plugin') != [profile]:
                raise ResearchUnavailable('research_guard_profile_unverified')
            ids = await self._request(client, 'GET', '/experimental/tool/ids')
            if not isinstance(ids, list) or len(ids) != len(NATIVE_TOOL_IDS) or set(ids) != NATIVE_TOOL_IDS:
                raise ResearchUnavailable('research_tool_registry_unverified')
        config = await self._request(client, 'GET', '/config')
        agents = await self._request(client, 'GET', '/agent')
        if self.require_guard:
            if config.get('plugin') != [profile] or config.get('username') != profile[1]['marker']:
                raise ResearchUnavailable('research_guard_not_loaded')
            guard_verified = True
        if ((config.get('plugin') and not guard_verified) or config.get('mcp') or config.get('instructions') or config.get('references')
                or config.get('reference') or config.get('share') != 'disabled' or config.get('snapshot') is not False):
            raise ResearchUnavailable('research_capsule_config_unverified')
        if any((config.get('agent', {}).get(name) or {}).get('disable') is not True
               for name in ('title', 'summary', 'compaction')):
            raise ResearchUnavailable('research_background_inference_not_disabled')
        if any(agent.get('name') in {'title', 'summary', 'compaction'} for agent in agents):
            raise ResearchUnavailable('research_background_inference_not_disabled')
        selected = next((agent for agent in agents if agent.get('name') == 'plan'), {})
        if selected.get('native') is not True or selected.get('mode') != 'primary':
            raise ResearchUnavailable('research_native_plan_unverified')
        steps = selected.get('steps')
        if type(steps) is not int or not 1 <= steps <= self.limits.max_steps:
            raise ResearchUnavailable('research_controller_steps_unverified')
        output = (config.get('provider', {}).get(self.provider_id, {}).get('models', {})
                  .get(self.model_id, {}).get('limit') or {}).get('output')
        tool_bytes = (config.get('tool_output') or {}).get('max_bytes')
        if type(output) is not int or not 1 <= output <= self.limits.max_output_tokens:
            raise ResearchUnavailable('research_model_output_not_bounded')
        if type(tool_bytes) is not int or not 1 <= tool_bytes <= self.limits.max_search_context_chars:
            raise ResearchUnavailable('research_search_context_not_bounded')
        return {'agent': 'plan', 'directory': self.directory, 'runtime_version': health.get('version'),
                'steps': steps, 'steps_enforcement': 'controller_observed_abort', 'atomic_tool_call_cap': False,
                'tool_boundary_enforced': guard_verified, 'search_call_limit': None,
                'allowed_tools': ['websearch'] if role == 'search' else [], 'deny_default': True,
                'permission_authority': 'attested_pre_execution_guard' if guard_verified else 'session_readback',
                'guard_sha256': profile[1]['marker'].split(':')[-1] if guard_verified else None,
                'max_output_tokens': output,
                'max_tool_bytes': tool_bytes, 'worker_timeout_seconds': self.limits.timeout_seconds}

    async def _checkpoint(self, binding, receipt):
        calls = {call.get('call_id') for call in receipt.get('search_calls', [])}
        assistants = receipt.get('assistants', [])
        receipt['controller_counters'] = {
            'observed_search_calls': len(calls), 'observed_assistant_steps': len(assistants),
            'assistant_step_overshoot': max(0, len(assistants) - self.limits.max_steps),
            'search_call_limit': None, 'atomic_tool_call_cap': False}
        holder = self._receipt.get()
        if holder is not None:
            # Shielded abort runs in a child task. Its final receipt must remain
            # visible to the waiting worker when cancellation has completed.
            holder['value'] = copy.deepcopy(receipt)
        await super()._checkpoint(binding, receipt)

    async def _run(self, role, prompt, binding, schema, *, snapshot=None):
        deadline_token = self._deadline.set(time.monotonic() + self.limits.timeout_seconds)
        receipt_token = self._receipt.set({'value': {'role': role, 'binding': binding, 'phase': 'attesting'}})
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                return await super()._run(role, prompt, binding, schema, snapshot=snapshot)
        except TimeoutError as exc:
            receipt = (self._receipt.get() or {}).get('value') or {}
            receipt['error_code'] = 'research_worker_timeout'
            await self._checkpoint(binding, receipt)
            raise ResearchUnavailable('research_worker_timeout', receipt) from exc
        finally:
            self._deadline.reset(deadline_token)
            self._receipt.reset(receipt_token)
