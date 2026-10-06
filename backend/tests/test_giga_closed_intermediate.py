"""A closed early answer reaches the structured final round of the same unit."""
import json

import httpx
import pytest

from street_story.errors import MalformedProviderResponse, RetryableProviderError
from test_gigachat_research import Admission, CAPSULE, Provider, client
from test_gigachat_dispatch_checkpoint import PAGE, setup, receipts


class EarlyClosed:
    def __init__(self, *, final='valid', intermediate_reason='stop'):
        self.base = Provider()
        self.requests = []
        self.final = final
        self.intermediate_reason = intermediate_reason

    async def __call__(self, request):
        if request.url.path == '/api/v2/oauth':
            return await self.base(request)
        body = json.loads(request.content)
        self.requests.append(body)
        if len(self.requests) == 1:
            return await self.base(request)
        if len(self.requests) == 2:
            assert body['function_call'] == 'auto' and 'response_format' not in body
            content = 'A closed but unstructured model answer.'
        else:
            assert len(self.requests) == 3
            assert body['function_call'] == 'none' and body['response_format']['strict'] is True
            assert body['messages'][-1]['role'] == 'user'
            if self.final == 'timeout':
                raise httpx.ReadTimeout('missing strict response', request=request)
            content = 'Still not valid JSON' if self.final == 'invalid' else json.dumps({
                'facts': [], 'source_matches_poi': True, 'source_content_valid': True,
                'continuation_needed': False})
        return httpx.Response(200, json={'model': 'GigaChat-2:2.0.30.01',
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': self.intermediate_reason if len(self.requests) == 2 else 'length' if self.final == 'length' else 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})


@pytest.mark.parametrize('reason', ['stop', 'length'])
@pytest.mark.asyncio
async def test_early_closed_invalid_answer_uses_remaining_strict_final_round(reason):
    provider, admission = EarlyClosed(intermediate_reason=reason), Admission()
    adapter = client(provider, admission)
    try:
        result = await adapter.research('Read verified facts', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert result['payload']['facts'] == []
    assert len(provider.requests) == len(admission.sends) == 3
    assert len(admission.bindings) == 1
    assert result['receipts'][1]['rejection_reason'] == ('semantic_json_invalid' if reason == 'stop' else 'closed_intermediate_length')
    if reason == 'stop':
        assert result['receipts'][1]['json_parser_error'] == 'JSONDecodeError'
    assert result['receipts'][1]['response_content_kind'] == 'str'
    assert result['receipts'][1]['response_content_bytes'] > 0
    assert 'A closed but unstructured model answer.' not in json.dumps(result['receipts'])


@pytest.mark.asyncio
async def test_invalid_strict_final_fails_closed_without_fourth_request():
    provider, admission = EarlyClosed(final='invalid'), Admission()
    adapter = client(provider, admission)
    try:
        with pytest.raises(MalformedProviderResponse, match='semantic_json_invalid'):
            await adapter.research('Read verified facts', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert len(provider.requests) == len(admission.sends) == 3
    assert len(admission.bindings) == 1


@pytest.mark.parametrize('reason', ['stop', 'length'])
@pytest.mark.asyncio
async def test_unknown_strict_final_is_not_repaired_or_resent_by_product(tmp_path, reason):
    provider, admission = EarlyClosed(final='timeout', intermediate_reason=reason), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    try:
        with pytest.raises(RetryableProviderError, match='gigachat_research_waiting'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read verified facts'})
        saved = receipts(adapter)
        assert saved[-1]['phase'] == 'unknown' and saved[-1]['retry_safe'] is False
        assert len(saved[-1]['inference_sends']) == 3
        with pytest.raises(RetryableProviderError, match='gigachat_attempt_outcome_unknown'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read verified facts'})
        assert len(provider.requests) == len(admission.sends) == 3
        assert len(receipts(adapter)) == 1
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
async def test_length_before_any_evidence_read_never_gets_a_repair_round():
    base, admission = Provider(), Admission()
    calls = []
    async def transport(request):
        if request.url.path == '/api/v2/oauth':
            return await base(request)
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Truncated before tool.'},
            'finish_reason': 'length'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 1500}})
    adapter = client(transport, admission)
    try:
        with pytest.raises(MalformedProviderResponse, match='missing_tool_roundtrip_or_truncated_output'):
            await adapter.research('Read verified facts', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert len(calls) == len(admission.sends) == 1


@pytest.mark.asyncio
async def test_strict_final_length_fails_without_fourth_round_or_using_truncated_text():
    provider, admission = EarlyClosed(final='length', intermediate_reason='length'), Admission()
    adapter = client(provider, admission)
    try:
        with pytest.raises(MalformedProviderResponse, match='missing_tool_roundtrip_or_truncated_output'):
            await adapter.research('Read verified facts', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert len(provider.requests) == len(admission.sends) == 3
    assert len(admission.bindings) == 1
    assert not any('A closed but unstructured model answer.' in str(body['messages']) for body in provider.requests)
