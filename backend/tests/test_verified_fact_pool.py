import asyncio
import copy
from contextvars import ContextVar

import pytest

from street_story.errors import MalformedProviderResponse, RetryableProviderError
from street_story.opencode_research import ResearchUnavailable
from street_story.research_adapter import ProductResearchAdapter
from test_gigachat_dispatch_checkpoint import PAGE
from test_research_control import fixture


PAYLOAD = {'facts': [], 'source_matches_poi': True, 'source_content_valid': True,
           'continuation_needed': False}


class FactClient:
    endpoint = 'http://127.0.0.1:4097'
    provider_id = 'opencode'
    directory = '/existing/research'

    def __init__(self, model_id, adapter):
        self.model_id, self.adapter, self.calls = model_id, adapter, []
        self.mode = 'completed'

    async def extract_facts(self, capsule, binding):
        self.calls.append(copy.deepcopy(binding))
        receipt = {'binding': binding, 'model_id': self.model_id, 'provider_id': self.provider_id,
                   'session_id': 'ses_existing', 'message_id': 'msg_existing', 'phase': self.mode}
        if binding.get('phase') in {'submitted', 'prompt_intent'}:
            receipt['readback_only'] = True
        if self.mode == 'quota':
            receipt.update(phase='failed', provider_status=429)
        if self.mode in {'unknown', 'submitted'}:
            receipt['phase'] = 'submitted'
        if self.mode == 'malformed':
            receipt.update(phase='failed', provider_send_state='response_closed')
        if self.mode in {'completed', 'invalid_payload'}:
            receipt.update(phase='completed', result=PAYLOAD if self.mode == 'completed' else {'facts': 'wrong'})
        await self.adapter.checkpoint(binding, receipt)
        if self.mode not in {'completed', 'invalid_payload'}:
            raise ResearchUnavailable('research_provider_failed', receipt)
        return {'result': receipt['result'], 'receipt': receipt}


class Giga:
    def __init__(self):
        self.calls, self.mode = [], 'completed'

    async def research(self, query, **kwargs):
        self.calls.append(query)
        await kwargs['before_inference']({'operation': 'research'})
        if self.mode == 'unknown':
            raise RuntimeError('unknown outcome')
        if self.mode == 'malformed':
            raise MalformedProviderResponse('closed malformed')
        return {'payload': copy.deepcopy(PAYLOAD), 'receipts': []}


def setup(tmp_path):
    svc, sid, photo = fixture(tmp_path)
    adapter = object.__new__(ProductResearchAdapter)
    adapter.service, adapter.giga = svc, Giga()
    adapter._active_binding = ContextVar('fact-pool-test', default=None)
    adapter.client = FactClient('mimo-v2.6-flash-free', adapter)
    extra = FactClient('nemotron-3-ultra-free', adapter)
    adapter._fact_extractor_clients = {'nemotron-3-ultra-free': extra}
    entries = []
    for provider, model in [('gigachat', 'GigaChat-2'), ('opencode', adapter.client.model_id),
                            ('opencode', extra.model_id)]:
        entries.append({'provider_id': provider, 'model_id': model,
                        **({'endpoint': adapter.client.endpoint} if provider == 'opencode' else {}),
                        'semantic_contract_verified': True, 'source_subject_negative_verified': True,
                        'planned_modality_verified': True, 'known_claim_reuse_verified': True})
    svc.store.cache_put('research-text-verification-v1', {'extractors': entries}, ttl_seconds=3600)
    story = {'id': sid, 'photo_sha256': photo, '_fact_research_control_revision': 0}
    return adapter, extra, story, entries


def page(ordinal, unit=None):
    return {**PAGE, '_unit_id': unit or f'page-{ordinal}', '_extractor_ordinal': ordinal}


@pytest.mark.asyncio
async def test_qualified_chunks_round_robin_without_changing_search_client(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    original = adapter.client
    for ordinal in range(6):
        result = await adapter.extract_fact_page(page(ordinal), story, {'coverage_goal': 'Read'})
        assert result['result'] == PAYLOAD
    assert len(adapter.giga.calls) == len(adapter.client.calls) == len(extra.calls) == 2
    assert adapter.client is original
    for ordinal in range(6):
        await adapter.extract_fact_page(page(ordinal), story, {'coverage_goal': 'Read'})
    assert len(adapter.giga.calls) == len(adapter.client.calls) == len(extra.calls) == 2


@pytest.mark.asyncio
async def test_unknown_giga_fences_same_unit_but_other_chunks_continue(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    adapter.giga.mode = 'unknown'
    with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
        await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Read'})
    for ordinal in (1, 2):
        with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
            await adapter.extract_fact_page(page(ordinal, 'page-0'), story, {'coverage_goal': 'Changed'})
    assert not adapter.client.calls and not extra.calls and len(adapter.giga.calls) == 1
    await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    await adapter.extract_fact_page(page(2), story, {'coverage_goal': 'Read'})
    assert len(adapter.client.calls) == len(extra.calls) == 1


@pytest.mark.asyncio
async def test_unknown_opencode_reads_original_route_after_proof_removed(tmp_path):
    adapter, extra, story, entries = setup(tmp_path)
    adapter.client.mode = 'submitted'
    with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
        await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': [entries[0], entries[2]]}, ttl_seconds=3600)
    adapter.client.mode = 'completed'
    result = await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    assert result['receipt']['readback_only']
    assert adapter.client.calls[0]['attempt_id'] == adapter.client.calls[1]['attempt_id']
    assert not adapter.giga.calls and not extra.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['malformed', 'quota', 'invalid_payload'])
async def test_closed_opencode_response_falls_through_without_unchanged_repeat(tmp_path, mode):
    adapter, extra, story, _ = setup(tmp_path)
    adapter.client.mode = mode
    result = await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    assert result['receipt']['model_id'] == extra.model_id
    assert len(adapter.client.calls) == len(extra.calls) == 1
    assert not adapter.giga.calls
    await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    assert len(adapter.client.calls) == len(extra.calls) == 1


@pytest.mark.asyncio
async def test_closed_giga_uses_next_qualified_route(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    adapter.giga.mode = 'malformed'
    result = await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Read'})
    assert result['receipt']['model_id'] == adapter.client.model_id
    assert len(adapter.giga.calls) == len(adapter.client.calls) == 1 and not extra.calls


@pytest.mark.asyncio
async def test_catalog_or_inference_alone_does_not_qualify_extractor(tmp_path):
    adapter, extra, story, entries = setup(tmp_path)
    entries[2].pop('planned_modality_verified')
    adapter.service.store.cache_put('research-text-verification-v1', {'extractors': entries}, ttl_seconds=3600)
    assert adapter.facts_available
    await adapter.extract_fact_page(page(2), story, {'coverage_goal': 'Read'})
    assert not extra.calls
    assert len(adapter.giga.calls) == 1


@pytest.mark.asyncio
async def test_unaddressable_unknown_cannot_use_an_available_other_route(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    binding, _ = adapter.attempt(story, 'facts', 'page-1')
    await adapter.checkpoint(binding, {'binding': binding, 'phase': 'prompt_intent'})
    with pytest.raises(RetryableProviderError, match='unit_outcome_unknown'):
        await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    assert not adapter.giga.calls and not adapter.client.calls and not extra.calls


def test_nemotron_transport_clones_same_server_without_any_request_or_profile_write(tmp_path):
    from street_story.shared_devcoveer_research import SharedDevCoveerResearch
    backend, admission, checkpoint = object(), object(), object()
    primary = SharedDevCoveerResearch(str(tmp_path), model_id='mimo-v2.6-flash-free',
        backend=backend, admission=admission, checkpoint=checkpoint)
    extra = primary.fact_extractor('nemotron-3-ultra-free')
    assert extra.directory == primary.directory and extra.endpoint == primary.endpoint
    assert extra.shared_backend is backend and extra.admission is admission and extra.checkpoint is checkpoint
    assert extra.limits.max_output_tokens == 8192
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match='not_approved'):
        primary.fact_extractor('unqualified-model')


@pytest.mark.asyncio
async def test_all_closed_routes_never_repeat_unchanged_chunk(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    adapter.giga.mode = adapter.client.mode = extra.mode = 'malformed'
    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='pool_waiting'):
            await adapter.extract_fact_page(page(0), story, {'coverage_goal': 'Read'})
    assert len(adapter.giga.calls) == len(adapter.client.calls) == len(extra.calls) == 1


@pytest.mark.asyncio
async def test_current_caller_loses_lease_before_original_readback(tmp_path):
    from street_story.service import ConflictError
    adapter, extra, story, _ = setup(tmp_path)
    adapter.client.mode = 'submitted'
    with pytest.raises(RetryableProviderError):
        await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    adapter.client.mode = 'completed'
    story = {**story, '_research_job_id': 'missing-current-job', '_research_job_attempt': 2}
    with pytest.raises(ConflictError):
        await adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'})
    assert len(adapter.client.calls) == 1 and not extra.calls and not adapter.giga.calls


@pytest.mark.asyncio
async def test_duplicate_unit_lock_does_not_serialize_other_chunks(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.client.extract_facts
    async def blocked(capsule, binding):
        entered.set()
        await release.wait()
        return await original(capsule, binding)
    adapter.client.extract_facts = blocked
    first = asyncio.create_task(adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'}))
    await asyncio.wait_for(entered.wait(), 1)
    duplicate = asyncio.create_task(adapter.extract_fact_page(page(1), story, {'coverage_goal': 'Read'}))
    independent = await asyncio.wait_for(adapter.extract_fact_page(page(2), story, {'coverage_goal': 'Read'}), 1)
    assert independent['receipt']['model_id'] == extra.model_id
    assert not duplicate.done()
    release.set()
    await asyncio.gather(first, duplicate)
    assert len(adapter.client.calls) == len(extra.calls) == 1
