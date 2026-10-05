import json
import ssl
from contextlib import asynccontextmanager

import httpx
import pytest

from street_story.errors import MalformedProviderResponse, PermanentProviderError
from street_story.gigachat_research import ACCOUNT_SCOPE, GigaChatProviderError, GigaChatResearchClient


PASSAGE = 'В 2027 году планируют открыть выставку. Подготовка началась в 2026 году.'
CAPSULE = {'sources': [{'source_version_id': 'srcv_frozen', 'url': 'https://example.org/article',
                      'title': 'Plans', 'passages': [{'passage_id': 0, 'text': PASSAGE}]}]}
NOW = 1_800_000_000


@pytest.mark.asyncio
async def test_frozen_inventory_tool_reads_known_fact_beyond_first_fifty_without_refetch():
    provider, admission = Provider(), Admission()
    inventory = [{'fact_id': f'fact_{i}', 'text': f'Known claim {i}'} for i in range(71)]
    async def handle(request):
        if request.url.path == '/api/v2/oauth':
            return await provider(request)
        body = json.loads(request.content)
        if body['function_call'] == {'name': 'get_evidence'}:
            return await provider(request)
        if body['messages'][-1]['name'] == 'get_evidence':
            message = {'role': 'assistant', 'content': '', 'functions_state_id': 'inventory-state',
                       'function_call': {'name': 'get_known_facts', 'arguments': {'offset': 50}}}
            reason = 'function_call'
        else:
            page = json.loads(body['messages'][-1]['content'])
            assert page['total_count'] == 71 and page['has_more'] is False
            assert page['facts'][-1]['fact_id'] == 'fact_70'
            assert all('fact_70' not in item.get('content', '') for item in body['messages'][:2])
            fact = {'text': 'В 2027 году планируют открыть выставку.', 'claim_key': 'plan', 'confidence': 1,
                    'existing_fact_id': 'fact_70', 'source_version_id': 'srcv_frozen', 'passage_ids': [0],
                    'evidence_quotes': [PASSAGE], 'verdict': 'supported', 'atomic': True,
                    'support_complete': True, 'qualifiers_preserved': True, 'review_reason': 'Equivalent known plan.'}
            message = {'role': 'assistant', 'content': json.dumps({'facts': [fact], 'continuation_needed': False,
                        'source_matches_poi': True, 'source_content_valid': True})}
            reason = 'stop'
        return httpx.Response(200, json={'model': 'GigaChat-2:2.0.30.01',
            'choices': [{'message': message, 'finish_reason': reason}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 30}})
    adapter = client(handle, admission)
    try:
        result = await adapter.research('Check known claims', capsule={**CAPSULE,
            'known_fact_inventory': inventory[:50], '_known_fact_inventory': inventory,
            'context': {'known_inventory_complete': False, 'known_inventory_next_offset': 50}})
    finally:
        await adapter.aclose()
    assert result['payload']['facts'][0]['existing_fact_id'] == 'fact_70'
    assert result['tool_roundtrips'] == 2
    assert result['receipts'][1]['tool'] == 'get_known_facts'
    assert len(admission.sends) == 3


class Admission:
    def __init__(self, denied=False):
        self.denied, self.bindings, self.sends, self.finalized = denied, [], [], []

    @asynccontextmanager
    async def __call__(self, binding, workload):
        self.bindings.append((binding, workload))
        if self.denied:
            raise RuntimeError('shared_admission_denied')
        yield self

    async def before_send(self, metadata):
        self.sends.append(metadata)

    async def finalize(self, usage, status='completed'):
        self.finalized.append(usage)


class Provider:
    def __init__(self, *, expired=False, retry401=False, usage=True, quote=PASSAGE, tool='get_evidence', code=None):
        self.requests, self.chats, self.auths = [], 0, 0
        self.expired, self.retry401, self.usage, self.quote, self.tool, self.code = expired, retry401, usage, quote, tool, code

    async def __call__(self, request):
        self.requests.append(request)
        if request.url.path == '/api/v2/oauth':
            self.auths += 1
            assert request.headers['Authorization'] == 'Basic fake-secret'
            assert len(request.headers['RqUID']) == 36
            assert request.content == b'scope=GIGACHAT_API_PERS'
            return httpx.Response(200, json={'access_token': f'fake-token-{self.auths}',
                                            'expires_at': (NOW + (35 if self.expired else 1800)) * 1000})
        self.chats += 1
        if self.code:
            return httpx.Response(self.code, headers={'Retry-After': '17'}, json={'error': 'redacted'})
        if self.retry401 and self.chats == 1:
            return httpx.Response(401, json={'error': 'expired'})
        body = json.loads(request.content)
        assert body['model'] == 'GigaChat-2'
        assert body['functions'][0]['name'] == 'get_evidence'
        if body['function_call'] == {'name': 'get_evidence'}:
            message, reason = {'role': 'assistant', 'content': '', 'functions_state_id': 'state-1',
                               'function_call': {'name': self.tool, 'arguments': {
                                   'source_version_id': 'srcv_frozen', 'passage_ids': [0]}}}, 'function_call'
        else:
            assert body['messages'][-1]['role'] == 'function'
            assert body['messages'][-2]['functions_state_id'] == 'state-1'
            assert json.loads(body['messages'][-1]['content'])['passages'][0]['text'] == PASSAGE
            fact = {'text': 'В 2027 году планируют открыть выставку.', 'claim_key': 'exhibition-plan',
                    'source_version_id': 'srcv_frozen', 'passage_ids': [0], 'evidence_quotes': [self.quote],
                    'verdict': 'supported', 'atomic': True, 'support_complete': True,
                    'qualifiers_preserved': True, 'review_reason': 'The source describes a plan.', 'selected': True}
            message, reason = {'role': 'assistant', 'content': json.dumps({'facts': [fact], 'continuation_needed': False})}, 'stop'
        response = {'choices': [{'message': message, 'finish_reason': reason}], 'model': 'GigaChat-2:2.0.30.01'}
        if self.usage:
            response['usage'] = {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30,
                                 'precached_prompt_tokens': 3}
        return httpx.Response(200, headers={'x-request-id': f'request-{self.chats}'}, json=response)


def client(provider, admission, **options):
    return GigaChatResearchClient('fake-secret', admission=admission, credential_ref='GIGACHAT_BINDING_A',
                                  transport=httpx.MockTransport(provider), clock=lambda: NOW, **options)


@pytest.mark.asyncio
async def test_actual_protocol_tool_roundtrip_usage_and_preserved_plan():
    provider, admission = Provider(), Admission()
    adapter = client(provider, admission)
    try:
        result = await adapter.research('Найди сведения о выставке', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert result['actual_model'] == 'GigaChat-2:2.0.30.01'
    assert result['tool_roundtrips'] == 1
    assert result['payload']['facts'][0]['text'] == 'В 2027 году планируют открыть выставку.'
    assert result['payload']['facts'][0]['selected'] is False
    assert result['receipts'][0]['cache_usage'] == 3
    assert result['cost'] == 'unknown'
    assert len(admission.sends) == 2
    assert all(send['images'] == 0 and send['estimated_tokens'] > send['output_allowance'] for send in admission.sends)
    assert admission.finalized[0]['input_tokens'] == 40
    assert admission.finalized[0]['output_tokens'] == 20


@pytest.mark.asyncio
async def test_denied_shared_admission_sends_no_oauth_or_inference():
    provider, admission = Provider(), Admission(denied=True)
    adapter = client(provider, admission)
    try:
        with pytest.raises(RuntimeError, match='shared_admission_denied'):
            await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('options,capsule', [
    ({'modality': 'image'}, CAPSULE), ({'purpose': 'visual_comparison'}, CAPSULE),
    ({}, {**CAPSULE, 'images': ['photo.jpeg']}), ({}, {**CAPSULE, 'context': {'image_bytes': b'pixels'}}),
])
async def test_text_only_rejects_visual_before_admission_or_provider(options, capsule):
    provider, admission = Provider(), Admission()
    adapter = client(provider, admission)
    try:
        with pytest.raises(PermanentProviderError, match='text_only_route'):
            await adapter.research('Compare', capsule=capsule, **options)
    finally:
        await adapter.aclose()
    assert admission.bindings == [] and provider.requests == []


@pytest.mark.asyncio
async def test_oauth401_renews_once_and_records_each_admitted_attempt():
    provider, admission = Provider(retry401=True), Admission()
    adapter = client(provider, admission)
    try:
        result = await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert provider.auths == 2 and provider.chats == 3
    assert result['receipts'][0]['status'] == 'credential_expired'
    assert result['tool_roundtrips'] == 1
    assert len(admission.sends) == 3
    assert admission.finalized[0]['usage_known'] is False


@pytest.mark.asyncio
async def test_expiring_token_refreshes_before_next_inference():
    provider, admission = Provider(expired=True), Admission()
    adapter = client(provider, admission)
    try:
        await adapter.research('Read', capsule=CAPSULE)
        adapter._clock = lambda: NOW + 10
        await adapter.research('Continue', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert provider.auths == 3  # Second run's token has <30s left at both sends.


@pytest.mark.asyncio
async def test_forged_quote_date_is_rejected_without_semantic_regex():
    provider, admission = Provider(quote='В 2026 году открыли выставку.'), Admission()
    adapter = client(provider, admission)
    try:
        result = await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert result['raw_candidate_count'] == 1
    assert result['payload']['facts'] == []
    assert result['binding_rejections'] == [{'incoming_index': 0, 'reason': 'literal_passage_binding_invalid'}]


@pytest.mark.asyncio
async def test_unknown_usage_is_explicit_and_not_fake_zero_cost():
    provider, admission = Provider(usage=False), Admission()
    adapter = client(provider, admission)
    try:
        result = await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert result['receipts'][0]['usage'] is None
    assert result['receipts'][0]['cache_usage'] == 'unknown'
    assert admission.finalized[0]['input_tokens'] is None
    assert admission.finalized[0]['cost'] == 'unknown'


@pytest.mark.asyncio
async def test_unexposed_tool_is_not_executed():
    provider, admission = Provider(tool='bash'), Admission()
    adapter = client(provider, admission)
    try:
        with pytest.raises(MalformedProviderResponse, match='tool_outside_scope'):
            await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert provider.chats == 1 and admission.finalized[0]['status'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('code,category', [(429, 'provider_quota'), (503, 'backend_unavailable'), (403, 'credential_failure')])
async def test_failures_classified_without_key_hopping_or_tight_retry(code, category):
    provider, admission = Provider(code=code), Admission()
    adapter = client(provider, admission)
    try:
        with pytest.raises(GigaChatProviderError) as error:
            await adapter.research('Read', capsule=CAPSULE)
    finally:
        await adapter.aclose()
    assert error.value.category == category and error.value.retry_at == NOW + 17
    assert provider.chats == 1 and admission.finalized[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_verified_tls_and_same_account_scope_across_credential_refs():
    provider, admission = Provider(), Admission()
    adapters = [GigaChatResearchClient('fake-secret', admission=admission, credential_ref=f'BINDING_{i}',
                transport=httpx.MockTransport(provider), clock=lambda: NOW) for i in range(3)]
    try:
        for adapter in adapters:
            assert adapter.ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert adapter.ssl_context.check_hostname is True
            await adapter.research('Read', capsule=CAPSULE)
    finally:
        for adapter in adapters:
            await adapter.aclose()
    assert {binding['account_scope'] for binding, _ in admission.bindings} == {ACCOUNT_SCOPE}


def test_invalid_ca_fails_closed():
    with pytest.raises(FileNotFoundError):
        GigaChatResearchClient('fake-secret', admission=Admission(), credential_ref='A',
                              ca_bundle_file='/nonexistent/gigachat-ca.pem')
