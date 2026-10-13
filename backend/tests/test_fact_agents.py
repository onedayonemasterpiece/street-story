"""Independent agents: fresh input, durable sends, explicit role qualification."""
from types import SimpleNamespace

import pytest

from street_story.fact_agents import agent_context, configured_routes, route_plan
from street_story.fact_agent_providers import AgentRoute, validate_result
from street_story.shared_devcoveer_research import guard_profile, scoped_research_config
from street_story.opencode_research import ResearchLimits


def route(provider, model):
    return AgentRoute(provider, model, 'https://example.org', object())


def test_plan_needs_independent_services_and_does_not_require_named_models():
    assert route_plan([route('opencode', 'a'), route('opencode', 'b')]) == []
    rows = [route('opencode', 'new-model'), route('opencode', 'another'),
        route('google', 'custom-google'), route('gigachat', 'custom-giga'), route('google', 'last')]
    plan = route_plan(rows)
    assert [row.provider_id for row in plan[:2]] == ['opencode', 'google']
    assert len(plan) == 4
    assert len({row.route_id for row in plan}) == 4


def test_agent_input_cannot_hydrate_old_poi_facts_or_photo_camera():
    identity = {'candidate_id': 'way42', 'city': 'TargetCity', 'address': 'TargetStreet 7',
        'facts': [{'text': 'Old claim'}], 'sources': [{'url': 'https://old.example'}],
        'camera_hints': {'city': 'OtherCity'}, 'latitude': 1, 'longitude': 2}
    context = agent_context(identity, 'History', 'run1', 1791849600)
    assert context['selected_object'] == {'candidate_id': 'way42', 'city': 'TargetCity', 'address': 'TargetStreet 7'}
    assert context['review_as_of_date_utc'] == '2026-10-13'


def test_candidate_format_has_no_semantic_regex_gate():
    item = {'text': 'Сведения о комплексе по состоянию на 2025 год.', 'subject_scope': 'complex_or_site',
        'url': 'https://example.org/article', 'quote': 'Exact relevant source excerpt', 'source_date': '2025-04-01'}
    assert validate_result({'facts': [item]})['facts'] == [item]
    with pytest.raises(ValueError, match='quote_too_long'):
        validate_result({'facts': [{**item, 'quote': ' '.join(['word']*26)}]})


@pytest.mark.parametrize('candidate_url', ['https://example.org/article?value=/',
    'https://example.org/article?value=#/', 'https://EXAMPLE.org/article?value=',
    'HTTPS://example.org/article?value='])
def test_read_alias_never_normalizes_query_fragment_host_or_scheme(candidate_url):
    from street_story.fact_agent_providers import partition_result
    item = {'text': 'Факт', 'subject_scope': 'selected_building', 'url': candidate_url,
        'quote': 'Short quote', 'source_date': None}
    result, rejected = partition_result({'facts': [item]}, fetched={'https://example.org/article?value='})
    assert result == {'facts': []}
    assert rejected[0]['reason'] == 'agent_source_not_read'


def test_agent_capsule_does_not_change_existing_reader_guard(tmp_path):
    normal = scoped_research_config('arbitrary-model', directory=tmp_path)
    limits = ResearchLimits(timeout_seconds=720, max_steps=4, max_output_tokens=4096)
    agent = scoped_research_config('arbitrary-model', directory=tmp_path, limits=limits,
        agent_research=True, model_context=1048576)
    assert normal['agent']['plan']['steps'] == 3
    assert agent['agent']['plan']['steps'] == 4
    assert normal['plugin'] != agent['plugin']
    assert normal['plugin'] == [guard_profile(tmp_path, ResearchLimits())]
    assert agent['snapshot'] is False and agent['share'] == 'disabled'


def test_old_reader_qualification_is_not_agent_tool_proof():
    store = SimpleNamespace(cache_get=lambda _key: {'extractors': [{'model_id': 'GigaChat-2',
        'semantic_contract_verified': True, 'source_subject_negative_verified': True}]})
    client = SimpleNamespace(binding={'model': 'GigaChat-2'})
    settings = SimpleNamespace(fact_agent_routes=({'provider_id': 'gigachat', 'model_id': 'GigaChat-2'},),
        gemini_keys=())
    adapter = SimpleNamespace(service=SimpleNamespace(settings=settings, store=store,
        providers=SimpleNamespace(gemini=SimpleNamespace(research_routes=[]))), giga=client)
    assert configured_routes(adapter) == []
    assert len(configured_routes(adapter, include_unqualified=True)) == 1


def test_google_mandatory_tool_strategy_invalidates_only_its_old_role_proof():
    import hashlib
    import json
    from street_story.fact_agent_providers import INSTRUCTIONS, RESULT_SCHEMA, TOOLS, wire_tools
    from street_story.fact_agents import QUALIFICATION_KEY
    gemini = SimpleNamespace(research_routes=[('configured-google', None, None, None)])
    google = AgentRoute('google', 'configured-google', 'https://generativelanguage.googleapis.com', gemini)
    giga_client = SimpleNamespace(binding={'model': 'GigaChat-2'})
    giga = AgentRoute('gigachat', 'GigaChat-2', 'https://api.giga.chat/v1/chat/completions', giga_client)
    manifest = {'instructions': INSTRUCTIONS, 'tools': TOOLS, 'schema': RESULT_SCHEMA, 'wire_tools': wire_tools()}
    def old_fingerprint(contract):
        return hashlib.sha256(json.dumps({**manifest, 'transport_contract': contract}, sort_keys=True).encode()).hexdigest()
    assert giga.profile_fingerprint == old_fingerprint('agent-tools-per-turn-admission-v3-strict-final')
    proofs = [{'route_id': row.route_id, 'profile_sha256': fingerprint, 'contract': QUALIFICATION_KEY,
        'tool_access_verified': True, 'tool_safety_verified': True, 'qualification_receipts': ['controlled-receipt']}
        for row, fingerprint in [(google, old_fingerprint('agent-tools-per-turn-admission-v6-separate-read-url-strict-final')), (giga, giga.profile_fingerprint)]]
    settings = SimpleNamespace(fact_agent_routes=(), gemini_keys=('controlled-key',))
    adapter = SimpleNamespace(giga=giga_client, service=SimpleNamespace(settings=settings,
        providers=SimpleNamespace(gemini=gemini), store=SimpleNamespace(cache_get=lambda _key: {'routes': proofs})))
    assert [row.provider_id for row in configured_routes(adapter)] == ['gigachat']
    assert len(configured_routes(adapter, include_unqualified=True)) == 2
    proofs[0]['profile_sha256'] = google.profile_fingerprint
    assert [row.provider_id for row in configured_routes(adapter)] == ['google', 'gigachat']


@pytest.mark.asyncio
async def test_unknown_synchronous_binding_is_not_sent_or_rewritten():
    from street_story.fact_agent_providers import run_tool_agent
    from street_story.opencode_research import ResearchUnavailable
    writes = []
    adapter = SimpleNamespace(_checkpoint_receipt=lambda *args: writes.append(args))
    binding = {'attempt_id': 'original', 'phase': 'unknown'}
    with pytest.raises(ResearchUnavailable, match='original_unaddressable'):
        await run_tool_agent(adapter, route('google', 'any-model'), {}, {'run_id': 'run'},
            binding=binding, guard=lambda: None)
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['valid', 'mixed', 'search_error', 'unsafe_error', 'empty_then_valid', 'slash_alias'])
async def test_tool_loop_reads_real_document_mapping_and_preserves_version(variant, monkeypatch):
    import json
    import sys
    from types import ModuleType
    from contextlib import asynccontextmanager
    from street_story.fact_agent_providers import run_tool_agent
    # This is a controlled loop/admission fixture, independent of the private
    # deployed quota SDK; exercise the complete envelope passed to its estimator.
    sdk = ModuleType('ai_resource_control.client')
    estimated_bodies = []
    def estimate(body):
        estimated_bodies.append(body)
        return len(json.dumps(body, ensure_ascii=False).encode())
    sdk.estimate_input_tokens = estimate
    monkeypatch.setitem(sys.modules, 'ai_resource_control.client', sdk)
    source = {'requested_url': 'https://source.example/article', 'final_url': 'https://source.example/article',
        'source_version_id': 'srcv1', 'normalized_text': 'Actual source article text.',
        'content_sha256': 'digest', 'read_status': 'complete'}
    candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
        'url': source['final_url'], 'quote': 'constructed in 1901', 'source_date': None}
    if variant == 'slash_alias':
        candidate = {**candidate, 'url': candidate['url'] + '/'}
    raw_result = {'facts': [candidate]}
    if variant == 'mixed':
        raw_result['facts'].extend([
            {**candidate, 'quote': ' '.join(['word'] * 29)},
            {**candidate, 'subject_scope': 'literal building name'},
            {**candidate, 'url': 'https://unread.example/article'}])
    messages = [
        {'role': 'assistant', 'function_call': {'name': 'websearch', 'arguments': {'query': 'selected building history'}}},
        {'role': 'assistant', 'function_call': {'name': 'webfetch', 'arguments': {'url': source['final_url']}}},
        {'role': 'assistant', 'content': json.dumps(raw_result)},
        {'role': 'assistant', 'content': json.dumps(raw_result)}]
    fetched, finalized, saved = [], [], []
    if variant == 'empty_then_valid':
        messages.insert(1, {'role': 'assistant', 'function_call': {'name': 'websearch',
            'arguments': {'query': 'different selected building query'}}})
    searches = []
    async def fetch(urls, context):
        fetched.append((urls, context))
        return {source['requested_url']: source}
    async def search(query):
        searches.append(query)
        if variant == 'empty_then_valid' and len(searches) == 1:
            from street_story.errors import RetryableProviderError
            raise RetryableProviderError('public_web_search_empty')
        if variant in {'search_error', 'unsafe_error'}:
            from street_story.errors import RetryableProviderError
            raise RetryableProviderError('grounded_search_sources_unavailable' if variant == 'search_error'
                else 'private provider detail must not enter error_code')
        return SimpleNamespace(grounding_sources=[{'url': source['requested_url']}])
    async def finalize(usage, status):
        finalized.append((usage, status))
    @asynccontextmanager
    async def admission(_binding, _workload):
        assert 1 <= _workload['max_provider_sends'] <= 4
        assert _workload['estimated_input_tokens'] > 0
        yield SimpleNamespace(finalize=finalize)
    async def completion(_lease, body, receipts, _purpose, _output, before):
        if variant == 'empty_then_valid' and len(messages) == 4:
            tool_result = json.loads(body['messages'][-1]['content'])
            assert tool_result == {'sources': [], 'error_code': 'public_web_search_empty'}
            assert body['function_call'] == {'name': 'websearch'}
        if len(messages) == 1:
            assert body['function_call'] == 'none'
            assert body['response_format']['strict'] is True
            assert body['response_format']['schema']['properties']['facts']['items']['properties']['subject_scope']['enum'] == ['selected_building', 'complex_or_site']
        await before({'request_body_sha256': 'wire'})
        receipts.append({'status': 'succeeded', 'http_status': 200,
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
        return messages.pop(0), 'stop'
    giga = SimpleNamespace(binding={'provider': 'gigachat', 'model': 'test'}, admission=admission, _completion=completion)
    gemini = SimpleNamespace(_fetch_page_documents=fetch, _public_web_search=search)
    adapter = SimpleNamespace(service=SimpleNamespace(store=SimpleNamespace(now=lambda: 10),
        providers=SimpleNamespace(gemini=gemini)), _checkpoint_receipt=lambda _binding, receipt: saved.append(dict(receipt)),
        attempt=lambda _story, _role, unit: ({'attempt_id': 'turn'+str(unit['turn'])}, None))
    if variant in {'search_error', 'unsafe_error'}:
        from street_story.opencode_research import ResearchUnavailable
        expected_code = 'grounded_search_sources_unavailable' if variant == 'search_error' else 'RetryableProviderError'
        with pytest.raises(ResearchUnavailable, match=expected_code) as failure:
            await run_tool_agent(adapter, AgentRoute('gigachat', 'test', 'https://api.example', giga), {},
                {'run_id': 'run1'}, binding={'attempt_id': 'one'}, guard=lambda: None)
        assert failure.value.receipt['error_code'] == expected_code
        assert saved[-1]['phase'] == 'failed'
        assert len(finalized) == 1 and not fetched
        return
    result = await run_tool_agent(adapter, AgentRoute('gigachat', 'test', 'https://api.example', giga), {},
        {'run_id': 'run1'}, binding={'attempt_id': 'one'}, guard=lambda: None)
    assert result['result']['facts'] == [{**candidate, 'url': source['final_url']}]
    assert result['sources'][0]['source_version_id'] == 'srcv1'
    assert fetched == [([source['requested_url']], {'research_run_id': 'run1'})]
    assert finalized[0][1] == 'completed'
    assert len(finalized) == (5 if variant == 'empty_then_valid' else 4)
    assert sum(item[0]['input_tokens'] for item in finalized) == len(finalized) * 10
    assert saved[-1]['phase'] == 'completed'
    assert len(estimated_bodies) == len(finalized)
    assert all(body['model'] == 'test' and body['messages'] for body in estimated_bodies)
    assert saved[-1]['raw_result'] == raw_result
    assert json.loads(saved[-1]['raw_result_text']) == raw_result
    assert [item['reason'] for item in saved[-1]['rejected_candidates']] == ([
        'agent_quote_too_long', 'agent_candidate_malformed', 'agent_source_not_read'] if variant == 'mixed' else [])
    if variant == 'mixed':
        assert saved[-1]['raw_result']['facts'][1]['quote'].split() == ['word'] * 29
    if variant == 'empty_then_valid':
        assert searches == ['selected building history', 'different selected building query']
        assert [row['name'] for row in saved[-1]['tool_receipts']] == ['websearch', 'websearch', 'webfetch']
    if variant == 'slash_alias':
        assert saved[-1]['raw_result']['facts'][0]['url'].endswith('/article/')


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_first', [False, True])
@pytest.mark.parametrize('extra_read', [False, True])
async def test_google_requires_search_then_own_fetch_before_automatic_final(empty_first, extra_read):
    import json
    from google.genai import types
    from street_story.fact_agent_providers import run_tool_agent
    source = {'requested_url': 'https://source.example/article', 'final_url': 'https://source.example/article',
        'source_version_id': 'version', 'content_sha256': 'content', 'normalized_text': 'Read actual source text.'}
    candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
        'url': source['final_url'], 'quote': 'built in 1901', 'source_date': None}
    second_url = 'https://source.example/second'
    stages, searches, saved, reads = [], [], [], []
    preliminary = '```json\n' + json.dumps({'facts': [candidate]}) + '\n```'
    async def search(query):
        searches.append(query)
        if empty_first and len(searches) == 1:
            from street_story.errors import RetryableProviderError
            raise RetryableProviderError('public_web_search_no_https_results')
        return SimpleNamespace(grounding_sources=[{'url': source['final_url']}])
    async def fetch(urls, context):
        assert urls[0] in {source['final_url'], second_url} and context == {'research_run_id': 'run1'}
        reads.extend(urls)
        return {urls[0]: {**source, 'requested_url': urls[0], 'final_url': urls[0]}}
    async def generate(_key, _timeout, contents, config, **kwargs):
        strict_final = config.tool_config is None
        if strict_final:
            from jsonschema import Draft202012Validator
            stages.append('strict_final')
            assert config.tools is None
            assert config.response_mime_type == 'application/json'
            wire = config.model_dump(mode='json', exclude_none=True)
            schema = wire['response_json_schema']
            assert schema['properties']['facts']['items']['properties']['url']['enum'] == sorted(reads)
            properties = schema['properties']['facts']['items']['properties']
            assert properties['subject_scope']['type'] == 'string'
            assert all('minLength' not in properties[key] and 'maxLength' not in properties[key]
                for key in ('text', 'quote', 'url'))
            assert Draft202012Validator(schema).is_valid({'facts': [candidate]})
            assert not Draft202012Validator(schema).is_valid({'facts': [{**candidate, 'url': 'https://source.example/aricle'}]})
            assert contents[-2].parts[0].text == preliminary
            assert any(part.function_response for content in contents for part in content.parts)
            forced = None
        else:
            tool_config = config.tool_config.function_calling_config
            forced = (tool_config.allowed_function_names or [None])[0]
            stages.append(forced)
            assert tool_config.mode.value == ('ANY' if forced else 'AUTO')
            assert config.tools and config.response_mime_type is None and config.response_json_schema is None
        kwargs['before_provider_send']({'request_body_sha256': 'wire'})
        if forced:
            args = {'query': 'different query' if searches else 'first query'} if forced == 'websearch' else {'url': source['final_url']}
            part = types.Part(function_call=types.FunctionCall(name=forced, args=args))
        elif strict_final:
            part = types.Part(text=json.dumps({'facts': [candidate]}))
        elif extra_read and second_url not in reads:
            part = types.Part(function_call=types.FunctionCall(name='webfetch', args={'url': second_url}))
        else:
            part = types.Part(text=preliminary)
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role='model', parts=[part]))])
    async def execute(_operation, call, **_kwargs):
        return await call('controlled-key', 60)
    gemini = SimpleNamespace(_public_web_search=search, _fetch_page_documents=fetch, _generate=generate,
        research_routes=[('configured-google', None, None, SimpleNamespace(execute=execute))])
    adapter = SimpleNamespace(service=SimpleNamespace(store=SimpleNamespace(now=lambda: 10), settings=SimpleNamespace(redact=str),
        providers=SimpleNamespace(gemini=gemini)), _checkpoint_receipt=lambda _binding, receipt: saved.append(dict(receipt)))
    result = await run_tool_agent(adapter, AgentRoute('google', 'configured-google', 'https://api.example', gemini), {},
        {'run_id': 'run1'}, binding={'attempt_id': 'one'}, guard=lambda: None)
    assert stages == (['websearch', 'websearch', 'webfetch'] if empty_first else ['websearch', 'webfetch']) + [None] * (2 if extra_read else 1) + ['strict_final']
    assert result['result']['facts'] == [{**candidate, 'url': source['final_url']}]
    assert saved[-1]['raw_result']['facts'][0]['url'] == candidate['url']
    assert saved[-1]['preliminary_result_text'] == preliminary
    assert saved[-1]['final_response_schema']['properties']['facts']['items']['properties']['url']['enum'] == sorted(reads)


def test_google_output_schema_preserves_host_contract_and_nullable_values():
    import copy
    from street_story.fact_agent_providers import CANDIDATE_SCHEMA, RESULT_SCHEMA, _google_output_schema
    original = copy.deepcopy(RESULT_SCHEMA)
    wire = _google_output_schema(RESULT_SCHEMA)
    properties = wire['properties']['facts']['items']['properties']
    assert properties['subject_scope'] == {'type': 'string', 'enum': ['selected_building', 'complex_or_site']}
    assert properties['source_date'] == {'type': ['string', 'null']}
    assert properties['url'] == {'type': 'string'}
    assert wire['properties']['facts']['maxItems'] == 24
    assert wire['additionalProperties'] is False
    assert wire['properties']['facts']['items']['additionalProperties'] is False
    assert RESULT_SCHEMA == original
    assert 'type' not in CANDIDATE_SCHEMA['properties']['subject_scope']
    assert CANDIDATE_SCHEMA['properties']['url']['maxLength'] == 2048
    wire['properties']['facts']['items']['properties']['subject_scope']['enum'].append('untrusted')
    assert RESULT_SCHEMA == original


def test_provider_wire_schema_uses_supported_subset_host_stays_strict():
    from street_story.fact_agent_providers import TOOLS, wire_tools
    for original, wire in zip(TOOLS, wire_tools()):
        assert original['parameters']['additionalProperties'] is False
        assert 'additionalProperties' not in wire['parameters']
        assert all(set(prop) == {'type'} for prop in wire['parameters']['properties'].values())
        assert wire['parameters']['required'] == original['parameters']['required']
