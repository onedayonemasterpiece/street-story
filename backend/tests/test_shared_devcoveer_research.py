from __future__ import annotations
from direct_visual_fixture import opencode_args

import asyncio
import ast
import copy
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from street_story.opencode_research import ResearchLimits, ResearchUnavailable
from street_story.shared_devcoveer_research import (
    GUARD_SOURCE, NATIVE_TOOL_IDS, SharedDevCoveerResearch, guard_profile, scoped_research_config,
)
from test_opencode_research import Harness, sheet
from test_reference_image_codec import jpeg


class Backend:
    def __init__(self, harness, directory):
        self.h = harness
        self.directory = str(directory)
        self.calls, self.session = [], None
        self.drop_policy = self.hang_messages = self.hang_abort = False
        self.extra_calls = 0
        self.guard_loaded = True
        self.tool_ids = sorted(NATIVE_TOOL_IDS)
        self.health = {'healthy': True, 'version': '1.18.31'}
        self.agent_permission = [{'permission': '*', 'pattern': '*', 'action': 'allow'}]
        self.image_capabilities = {'attachment': True, 'input': {'image': True}}

    async def request(self, method, path, *, directory=None, payload=None, timeout=30):
        self.calls.append((method, path, directory, copy.deepcopy(payload), timeout))
        assert directory == self.directory
        clean_path = urlsplit(path).path
        if clean_path == '/global/health':
            return self.health
        if clean_path == '/agent':
            return [{'name': 'plan', 'native': True, 'mode': 'primary',
                     'steps': self.h.config['agent']['plan']['steps'], 'permission': self.agent_permission}]
        if clean_path == '/config/providers':
            return {'providers': [{'id': 'opencode', 'models': {'mimo-v2.6-flash-free': {
                'capabilities': self.image_capabilities}}}]}
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
    async def public_image_loader(url):
        return 'image/jpeg', jpeg()
    kwargs.setdefault('public_image_loader', public_image_loader)
    h = Harness()
    h.config = scoped_research_config('mimo-v2.6-flash-free', limits=kwargs.get('limits'))
    backend = Backend(h, tmp_path)
    adapter = SharedDevCoveerResearch(str(tmp_path), backend=backend, model_id='mimo-v2.6-flash-free',
                                     admission=h.admission, checkpoint=h.checkpoint,
                                     require_guard=kwargs.pop('require_guard', False), **kwargs)
    return h, backend, adapter


@pytest.mark.asyncio
async def test_shared_identity_planner_forwards_operation_cap_without_expanding_fact_cap(tmp_path):
    h, backend, adapter = setup(tmp_path)
    h.result = {'summary': 'Actual sources'}
    schema = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
              'required': ['summary']}
    limits = adapter.limits
    result = await adapter.plan_identity_search('Observed map ' + 'x'*30000,
        {'request_id': 'shared-large-plan'}, schema)
    assert result['receipt']['phase'] == 'completed'
    assert adapter.limits is limits and limits.max_input_chars == 24000
    assert len(h.sends) == 1
    before = len(backend.calls)
    with pytest.raises(ResearchUnavailable, match='research_input_too_large'):
        await adapter._run('facts', 'x'*30000, {'request_id': 'normal-facts'}, schema)
    assert len(backend.calls) == before and len(h.sends) == 1


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
    ({'provider': {'opencode': {'models': {'mimo-v2.6-flash-free': {'limit': {'output': 8193}}}}}},
     'research_model_output_not_bounded'),
    ({'plugin': ['untrusted']}, 'research_capsule_config_unverified'),
    ({'mcp': {'writer': {'enabled': True}}}, 'research_capsule_config_unverified'),
    ({'mcp': {'writer': {}}}, 'research_capsule_config_unverified'),
    ({'mcp': {'writer': {'enabled': 'false'}}}, 'research_capsule_config_unverified'),
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
async def test_explicitly_disabled_inherited_mcp_allows_only_native_registry(tmp_path):
    h, backend, adapter = setup(tmp_path)
    h.config['mcp'] = {'unrelated-global-integration': {'enabled': False}}
    result = await adapter.search_articles('Facade', {'request_id': 'r'})
    assert result['receipt']['phase'] == 'completed'
    assert len(h.sends) == 1


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
    assert config['provider']['opencode']['models']['mimo-v2.6-flash-free'] == {'limit': {'context': 200000, 'output': 8192}}


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
async def test_guarded_fact_extraction_uses_same_server_with_search_denied(tmp_path):
    h, backend, adapter = guarded_setup(tmp_path)
    h.result = {'facts': []}
    result = await adapter.extract_facts({'binding': {'request_id': 'fact-reserve'},
        'jsonschema': {'type': 'object'}, 'source_passages': [{'text': 'Construction was planned.'}]})
    isolation = result['receipt']['isolation']
    assert isolation['allowed_tools'] == []
    assert isolation['permission_authority'] == 'attested_guard_and_session_readback'
    assert {'permission': 'websearch', 'pattern': '*', 'action': 'deny'} in backend.session['permission']
    assert all(directory == str(tmp_path) for _method, _path, directory, *_rest in backend.calls)
    assert len(h.sends) == 1


@pytest.mark.asyncio
async def test_guarded_facts_refuse_lost_session_policy_before_inference(tmp_path):
    h, backend, adapter = guarded_setup(tmp_path)
    backend.drop_policy = True
    with pytest.raises(ResearchUnavailable, match='research_session_scope_unverified'):
        await adapter.extract_facts({'binding': {'request_id': 'fact-reserve'},
            'jsonschema': {'type': 'object'}, 'source_passages': []})
    assert not h.sends


@pytest.mark.asyncio
async def test_guarded_vision_uses_actual_attachment_same_server_and_no_search(tmp_path):
    h, backend, adapter = guarded_setup(tmp_path)
    h.result = {'status': 'mismatch'}
    result = await adapter.compare_image(*opencode_args(sheet(), {'request_id': 'vision'}, {'type': 'object'}, 'SOURCE / REF park'))
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert result['receipt']['isolation']['allowed_tools'] == []
    assert {'permission': 'websearch', 'pattern': '*', 'action': 'deny'} in backend.session['permission']
    assert len(h.sends) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('capabilities', [{}, {'attachment': True, 'input': {'image': False}},
    {'attachment': False, 'input': {'image': True}}])
async def test_guarded_vision_rejects_text_only_or_unverified_image_transport_before_send(tmp_path, capabilities):
    h, backend, adapter = guarded_setup(tmp_path)
    backend.image_capabilities = capabilities
    with pytest.raises(ResearchUnavailable, match='research_model_image_input_unverified'):
        await adapter.compare_image(*opencode_args(sheet(), {'request_id': 'vision'}, {'type': 'object'}, 'SOURCE / REF park'))
    assert not h.sends


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['999.0.0', 'custom-build', None])
async def test_runtime_version_is_diagnostic_and_required_contract_still_attested(tmp_path, version):
    h, backend, adapter = guarded_setup(tmp_path)
    backend.health = {'healthy': True}
    if version is not None:
        backend.health['version'] = version
    result = await adapter.search_articles('Facade', {'request_id': 'r'})
    assert result['receipt']['isolation']['runtime_version'] == version
    assert result['receipt']['isolation']['tool_boundary_enforced'] is True
    assert h.sends


@pytest.mark.asyncio
async def test_unhealthy_runtime_does_not_dispatch_even_with_familiar_version(tmp_path):
    h, backend, adapter = guarded_setup(tmp_path)
    backend.health = {'healthy': False, 'version': '1.18.31'}
    with pytest.raises(ResearchUnavailable, match='research_shared_unhealthy'):
        await adapter.search_articles('Facade', {'request_id': 'r'})
    assert not h.sends


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
                                   ResearchLimits(max_output_tokens=8193)])
def test_shared_worker_cannot_silently_expand_admission_bounds(tmp_path, limits):
    with pytest.raises(ValueError, match='research_shared_limits_invalid'):
        SharedDevCoveerResearch(str(tmp_path), model_id='mimo-v2.6-flash-free', limits=limits)


@pytest.mark.parametrize('output', [2048, 4096, 8192])
def test_shared_profile_and_controller_use_same_finite_output_bound(tmp_path, output):
    limits = ResearchLimits(max_output_tokens=output)
    h, backend, adapter = setup(tmp_path, limits=limits)
    assert h.config['provider']['opencode']['models']['mimo-v2.6-flash-free']['limit']['output'] == output
    assert adapter.limits.max_output_tokens == output
    assert adapter.limits.max_steps == 3
    assert adapter.limits.timeout_seconds == 120


@pytest.mark.asyncio
async def test_default_shared_bound_is_charged_before_send_without_relaxing_tool_policy(tmp_path):
    h, backend, adapter = setup(tmp_path)
    result = await adapter.search_articles('Facade alternatives', {'request_id': 'r'})
    binding, workload = h.admissions[0]
    assert workload['max_output_tokens'] == 8192
    assert workload['max_steps'] == 3
    assert workload['estimated_tokens'] >= 10000 + 8192
    assert result['receipt']['isolation']['max_output_tokens'] == 8192
    assert result['receipt']['isolation']['deny_default'] is True
    assert result['receipt']['isolation']['allowed_tools'] == ['websearch']
    assert len(h.sends) == 1


@pytest.mark.parametrize('provider,model', [
    ('opencode', 'kimi-k2.5'), ('opencode', 'mimo-v2.6-flash'),
    ('another-provider', 'mimo-v2.6-flash-free'),
])
def test_other_shared_routes_preserve_original_completion_ceiling(tmp_path, provider, model):
    config = scoped_research_config(model, provider_id=provider)
    assert config['provider'][provider]['models'][model]['limit']['output'] == 2048
    adapter = SharedDevCoveerResearch(str(tmp_path), provider_id=provider, model_id=model)
    assert adapter.limits.max_output_tokens == 2048
    with pytest.raises(ValueError, match='research_shared_limits_invalid'):
        SharedDevCoveerResearch(str(tmp_path), provider_id=provider, model_id=model,
                               limits=ResearchLimits(max_output_tokens=2049))
    clamped = scoped_research_config(model, provider_id=provider,
                                    limits=ResearchLimits(max_output_tokens=8192))
    assert clamped['provider'][provider]['models'][model]['limit']['output'] == 2048


def test_generated_installer_uses_same_mimo_limit_and_guard_without_inference(tmp_path, monkeypatch, capsys):
    import street_story.shared_devcoveer_research as shared
    tree = ast.parse((GUARD_SOURCE.parent / 'devcoveer_install.py').read_text())
    install = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'install_research_runtime')
    program = next(ast.literal_eval(node.value) for node in ast.walk(install)
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                   and target.id == 'program' for target in node.targets))
    h = Harness()
    h.config = scoped_research_config('mimo-v2.6-flash-free', directory=tmp_path)
    guard = tmp_path / Path(h.config['plugin'][0][0]).name
    guard.write_bytes(GUARD_SOURCE.read_bytes())
    backend = Backend(h, tmp_path)
    original = shared.SharedDevCoveerResearch
    created = []
    def factory(directory, **kwargs):
        adapter = original(directory, backend=backend, **kwargs)
        created.append(adapter)
        return adapter
    monkeypatch.setattr(shared, 'SharedDevCoveerResearch', factory)
    monkeypatch.setattr(sys, 'argv', ['installer', str(tmp_path), json.dumps(['mimo-v2.6-flash-free'])])
    exec(compile(program, '<generated research installer>', 'exec'), {})
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['guard_sha256']
    assert receipt['tool_boundary_enforced'] is True
    assert created[0].limits.max_output_tokens == 8192
    assert json.loads((tmp_path / 'opencode.json').read_text()) == scoped_research_config(
        'mimo-v2.6-flash-free', directory=tmp_path)
    assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)
    # An old profile must be explicitly reconciled/migrated by the integrator.
    legacy = scoped_research_config('mimo-v2.6-flash-free', directory=tmp_path,
                                   limits=ResearchLimits(max_output_tokens=2048))
    (tmp_path / 'opencode.json').write_text(json.dumps(legacy))
    before = len(backend.calls)
    with pytest.raises(RuntimeError, match='reconcile its active attempts'):
        exec(compile(program, '<generated research installer>', 'exec'), {})
    assert len(backend.calls) == before
    assert json.loads((tmp_path / 'opencode.json').read_text()) == legacy
