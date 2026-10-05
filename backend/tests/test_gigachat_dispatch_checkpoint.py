"""Durable Giga dispatch markers distinguish local failures from unknown sends."""
import json
from contextvars import ContextVar

import httpx
import pytest

from street_story.errors import RetryableProviderError
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
@pytest.mark.parametrize('invalid_context,code', [
    ({'coverage_goal': ''}, 'gigachat:bounded_request_required'),
    ({'coverage_goal': 'Read', 'extra': 'x' * 70000}, 'gigachat:capsule_too_large'),
])
async def test_local_validation_failure_creates_safe_new_attempt_after_input_fixed(tmp_path, invalid_context, code):
    provider, admission = EmptyFindings(), Admission()
    adapter, story = setup(tmp_path, provider, admission)
    try:
        with pytest.raises(RetryableProviderError, match='gigachat_research_waiting'):
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
