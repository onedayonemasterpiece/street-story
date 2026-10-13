"""Agent send boundaries exercise the ordinary durable executor/key pool."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types

from street_story.errors import RetryableProviderError
from street_story.fact_agent_providers import AgentRoute, run_tool_agent
from street_story.opencode_research import ResearchUnavailable
from test_gemini_reliability import KEYS, ProviderError, pool


@pytest.mark.asyncio
@pytest.mark.parametrize('first_error', [404, 503, 'not_sent', 'unknown'])
async def test_agent_known_closed_or_not_sent_can_use_unused_key_but_unknown_cannot(tmp_path, first_error):
    key_pool, executor, clock = pool(tmp_path, keys=KEYS[:2])
    calls, reservations, saved = [], [], []
    reserve = key_pool.reserve
    def reserve_record(operation, excluded, timeout):
        key_id = reserve(operation, excluded, timeout)
        reservations.append((set(excluded), key_id))
        return key_id
    key_pool.reserve = reserve_record
    url = 'https://source.example/history'
    source = {'requested_url': url, 'final_url': url, 'source_version_id': 'own-version',
        'content_sha256': 'frozen-content', 'normalized_text': 'The building was constructed in 1901.'}
    candidate = {'text': 'Здание построено в 1901 году.', 'subject_scope': 'selected_building',
        'url': url, 'quote': 'constructed in 1901', 'source_date': None}
    successful_turns = 0
    async def generate(key, timeout, contents, config, **kwargs):
        nonlocal successful_turns
        calls.append(key)
        if len(calls) == 1:
            if first_error != 'not_sent':
                kwargs['before_provider_send']()
            if isinstance(first_error, int):
                raise ProviderError(first_error)
            if first_error == 'unknown':
                raise httpx.ReadTimeout('Controlled sent request without reply')
            error = RetryableProviderError('controlled_not_sent')
            error.provider_send_state = 'not_sent'
            raise error
        kwargs['before_provider_send']()
        if successful_turns == 0:
            part = types.Part(function_call=types.FunctionCall(name='websearch', args={'query': 'building history'}))
        elif successful_turns == 1:
            part = types.Part(function_call=types.FunctionCall(name='webfetch', args={'url': url}))
        else:
            part = types.Part(text=json.dumps({'facts': [candidate]}))
        successful_turns += 1
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role='model', parts=[part]))])
    async def search(query):
        return SimpleNamespace(grounding_sources=[{'url': url}])
    async def fetch(urls, context):
        assert urls == [url]
        return {url: source}
    client = SimpleNamespace(_generate=generate, _public_web_search=search, _fetch_page_documents=fetch,
        research_routes=[(key_pool.model, key_pool, None, executor)])
    adapter = SimpleNamespace(service=SimpleNamespace(store=SimpleNamespace(now=clock),
        settings=SimpleNamespace(redact=lambda text: text), providers=SimpleNamespace(gemini=client)),
        _checkpoint_receipt=lambda binding, receipt: saved.append(copy.deepcopy(receipt)))
    invoke = run_tool_agent(adapter, AgentRoute('google', key_pool.model, 'https://google.example', client), {},
        {'run_id': 'own-run'}, binding={'attempt_id': 'original-agent'}, guard=lambda: None)
    if first_error == 'unknown':
        with pytest.raises(ResearchUnavailable) as unknown:
            await invoke
        assert unknown.value.receipt['phase'] == 'unknown'
        assert unknown.value.receipt['last_request_sent'] is True
        assert unknown.value.receipt['provider_send_state'] == 'possibly_sent'
        assert len(calls) == len(reservations) == 1
        assert key_pool.snapshot()['cooling_down_keys'] == 1
    else:
        result = await invoke
        assert result['result'] == {'facts': [candidate]}
        assert result['receipt']['phase'] == 'completed'
        assert calls[:2] == list(KEYS[:2])
        assert reservations[1] == ({key_pool.ids[0]}, key_pool.ids[1])
        assert successful_turns == 4
        assert [row['name'] for row in result['receipt']['tool_receipts']] == ['websearch', 'webfetch']
        failed = next(row for row in saved if row.get('sdk_error'))
        assert failed['last_request_sent'] is False
        assert failed['provider_send_state'] == ('not_sent' if first_error == 'not_sent' else 'response_closed')
    assert key_pool.snapshot()['in_flight'] == 0
