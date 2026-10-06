"""Closed semantic failures never pay twice for the same frozen fact unit."""
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from street_story.errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from street_story.research_adapter import ProductResearchAdapter
from test_gigachat_dispatch_checkpoint import PAGE, receipts
from test_research_control import fixture


class Giga:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def research(self, query, **kwargs):
        self.calls.append(query)
        await kwargs['before_inference']({'attempt_id': f'send-{len(self.calls)}', 'request_body_sha256': 'a' * 64})
        if self.failure == 'unknown':
            raise RuntimeError('unknown transport outcome')
        if self.failure == 'not_sent':
            raise AssertionError('not_sent fixture uses its own subclass')
        if self.failure or len(self.calls) == 1:
            raise MalformedProviderResponse('gigachat:semantic_json_invalid')
        return {'payload': {'facts': [], 'source_matches_poi': True, 'source_content_valid': True,
                            'continuation_needed': False}, 'receipts': []}


def prepared(tmp_path, *, failure=None):
    service, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service, adapter.giga = service, Giga(failure)
    adapter._active_binding = ContextVar('closed-replay-test', default=None)
    return adapter, {'id': sid, 'photo_sha256': photo, '_fact_research_control_revision': 0}


@pytest.mark.asyncio
async def test_worker_retries_preserve_closed_receipt_and_send_only_one_model_unit(tmp_path):
    adapter, story = prepared(tmp_path, failure='closed')
    context = {'coverage_goal': 'Read the frozen source'}
    with pytest.raises(MalformedProviderResponse):
        await adapter.extract_fact_page(PAGE, story, context)
    saved = receipts(adapter)
    assert saved[0]['phase'] == 'failed' and saved[0]['provider_send_state'] == 'response_closed'
    for _ in range(3):
        with pytest.raises(PermanentProviderError, match='closed_semantic_unit_requires_live'):
            await adapter.extract_fact_page(PAGE, story, context)
    assert adapter.giga.calls == [context['coverage_goal']]
    assert receipts(adapter) == saved


@pytest.mark.asyncio
async def test_changed_input_permits_one_new_unit_and_reuses_successful_completion(tmp_path):
    adapter, story = prepared(tmp_path)
    with pytest.raises(MalformedProviderResponse):
        await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read source'})
    failed = receipts(adapter)[0]
    result = await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Changed owner scope'})
    assert result['result']['facts'] == []
    saved = receipts(adapter)
    assert len(saved) == 2 and saved[0] == failed
    assert saved[1]['input_sha256'] != failed['input_sha256']
    # A previous failed digest must not shadow a later successful unit.
    repeated = await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read source'})
    assert repeated == result
    assert len(adapter.giga.calls) == 2 and receipts(adapter) == saved


@pytest.mark.asyncio
async def test_unknown_outcome_stays_waiting_even_if_new_input_changes(tmp_path):
    adapter, story = prepared(tmp_path, failure='unknown')
    with pytest.raises(RetryableProviderError, match='gigachat_research_waiting'):
        await adapter.extract_fact_page(PAGE, story, {'coverage_goal': 'Read source'})
    saved = receipts(adapter)
    assert saved[0]['phase'] == 'unknown'
    for goal in ('Read source', 'Changed owner scope'):
        with pytest.raises(RetryableProviderError, match='gigachat_attempt_outcome_unknown'):
            await adapter.extract_fact_page(PAGE, story, {'coverage_goal': goal})
    assert len(adapter.giga.calls) == 1 and receipts(adapter) == saved


@pytest.mark.asyncio
async def test_only_qualified_fallback_can_consume_the_closed_unit(tmp_path):
    adapter, story = prepared(tmp_path, failure='closed')
    context = {'coverage_goal': 'Read source'}
    with pytest.raises(MalformedProviderResponse):
        await adapter.extract_fact_page(PAGE, story, context)
    saved = receipts(adapter)
    adapter.client = SimpleNamespace(endpoint='http://existing:4097', model_id='qualified-text')
    calls = []
    async def fallback(capsule, page, story):
        calls.append(capsule)
        return {'result': {'facts': []}, 'receipt': {'provider_id': 'qualified-text'}}
    adapter._extract_opencode_page = fallback
    with pytest.raises(PermanentProviderError):
        await adapter.extract_fact_page(PAGE, story, context)
    assert not calls
    adapter.service.store.cache_put('research-text-verification-v1', {
        'model_id': adapter.client.model_id, 'endpoint': adapter.client.endpoint,
        'semantic_contract_verified': True}, ttl_seconds=3600)
    result = await adapter.extract_fact_page(PAGE, story, context)
    assert result['result']['facts'] == [] and len(calls) == 1
    assert len(adapter.giga.calls) == 1 and receipts(adapter) == saved


@pytest.mark.asyncio
async def test_unacknowledged_abort_keeps_unknown_wait_over_older_closed_failure(tmp_path):
    adapter, story = prepared(tmp_path, failure='closed')
    context = {'coverage_goal': 'Read source'}
    with pytest.raises(MalformedProviderResponse):
        await adapter.extract_fact_page(PAGE, story, context)
    binding, _ = adapter.attempt(story, 'facts_gigachat', PAGE['_unit_id'])
    await adapter.checkpoint(binding, {'binding': binding, 'phase': 'aborted', 'abort_acknowledged': False})
    saved = receipts(adapter)
    with pytest.raises(RetryableProviderError, match='research_attempt_unknown'):
        await adapter.extract_fact_page(PAGE, story, context)
    assert len(adapter.giga.calls) == 1 and receipts(adapter) == saved
