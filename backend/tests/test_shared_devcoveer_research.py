from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from urllib.parse import urlsplit

import httpx
import pytest

from street_story.opencode_research import ResearchLimits, ResearchUnavailable
from street_story.shared_devcoveer_research import (
    GUARD_SOURCE, NATIVE_TOOL_IDS, SharedDevCoveerResearch, guard_profile, scoped_research_config,
)
from test_opencode_research import Harness


class Backend:
    def __init__(self, harness, directory):
        self.h = harness
        self.directory = str(directory)
        self.calls, self.session = [], None
        self.drop_policy = self.hang_messages = self.hang_abort = False
        self.extra_calls = 0
        self.guard_loaded = True
        self.tool_ids = sorted(NATIVE_TOOL_IDS)
        self.agent_permission = [{'permission': '*', 'pattern': '*', 'action': 'allow'}]

    async def request(self, method, path, *, directory=None, payload=None, timeout=30):
        self.calls.append((method, path, directory, copy.deepcopy(payload), timeout))
        assert directory == self.directory
        clean_path = urlsplit(path).path
        if clean_path == '/global/health':
            return {'healthy': True, 'version': '1.18.31'}
        if clean_path == '/agent':
            return [{'name': 'plan', 'native': True, 'mode': 'primary',
                     'steps': self.h.config['agent']['plan']['steps'], 'permission': self.agent_permission}]
        if clean_path == '/experimental/tool/ids':
            if self.guard_loaded and self.h.config.get('plugin'):
                self.h.config['username'] = self.h.config['plugin'][0][1]['marker']
            return self.tool_ids
        if clean_path == '/session/sesBounded':
            return {**self.session, 'permission': [] if self.drop_policy else self.session['permission']}
        if clean_path.endswith('/abort') and self.hang_abort:
            await asyncio.Event().wait()
        if clean_path.endswith('/message') and self.hang_messages and self.h.message_id:
            await asyncio.Event().wait()
        request = httpx.Request(method, 'http://127.0.0.1:4097' + path,
                                content=json.dumps(payload).encode() if payload is not None else b'')
        response = self.h.handle(request)
        response.request = request
        response.raise_for_status()
        result = response.json() if response.content else None
        if clean_path == '/session' and method == 'POST':
            self.session = {**result, 'directory': directory, 'permission': copy.deepcopy(payload['permission'])}
            return self.session
        if clean_path.endswith('/message') and self.extra_calls and self.h.message_id:
            assistant = result[-1]
            original = next(part for part in assistant['parts'] if part.get('type') == 'tool')
            for n in range(self.extra_calls):
                assistant['parts'].append({**copy.deepcopy(original), 'callID': 'overshoot_' + str(n)})
        return result


def setup(tmp_path, **kwargs):
    h = Harness()
    h.config = scoped_research_config('mimo-v2.6-flash-free')
    backend = Backend(h, tmp_path)
    adapter = SharedDevCoveerResearch(str(tmp_path), backend=backend, model_id='mimo-v2.6-flash-free',
                                     admission=h.admission, checkpoint=h.checkpoint,
                                     require_guard=kwargs.pop('require_guard', False), **kwargs)
    return h, backend, adapter


@pytest.mark.asyncio
async def test_native_plan_session_overrides_permissions_and_every_request_is_scoped(tmp_path):
    h, backend, adapter = setup(tmp_path)
    result = await adapter.search_articles('Facade alternatives', {'request_id': 'r'})
    assert result['sources'][0]['url'] == 'https://example.org/gallery'
    assert result['receipt']['isolation']['steps_enforcement'] == 'controller_observed_abort'
    assert result['receipt']['isolation']['atomic_tool_call_cap'] is False
    assert result['receipt']['controller_counters']['search_call_limit'] is None
    assert backend.session['permission'] == adapter._session_permissions('search')
    prompt = next(call[3] for call in backend.calls if '/prompt_async' in call[1])
    assert prompt['agent'] == 'plan' and 'tools' not in prompt
    assert all(call[2] == str(tmp_path) for call in backend.calls)
    assert not any(method == 'PATCH' for method, *_rest in backend.calls)
    assert h.finalized[0][1] == 'completed'


@pytest.mark.asyncio
async def test_session_permission_readback_must_confirm_policy_before_inference(tmp_path):
    h, backend, adapter = setup(tmp_path)
    backend.drop_policy = True
    with pytest.raises(ResearchUnavailable, match='research_session_scope_unverified') as failure:
        await adapter.search_articles('Facade', {'request_id': 'r'})
    assert failure.value.receipt['session_id'] == 'sesBounded'
    assert not h.sends
    assert not any('/prompt_async' in path for _method, path, *_rest in backend.calls)


@pytest.mark.asyncio
async def test_role_without_search_cannot_gain_agent_websearch_permission(tmp_path):
    h, backend, adapter = setup(tmp_path)
    h.result = {'facts': []}
    await adapter.extract_facts({'binding': {'request_id': 'f'}, 'jsonschema': {'type': 'object'},
                                 'source_passages': [{'text': 'Construction was planned.'}]})
    assert backend.session['permission'] == adapter._session_permissions('facts')
    assert backend.session['permission'] == [{'permission': '*', 'pattern': '*', 'action': 'deny'}]


@pytest.mark.asyncio
async def test_multiple_searches_preserve_all_discoveries_without_artificial_call_cap(tmp_path):
    h, backend, adapter = setup(tmp_path)
    backend.extra_calls = 2
    result = await adapter.search_articles('Facade', {'request_id': 'r'})
    receipt = result['receipt']
    assert receipt['controller_counters']['observed_search_calls'] == 3
    assert receipt['controller_counters']['search_call_limit'] is None
    assert h.finalized[0][0]['actual_total_tokens'] == 70
    assert sum('/abort' in path for _method, path, *_rest in backend.calls) == 0
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_hanging_transport_has_bounded_abort_and_unknown_ack_is_retained(tmp_path):
    h, backend, adapter = setup(tmp_path, limits=ResearchLimits(timeout_seconds=.02, poll_seconds=.001),
                                abort_timeout_seconds=.01)
    backend.hang_messages = backend.hang_abort = True
    with pytest.raises(ResearchUnavailable) as failure:
        await asyncio.wait_for(adapter.search_articles('Facade', {'request_id': 'r'}), timeout=.5)
    assert failure.value.receipt['abort_acknowledged'] is False
    assert failure.value.receipt['phase'] == 'abort_outcome_unknown'
    assert len(h.sends) == 1
    assert sum('/abort' in path for _method, path, *_rest in backend.calls) == 1


@pytest.mark.asyncio
async def test_existing_result_is_reconciled_without_second_prompt(tmp_path):
    h, backend, adapter = setup(tmp_path)
    result = await adapter.search_articles('Facade', {'request_id': 'r'})
    receipt = result['receipt']
    resumed = await adapter.search_articles('Facade', {'request_id': 'r', 'session_id': receipt['session_id'],
                                                     'message_id': receipt['message_id'], 'phase': 'submitted'})
    assert resumed['sources'] == result['sources']
    assert len(h.sends) == 1
    assert sum('/prompt_async' in path for _method, path, *_rest in backend.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change,code', [
    ({'provider': {'opencode': {'models': {'mimo-v2.6-flash-free': {'limit': {'output': 2049}}}}}},
     'research_model_output_not_bounded'),
    ({'plugin': ['untrusted']}, 'research_capsule_config_unverified'),
    ({'mcp': {'writer': {'enabled': True}}}, 'research_capsule_config_unverified'),
    ({'instructions': ['private-file']}, 'research_capsule_config_unverified'),
    ({'share': 'auto'}, 'research_capsule_config_unverified'),
    ({'agent': {'plan': {'steps': 3}}}, 'research_background_inference_not_disabled'),
])
async def test_scoped_config_must_attest_before_any_dispatch(tmp_path, change, code):
    h, backend, adapter = setup(tmp_path)
    h.config.update(change)
    with pytest.raises(ResearchUnavailable, match=code):
        await adapter.search_articles('Facade', {'request_id': 'r'})
    assert not h.sends and not any(method == 'POST' for method, *_rest in backend.calls)


@pytest.mark.asyncio
async def test_gateway_never_executes_commands_or_replaces_session_policy(tmp_path):
    h, backend, adapter = setup(tmp_path)
    with pytest.raises(ResearchUnavailable, match='research_api_operation_denied'):
        await adapter._request(None, 'POST', '/session/sesBounded/shell', json={'command': 'x'})
    with pytest.raises(ResearchUnavailable, match='research_permission_replacement_denied'):
        await adapter._request(None, 'POST', '/session/sesBounded/prompt_async', json={'tools': {'bash': True}})
    assert not backend.calls


@pytest.mark.asyncio
async def test_shared_sdk_response_also_obeys_product_body_limit(tmp_path):
    h, backend, adapter = setup(tmp_path, limits=ResearchLimits(max_response_bytes=1024))
    h.config['untrusted_oversized_extra'] = 'x' * 2048
    with pytest.raises(ResearchUnavailable, match='research_response_too_large'):
        await adapter.search_articles('Facade', {'request_id': 'r'})
    assert not h.sends and not any(method == 'POST' for method, *_rest in backend.calls)


def test_capsule_directory_cannot_modify_normal_repository_profiles(tmp_path):
    (tmp_path / '.git').mkdir()
    with pytest.raises(ValueError, match='research_capsule_must_be_outside_repository'):
        SharedDevCoveerResearch(str(tmp_path), model_id='mimo-v2.6-flash-free')


def test_scoped_profile_keeps_provider_credentials_and_other_native_agents_untouched():
    config = scoped_research_config('mimo-v2.6-flash-free')
    assert set(config['agent']) == {'plan', 'title', 'summary', 'compaction'}
    assert 'permission' not in config and 'default_agent' not in config and 'plugin' not in config
    assert config['provider']['opencode']['models']['mimo-v2.6-flash-free'] == {'limit': {'context': 200000, 'output': 2048}}


def test_real_plugin_enforces_tool_boundary_without_limiting_search_quantity(tmp_path):
    script = """
      const {StreetStoryResearchGuard} = await import(process.argv[1]);
      const hooks = await StreetStoryResearchGuard({directory: process.argv[2]}, {marker:'test-loaded'});
      const config = {}; await hooks.config(config);
      if(config.username !== 'test-loaded') throw new Error('marker_missing');
      for(let i=0; i<20; i++) await hooks['tool.execute.before']({tool:'websearch',sessionID:'ses_test',callID:String(i)});
      for(const tool of ['bash','read','write','task','webfetch','apply_patch','skill']) {
        let denied = false;
        try { await hooks['tool.execute.before']({tool,sessionID:'ses_test',callID:'denied'}); }
        catch(error) { denied = error.message === 'research_tool_denied'; }
        if(!denied) throw new Error('tool_not_denied:'+tool);
      }
    """
    subprocess.run(['node', '--input-type=module', '-e', script, GUARD_SOURCE.as_uri(), str(tmp_path)], check=True,
                   capture_output=True, text=True, timeout=10)


def guarded_setup(tmp_path):
    h, backend, adapter = setup(tmp_path, require_guard=True)
    h.config = scoped_research_config('mimo-v2.6-flash-free', directory=tmp_path)
    (tmp_path / 'opencode.json').write_text(json.dumps(h.config))
    profile = guard_profile(tmp_path, adapter.limits)
    (tmp_path / profile[0].split('/')[-1]).write_bytes(GUARD_SOURCE.read_bytes())
    return h, backend, adapter


@pytest.mark.asyncio
async def test_loaded_guard_attested_before_prompt_with_native_tool_schemas(tmp_path):
    h, backend, adapter = guarded_setup(tmp_path)
    result = await adapter.search_articles('Facade', {'request_id': 'r'})
    assert result['receipt']['isolation']['tool_boundary_enforced'] is True
    assert result['receipt']['isolation']['permission_authority'] == 'attested_pre_execution_guard'
    assert backend.session['permission'] == adapter._session_permissions('search')
    assert not any(rule['permission'] == '*' for rule in backend.session['permission'])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code', [
    ('not_loaded', 'research_guard_not_loaded'),
    ('foreign_tools', 'research_tool_registry_unverified'),
    ('duplicate_builtin', 'research_tool_registry_unverified'),
    ('forged_marker', 'research_guard_profile_unverified'),
    ('changed_file', 'research_guard_file_unverified'),
])
async def test_failed_guard_attestation_never_dispatches_inference(tmp_path, failure, code):
    h, backend, adapter = guarded_setup(tmp_path)
    if failure == 'not_loaded':
        backend.guard_loaded = False
    elif failure == 'foreign_tools':
        backend.tool_ids.append('foreign')
    elif failure == 'duplicate_builtin':
        backend.tool_ids.append('websearch')
    elif failure == 'forged_marker':
        local = json.loads((tmp_path / 'opencode.json').read_text())
        local['username'] = local['plugin'][0][1]['marker']
        (tmp_path / 'opencode.json').write_text(json.dumps(local))
    else:
        guard = next(tmp_path.glob('research-guard-*.mjs'))
        guard.write_text('export const broken = true;')
    with pytest.raises(ResearchUnavailable, match=code):
        await adapter.search_articles('Facade', {'request_id': 'r'})
    assert not h.sends and not any('/prompt_async' in path for _, path, *_ in backend.calls)


@pytest.mark.parametrize('limits', [ResearchLimits(timeout_seconds=0), ResearchLimits(max_steps=4),
                                   ResearchLimits(max_output_tokens=2049)])
def test_shared_worker_cannot_silently_expand_admission_bounds(tmp_path, limits):
    with pytest.raises(ValueError, match='research_shared_limits_invalid'):
        SharedDevCoveerResearch(str(tmp_path), model_id='mimo-v2.6-flash-free', limits=limits)
