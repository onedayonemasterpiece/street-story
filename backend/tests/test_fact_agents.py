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


def test_agent_capsule_does_not_change_existing_reader_guard(tmp_path):
    normal = scoped_research_config('arbitrary-model', directory=tmp_path)
    limits = ResearchLimits(timeout_seconds=720, max_steps=4, max_output_tokens=4096)
    agent = scoped_research_config('arbitrary-model', directory=tmp_path, limits=limits, agent_research=True)
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
async def test_tool_loop_reads_real_document_mapping_and_preserves_version():
    import json
    from contextlib import asynccontextmanager
    from street_story.fact_agent_providers import run_tool_agent
    source = {'requested_url': 'https://source.example/article', 'final_url': 'https://source.example/article',
        'source_version_id': 'srcv1', 'normalized_text': 'Actual source article text.',
        'content_sha256': 'digest', 'read_status': 'complete'}
    candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
        'url': source['final_url'], 'quote': 'constructed in 1901', 'source_date': None}
    messages = [
        {'role': 'assistant', 'function_call': {'name': 'websearch', 'arguments': {'query': 'selected building history'}}},
        {'role': 'assistant', 'function_call': {'name': 'webfetch', 'arguments': {'url': source['final_url']}}},
        {'role': 'assistant', 'content': json.dumps({'facts': [candidate]})},
        {'role': 'assistant', 'content': json.dumps({'facts': [candidate]})}]
    fetched, finalized, saved = [], [], []
    async def fetch(urls, context):
        fetched.append((urls, context))
        return {source['requested_url']: source}
    async def search(query):
        return SimpleNamespace(grounding_sources=[{'url': source['requested_url']}])
    async def finalize(usage, status):
        finalized.append((usage, status))
    @asynccontextmanager
    async def admission(_binding, _workload):
        assert 1 <= _workload['max_provider_sends'] <= 4
        assert _workload['estimated_input_tokens'] > 0
        yield SimpleNamespace(finalize=finalize)
    async def completion(_lease, body, receipts, _purpose, _output, before):
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
    result = await run_tool_agent(adapter, AgentRoute('gigachat', 'test', 'https://api.example', giga), {},
        {'run_id': 'run1'}, binding={'attempt_id': 'one'}, guard=lambda: None)
    assert result['result']['facts'] == [candidate]
    assert result['sources'][0]['source_version_id'] == 'srcv1'
    assert fetched == [([source['requested_url']], {'research_run_id': 'run1'})]
    assert finalized[0][1] == 'completed'
    assert len(finalized) == 4
    assert sum(item[0]['input_tokens'] for item in finalized) == 40
    assert saved[-1]['phase'] == 'completed'


def test_provider_wire_schema_uses_supported_subset_host_stays_strict():
    from street_story.fact_agent_providers import TOOLS, wire_tools
    for original, wire in zip(TOOLS, wire_tools()):
        assert original['parameters']['additionalProperties'] is False
        assert 'additionalProperties' not in wire['parameters']
        assert all(set(prop) == {'type'} for prop in wire['parameters']['properties'].values())
        assert wire['parameters']['required'] == original['parameters']['required']
