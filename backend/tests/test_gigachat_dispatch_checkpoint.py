"""Durable Giga dispatch markers distinguish local failures from unknown sends."""
import json
from contextvars import ContextVar

import httpx
import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.research_adapter import ProductResearchAdapter
from test_gigachat_research import Admission, PASSAGE, Provider, client
from test_research_control import fixture


PAGE = {'source_version_id': 'srcv_frozen', 'source_url': 'https://example.org/article',
        'source_ref': 'source', 'evidence_passages': [{'passage_id': 0, 'text': PASSAGE}],
        '_unit_id': 'page-unit'}


def setup(tmp_path, provider, admission):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter._active_binding = ContextVar('giga-dispatch-test', default=None)
    adapter.giga = client(provider, admission)
    story = {'id': sid, 'photo_sha256': photo, '_fact_research_control_revision': 0}
    return adapter, story


def receipts(adapter):
    with adapter.service.store.connection() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT receipt_json FROM research_provider_attempts WHERE role='facts_gigachat' ORDER BY created_at,rowid")]


class EmptyFindings(Provider):
    async def __call__(self, request):
        result = await super().__call__(request)
        if request.url.path != '/api/v2/oauth' and result.status_code == 200:
            payload = result.json()
            if payload['choices'][0]['finish_reason'] == 'stop':
                payload['choices'][0]['message']['content'] = json.dumps({
                    'facts': [], 'source_matches_poi': True, 'source_content_valid': True,
                    'continuation_needed': False})
                return httpx.Response(200, json=payload)
        return result


@pytest.mark.asyncio
async def test_large_giga_context_reaches_provider_with_complete_evidence(tmp_path):
    provider, admission = EmptyFindings(), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    context = {'coverage_goal': 'Check the source', 'architectural_context': 'ж'*70000}
    try:
        result = await adapter._extract_giga_page(PAGE, story, context, allow_fallback=False)
        saved = receipts(adapter)[-1]
        assert result['result']['facts'] == []
        assert saved['phase'] == 'completed' and saved['provider_send_state'] == 'response_closed'
        assert provider.chats == len(saved['inference_sends']) == 2
        assert any(context['architectural_context'] in request.content.decode('utf-8')
                   for request in provider.requests)
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
async def test_envelope_expires_during_admission_before_giga_chat_dispatch(tmp_path):
    from street_story.research_budget import ResearchTerminated, ensure_budget
    from contextlib import asynccontextmanager
    provider, admission = EmptyFindings(), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    clock = [adapter.service.store.now()]
    adapter.service.store.now = lambda: clock[0]
    budget = ensure_budget(adapter.service, story['id'], explicit=True)
    @asynccontextmanager
    async def expire_in_admission(binding, workload):
        async with admission(binding, workload) as lease:
            clock[0] = budget['deadline_at']
            yield lease
    adapter.giga.admission = expire_in_admission
    try:
        with pytest.raises(ResearchTerminated, match='research_deadline_exceeded'):
            await adapter._extract_giga_page(PAGE, story, {'coverage_goal': 'Check the source'}, allow_fallback=False)
        assert provider.chats == 0
        saved = receipts(adapter)
        assert len(saved) == 1 and saved[0]['provider_send_state'] == 'not_sent'
        assert 'inference_sends' not in saved[0]
        assert ensure_budget(adapter.service, story['id'])['deadline_at'] == budget['deadline_at']
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_context,code', [
    ({'coverage_goal': ''}, 'gigachat:bounded_request_required'),
])
async def test_local_validation_failure_creates_safe_new_attempt_after_input_fixed(tmp_path, invalid_context, code):
    provider, admission = EmptyFindings(), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    try:
        with pytest.raises(PermanentProviderError, match=code):
            await adapter.extract_fact_page(PAGE, story, invalid_context)
        before = receipts(adapter)
        assert len(before) == 1
        failed = before[0]
        assert failed['phase'] == 'failed' and failed['retry_safe'] is True
        assert failed['provider_send_state'] == 'not_sent'
        assert failed['error_code'] == code
        assert len(failed['input_sha256']) == 64
        assert 'inference_sends' not in failed
        assert provider.requests == [] and admission.bindings == []

        result = await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Check the source'})
        after = receipts(adapter)
        assert len(after) == 2 and after[0] == failed
        assert after[1]['phase'] == 'completed'
        assert after[1]['provider_send_state'] == 'response_closed'
        assert after[1]['binding']['attempt_id'] != failed['binding']['attempt_id']
        assert after[1]['input_sha256'] != failed['input_sha256']
        assert result['result']['facts'] == []
        assert provider.auths == 1 and provider.chats == 2
        assert len(after[1]['inference_sends']) == 2
        assert all(len(send['request_body_sha256']) == 64 for send in after[1]['inference_sends'])
        assert 'fake-secret' not in json.dumps(after)
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['valueerror', 'transport_timeout'])
async def test_post_dispatch_failure_stays_unknown_and_blocks_second_call(tmp_path, failure):
    provider, admission = Provider(), Admission()
    chats = []

    async def fail_chat(request):
        if request.url.path == '/api/v2/oauth':
            return await provider(request)
        chats.append(request)
        if failure == 'transport_timeout':
            raise httpx.ReadTimeout('response missing', request=request)
        raise ValueError('private payload that must not be persisted')

    adapter, story = setup(tmp_path, fail_chat, admission)
    try:
        with pytest.raises(RetryableProviderError, match='gigachat_research_waiting'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        saved = receipts(adapter)
        assert len(saved) == 1
        assert saved[0]['phase'] == 'unknown' and saved[0]['retry_safe'] is False
        assert saved[0]['provider_send_state'] == 'possibly_sent'
        assert saved[0]['error_type'] == ('ValueError' if failure == 'valueerror' else 'GigaChatProviderError')
        if failure == 'valueerror':
            assert saved[0]['error_code'] == 'gigachat:local_validation_failed'
        else:
            assert saved[0]['error_code'] == 'gigachat:inference_transport_unknown'
        assert len(saved[0]['inference_sends']) == 1
        assert len(admission.sends) == len(chats) == 1
        with pytest.raises(RetryableProviderError, match='gigachat_attempt_outcome_unknown'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        assert len(chats) == 1 and receipts(adapter) == saved
        assert 'private payload' not in json.dumps(saved)
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['oauth', 'admission'])
async def test_failure_before_model_boundary_is_not_an_unknown_inference(tmp_path, failure):
    callbacks = []
    admission = Admission(denied=failure == 'admission')

    async def oauth_failure(request):
        callbacks.append(request.url.path)
        raise httpx.ConnectError('OAuth network failure', request=request)

    adapter, story = setup(tmp_path, oauth_failure, admission)
    try:
        with pytest.raises(RetryableProviderError, match='gigachat_research_waiting'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        saved = receipts(adapter)[0]
        assert saved['phase'] == 'failed' and saved['retry_safe'] is True
        assert saved['provider_send_state'] == 'not_sent'
        assert 'inference_sends' not in saved and not admission.sends
        assert callbacks == (['/api/v2/oauth'] if failure == 'oauth' else [])
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
async def test_durable_marker_precedes_shared_send_and_model_post_after_oauth(tmp_path):
    provider = EmptyFindings()
    adapter = None

    class InspectAdmission(Admission):
        async def before_send(self, metadata):
            assert provider.auths == 1
            saved = receipts(adapter)[-1]
            assert saved['phase'] == 'submitted'
            assert saved['inference_sends'][-1]['attempt_id'] == metadata['attempt_id']
            assert saved['inference_sends'][-1]['request_body_sha256'] == metadata['request_body_sha256']
            await super().before_send(metadata)

    admission = InspectAdmission()
    adapter, story = setup(tmp_path, provider, admission)
    try:
        await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        assert provider.chats == len(admission.sends) == 2
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
async def test_historical_unknown_receipt_is_never_reclassified_or_resubmitted(tmp_path):
    provider, admission = EmptyFindings(), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    binding, _ = adapter.attempt(story, 'facts_gigachat', PAGE['_unit_id'])
    historical = {'binding': binding, 'phase': 'unknown', 'error_type': 'ValueError',
                  'provider_id': 'gigachat', 'model_id': 'GigaChat-2'}
    await adapter.checkpoint(binding, historical)
    try:
        with pytest.raises(RetryableProviderError, match='gigachat_attempt_outcome_unknown'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        assert receipts(adapter) == [historical]
        assert not provider.requests and not admission.bindings
    finally:
        await adapter.giga.aclose()


@pytest.mark.asyncio
async def test_configured_but_unavailable_giga_uses_only_qualified_text_fallback(tmp_path):
    from types import SimpleNamespace
    adapter, story = setup(tmp_path, EmptyFindings(), Admission(denied=True))
    adapter.client = SimpleNamespace(endpoint='http://existing-opencode:4097', model_id='qualified-text')
    calls = []
    async def fallback(capsule, page, story):
        calls.append(capsule)
        return {'result': {'facts': []}, 'receipt': {'provider_id': 'qualified-text'}}
    adapter._extract_opencode_page = fallback
    try:
        with pytest.raises(RetryableProviderError):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        assert not calls
        adapter.service.store.cache_put('research-text-verification-v1', {
            'model_id': adapter.client.model_id, 'endpoint': adapter.client.endpoint,
            'semantic_contract_verified': True}, ttl_seconds=3600)
        result = await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read'})
        assert len(calls) == 1 and result['result']['facts'] == []
        assert all(item['provider_send_state'] == 'not_sent' for item in receipts(adapter))
    finally:
        await adapter.giga.aclose()


def test_compact_capsule_preserves_whole_claims_and_pages_all_inventory():
    from street_story.research_adapter import fact_page_capsule
    text = 'В 2027 году планируют открыть выставку. ' * 10
    facts = [{'fact_id': f'f{i}', 'text': text, 'sources': [{'supports': ['x' * 5000]}]} for i in range(80)]
    capsule = fact_page_capsule(PAGE, {
        'confirmed_identity': {'status': 'match', 'candidate_id': 'wiki:77', 'candidate_name': 'Gate',
                               'candidates': [{'runtime_receipts': 'x' * 16000}]},
        'known_facts': facts[:50], '_known_fact_inventory': facts, 'prior_poi_facts': facts[:60]})
    public = {key: value for key, value in capsule.items() if key != '_known_fact_inventory'}
    assert len(json.dumps(public, ensure_ascii=False).encode()) < 65536
    assert len(capsule['_known_fact_inventory']) == 80
    assert all(item['text'] == text for item in capsule['_known_fact_inventory'])
    assert capsule['context']['known_inventory_complete'] is False
    assert capsule['context']['known_inventory_next_offset'] == len(capsule['known_fact_inventory'])
    assert 'known_facts' not in capsule['context'] and 'prior_poi_facts' not in capsule['context']
