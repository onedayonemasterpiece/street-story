"""Native tool receipts become immutable own reads through existing runtime paths."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.fact_agent_providers import AgentRoute, run_tool_agent
from street_story.opencode_research import ResearchLimits, ResearchUnavailable
from street_story.shared_devcoveer_research import (
    AGENT_GUARD_SOURCE, SharedDevCoveerResearch, guard_profile, scoped_research_config,
)
from test_headless_facts import fixture
from test_opencode_research import Harness
from test_shared_devcoveer_research import Backend


@pytest.mark.asyncio
async def test_native_model_receives_exact_failed_read_outcome_from_its_run(tmp_path):
    from street_story.research_runs import register_discovered_source
    service, job, _, reader, _ = await fixture(tmp_path)
    url = 'https://example.org/blocked/'
    received, saved = [], []
    async def blocked(urls, context):
        assert urls == [url] and context['research_run_id'] == 'headless-run'
        with service.store.tx() as db:
            register_discovered_source(db, run_id='headless-run', url=url.rstrip('/'), title='Blocked source',
                status='failed', error_code='http_403', now=service.store.now())
        return {}
    service.providers.gemini._fetch_page_documents = blocked
    class Native:
        async def run(self, **kwargs):
            received.append(await kwargs['callback']('webfetch', {'url': url}))
            kwargs['receipt'].update(phase='completed', provider_send_state='response_closed')
            return {'text': '{"facts":[]}', 'receipt': kwargs['receipt']}
    adapter = SimpleNamespace(service=service,
        _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
    try:
        await run_tool_agent(adapter, AgentRoute('codex_native', 'gpt-6-luna', 'native', Native()),
            {}, {'run_id': 'headless-run'}, binding={'attempt_id': 'own'}, guard=lambda: None)
        assert received == [{'documents': [], 'read_attempts': [{'requested_url': url,
            'status': 'failed', 'error_code': 'http_403', 'source_version_id': None}]}]
        assert saved[-1]['completed_tool_transcript'][0]['result'] == received[0]
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_native_continuation_receives_own_failed_leads_but_excludes_foreign_generation(tmp_path):
    from street_story.fact_agents import native_agent_context
    from street_story.headless_facts import HeadlessFacts
    service, job, _, reader, _ = await fixture(tmp_path)
    story, research, _ = HeadlessFacts(service)._snapshot(job, 'headless-run')
    try:
        with service.store.tx() as db:
            for attempt, generation, url in [('own', 0, 'https://example.org/own'),
                    ('foreign', 5, 'https://example.org/foreign')]:
                receipt = {'provider_id': 'codex_native', 'binding': {'attempt_id': attempt,
                    'photo_sha256': story['photo_sha256'], 'generation': generation},
                    'native_tool_calls': {'original-search': {'phase': 'completed',
                        'input': {'name': 'websearch'}, 'result': {'sources': [{'title': attempt, 'url': url}]}}}}
                db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?)', (attempt, attempt, story['id'], 'facts_agent_native', json.dumps(receipt), 1, 1))
        context = native_agent_context(service, story, research['visual_identity'], 'Continue actual source gaps', 'headless-run')
        assert context['prior_search_leads'] == [{'title': 'own', 'url': 'https://example.org/own',
            'original_attempt_id': 'own', 'kind': 'unverified_search_lead'}]
        assert context['prior_native_attempt_ids'] == ['own']
    finally:
        await reader.search_http.aclose()


def test_native_agent_mask_is_ordered_and_profile_bound_without_changing_legacy(tmp_path):
    legacy = scoped_research_config('configured-model')
    assert 'permission' not in legacy['agent']['plan']
    agent = scoped_research_config('configured-model', agent_research=True)
    assert list(agent['agent']['plan']['permission'].items()) == [
        ('*', 'deny'), ('websearch', 'allow'), ('webfetch', 'allow')]
    for mode, config in [(False, legacy), (True, agent)]:
        client = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model',
            agent_research=mode, require_guard=False)
        assert client.profile_fingerprint == hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    prior_agent = copy.deepcopy(agent)
    del prior_agent['agent']['plan']['permission']
    assert hashlib.sha256(json.dumps(prior_agent, sort_keys=True).encode()).hexdigest() != client.profile_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize('sorted_json', [False, True])
@pytest.mark.parametrize('later_rule', [None,
    {'permission': 'external_directory', 'pattern': '*', 'action': 'allow'},
    {'permission': '*', 'pattern': '*', 'action': 'allow'},
    {'permission': 'read', 'pattern': '*', 'action': 'allow'},
    {'permission': 'grep', 'pattern': '*.md', 'action': 'deny'},
    {'permission': 'websearch', 'pattern': '*', 'action': 'deny'},
    {'permission': 'r*', 'pattern': '*', 'action': 'allow'},
    {'permission': 'unknown_future_tool', 'pattern': '*', 'action': 'allow'}])
async def test_native_agent_resolved_permissions_require_closed_tail_before_dispatch(tmp_path, later_rule, sorted_json):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, agent_research=True)
    h.config = scoped_research_config(client.model_id, agent_research=True)
    h.config = json.loads(json.dumps(h.config, sort_keys=sorted_json))
    closed_tail = [{'permission': name, 'pattern': '*', 'action': action}
        for name, action in h.config['agent']['plan']['permission'].items()]
    backend.agent_permission = [
        {'permission': '*', 'pattern': '*', 'action': 'allow'},
        {'permission': 'read', 'pattern': '*', 'action': 'allow'}, *closed_tail]
    if later_rule is not None:
        backend.agent_permission.append(later_rule)
    if later_rule is not None and later_rule['permission'] != 'external_directory':
        with pytest.raises(ResearchUnavailable, match='research_agent_tool_permissions_unverified'):
            await client._run('agent_research', 'Controlled tools', {'attempt_id': 'mask-control'}, {'type': 'object'})
    else:
        proof = await client._attest(None, 'agent_research')
        assert proof['resolved_agent_tool_permission_tail'] == closed_tail
    assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.parametrize('context', [None, 0, -1, True, '1048576'])
def test_bounded_native_config_requires_actual_positive_catalog_context(context):
    with pytest.raises(ValueError, match='research_catalog_context_required'):
        scoped_research_config('configured-model', limits=ResearchLimits(max_output_tokens=4096), model_context=context)


def test_native_config_includes_context_with_output_and_preserves_unbounded_default():
    assert 'provider' not in scoped_research_config('configured-model')
    config = scoped_research_config('configured-model', limits=ResearchLimits(max_output_tokens=4096), model_context=1048576)
    assert config['provider']['opencode']['models']['configured-model']['limit'] == {'context': 1048576, 'output': 4096}


@pytest.mark.parametrize('names', ['connector', ['', 'valid'], ['../connector'], [None], {'connector': False}])
def test_native_capsule_mcp_override_accepts_only_observed_valid_name_list(names):
    with pytest.raises(ValueError, match='research_disabled_mcp_names_invalid'):
        scoped_research_config('configured-model', disabled_mcp=names)


def test_native_capsule_disables_only_named_mcp_and_pins_override_in_profile(tmp_path):
    assert 'mcp' not in scoped_research_config('configured-model')
    config = scoped_research_config('configured-model', disabled_mcp=['kalinigrad-base'], agent_research=True)
    assert config['mcp'] == {'kalinigrad-base': {'enabled': False}}
    legacy_original = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').profile_fingerprint
    original = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model', agent_research=True).profile_fingerprint
    (tmp_path / 'opencode.json').write_text(json.dumps(config))
    client = SharedDevCoveerResearch(str(tmp_path), model_id='configured-model', agent_research=True)
    assert client.disabled_mcp == ('kalinigrad-base',)
    assert client.profile_fingerprint != original
    assert SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').profile_fingerprint == legacy_original
    config['mcp']['active-other'] = {'enabled': None}
    (tmp_path / 'opencode.json').write_text(json.dumps(config))
    assert SharedDevCoveerResearch(str(tmp_path), model_id='configured-model').disabled_mcp == ('kalinigrad-base',)


@pytest.mark.asyncio
@pytest.mark.parametrize('context', [200000, 199999])
async def test_native_attestation_checks_actual_catalog_context_before_send(tmp_path, context):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, limits=ResearchLimits(max_output_tokens=4096))
    h.config['provider']['opencode']['models']['mimo-v2.6-flash-free']['limit']['context'] = context
    client.model_context = context
    if context == 200000:
        result = await client.search_articles('Facade history', {'attempt_id': 'control'})
        assert result['receipt']['isolation']['model_context'] == context
        assert len(h.sends) == 1
    else:
        with pytest.raises(ResearchUnavailable, match='research_model_context_unverified'):
            await client.search_articles('Facade history', {'attempt_id': 'control'})
        assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.asyncio
async def test_native_attestation_refuses_output_above_unscoped_catalog_limit(tmp_path):
    from test_shared_devcoveer_research import setup
    h, backend, client = setup(tmp_path, limits=ResearchLimits(max_output_tokens=65536))
    with pytest.raises(ResearchUnavailable, match='research_model_output_not_bounded'):
        await client.search_articles('Facade history', {'attempt_id': 'control'})
    assert ('GET', '/config/providers', None) in [call[:3] for call in backend.calls]
    assert not h.sends and not any(method == 'POST' for method, *_ in backend.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,category', [(400, 'validation'), (500, 'unavailable')])
async def test_native_closed_http_error_retains_safe_status_and_binding_without_private_body(tmp_path, status, category):
    class OpenCodeError(RuntimeError):
        def __init__(self):
            super().__init__('private provider body must not enter receipt')
            self.status, self.category = status, category
    class BadBackend:
        async def request(self, *args, **kwargs):
            raise OpenCodeError()
    h = Harness()
    client = SharedDevCoveerResearch(str(tmp_path), backend=BadBackend(), require_guard=False,
        model_id='configured-model', admission=h.admission, checkpoint=h.checkpoint)
    with pytest.raises(ResearchUnavailable) as failed:
        await client._run('facts', 'Control prompt', {'attempt_id': 'original-control'}, {'type': 'object'})
    assert failed.value.receipt['transport_status'] == status
    assert failed.value.receipt['transport_category'] == category
    assert failed.value.receipt['transport_response_closed'] is True
    assert failed.value.receipt['binding']['attempt_id'] == 'original-control'
    assert not failed.value.receipt.get('session_id') and not h.sends
    assert 'private' not in json.dumps(failed.value.receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_end', [False, True])
async def test_actual_native_tool_loop_versions_only_its_completed_read_output(tmp_path, missing_end):
    service, job, _, reader, fetches = await fixture(tmp_path)
    try:
        capsule = tmp_path / 'capsule'
        capsule.mkdir()
        h = Harness()
        limits = ResearchLimits(timeout_seconds=120, max_steps=4, max_output_tokens=4096)
        h.config = scoped_research_config('mimo-v2.6-flash-free', limits=limits, directory=capsule,
            agent_research=True, model_context=200000)
        (capsule / 'opencode.json').write_text(json.dumps(h.config))
        profile = guard_profile(capsule, limits, agent_research=True)
        (capsule / profile[0].split('/')[-1]).write_bytes(AGENT_GUARD_SOURCE.read_bytes())
        url, text = 'https://archive.example/native', 'The building was constructed in 1901. Exact native output.'
        candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
            'url': url, 'quote': 'constructed in 1901', 'source_date': None}
        h.result = {'facts': [candidate, {**candidate, 'url': 'https://archive.example/unread'}]}
        original_messages = h.messages
        def messages():
            result = original_messages()
            if len(result) > 1:
                result[-1]['parts'][:0] = [
                    {'type': 'tool', 'tool': 'websearch', 'callID': 'search-own', 'state': {
                        'status': 'completed', 'input': {'query': 'building history', 'numResults': 5},
                        'output': 'Title: History\nURL: '+url+'\nText: discovery only\n', 'time': {'end': 1000}}},
                    {'type': 'tool', 'tool': 'webfetch', 'callID': 'read-own', 'state': {
                        'status': 'completed', 'input': {'url': url, 'format': 'text'}, 'output': text,
                        'metadata': {'truncated': True}, 'time': {} if missing_end else {'end': 2000}}}]
            return result
        h.messages = messages
        backend = Backend(h, capsule)
        client = SharedDevCoveerResearch(str(capsule), backend=backend, model_id='mimo-v2.6-flash-free',
            limits=limits, agent_research=True, admission=h.admission, checkpoint=h.checkpoint)
        saved = []
        adapter = SimpleNamespace(service=service, _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
        invoke = run_tool_agent(adapter, AgentRoute('opencode', client.model_id, client.endpoint, client, str(capsule)),
            {}, {'run_id': 'headless-run'}, binding={'attempt_id': 'native-control', 'story_id': job['story_id'], 'purpose': 'facts'},
            guard=lambda: None)
        if missing_end:
            with pytest.raises(ResearchUnavailable, match='agent_result_no_valid_candidates'):
                await invoke
            assert saved[-1]['tool_receipts'][1]['completed_at'] is None
        else:
            result = await invoke
            receipt = result['receipt']
            assert receipt['phase'] == 'completed' and receipt['provider_send_state'] == 'response_closed'
            assert receipt['model_attempts'] and receipt['isolation']['tool_boundary_enforced'] is True
            assert receipt['tool_receipts'][1]['arguments'] == {'url': url}
            assert receipt['tool_receipts'][1]['completed_at'] == 2
            assert receipt['native_tool_receipts'][1]['state']['input']['format'] == 'text'
            assert result['result']['facts'] == [candidate]
            assert receipt['rejected_candidates'][0]['reason'] == 'agent_source_not_read'
            assert receipt['sources'][0]['http_status_verified'] is False
        with service.store.connection() as db:
            rows = db.execute("SELECT * FROM source_versions WHERE requested_url=?", (url,)).fetchall()
            assert len(rows) == (0 if missing_end else 1)
            if rows:
                assert rows[0]['normalized_text'] == text
                assert rows[0]['content_sha256'] == hashlib.sha256(text.encode()).hexdigest()
                assert rows[0]['read_status'] == 'native_tool_output_partial' and rows[0]['http_status'] == 0
        assert not fetches and len(h.sends) == 1
    finally:
        await reader.search_http.aclose()
