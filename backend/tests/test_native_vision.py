import asyncio
import base64
import copy
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from PIL import Image
import pytest

from street_story.errors import RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.native_quota import NativeQuotaPermission
from street_story.native_vision import NativeVisionProvider
from test_native_quota import Store


class NativeClient:
    def __init__(self):
        self.calls, self.inputs, self.closed = [], {}, False
        self.used, self.turn_status, self.reject = 50, 'completed', False
        self.read_started = asyncio.Event()
        self.block_read = False
        self.wrong_image = False

    async def request(self, method, params, timeout=30):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'config/read':
            return {'config': {'mcp_servers': {'example': {}}}}
        if method == 'thread/start':
            return {'thread': {'id': 'thread_' + str(len(self.inputs))}, 'model': 'gpt-6-luna',
                    'reasoningEffort': 'medium', 'approvalPolicy': 'never',
                    'sandbox': {'type': 'readOnly', 'networkAccess': False}}
        if method == 'account/rateLimits/read':
            return {'accountId': 'owner', 'rateLimits': {'limitId': 'codex',
                    'primary': {'usedPercent': self.used, 'resetsAt': 9000}}}
        if method == 'turn/start':
            if self.reject:
                class NativeAppServerError(RuntimeError):
                    code, data = 429, {'category': 'rate_limit'}
                raise NativeAppServerError('Rate limit exceeded')
            self.inputs[params['threadId']] = copy.deepcopy(params['input'])
            return {'turn': {'id': 'turn_' + params['threadId']}}
        if method == 'thread/read':
            self.read_started.set()
            if self.block_read:
                await asyncio.Event().wait()
            content = copy.deepcopy(self.inputs[params['threadId']])
            content[0]['text_elements'] = []
            content[2]['detail'] = None
            if self.wrong_image:
                content[2]['url'] = 'https://example.org/foreign.jpg'
            result = {'status': 'mismatch', 'candidate_id': 'web:reference',
                      'reference_subject_candidate_id': '', 'reference_subject_observations': [],
                      'confidence': .99, 'observations': ['Distinct facade'], 'alternative_candidate_ids': []}
            return {'thread': {'turns': [{'id': 'turn_' + params['threadId'], 'status': self.turn_status,
                    'items': [{'type': 'userMessage', 'content': content},
                              {'type': 'agentMessage', 'text': json.dumps(result)}]}]}}
        if method == 'turn/interrupt':
            return {}
        raise AssertionError(method)

    def turn_token_usage(self, thread_id, turn_id):
        return {'totalTokens': 321, 'inputTokens': 300, 'outputTokens': 21}

    def _stop_sync(self):
        self.closed = True


def setup(tmp_path):
    store, client, receipts, sends, finalized = Store(), NativeClient(), [], [], []
    service = SimpleNamespace(store=store, settings=SimpleNamespace(data_dir=tmp_path, native_vision_reserve=True))
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'owner-binding')
    async def checkpoint(binding, receipt):
        receipts.append(copy.deepcopy(receipt))
    @asynccontextmanager
    async def admission(binding, workload):
        async def send(metadata):
            sends.append(metadata)
        async def finalize(metadata, state):
            finalized.append((metadata, state))
        yield SimpleNamespace(before_send=send, finalize=finalize)
    provider = NativeVisionProvider(service, admission=admission, checkpoint=checkpoint,
                                    client_factory=lambda: client, permission=permission)
    provider.poll_seconds = .001
    output = io.BytesIO()
    Image.new('RGB', (40, 40), 'red').save(output, format='JPEG')
    context = {'comparison_id': 'comparison', 'references': [{'label': 'REF 1', 'reference_id': 'ref-1', 'candidate_id': 'web:reference'}],
               'physical_candidates': [{'candidate_id': 'osm:way:1'}]}
    story = {'id': 'story', '_identity_generation': 0,
             '_visual_image_parts': [{'label': 'SOURCE', 'mime_type': 'image/jpeg',
                                      'data': base64.b64encode(output.getvalue()).decode()},
                                     {'label': 'REF 1', 'url': 'https://example.org/ref.jpg'}],
             '_visual_reference_mapping': context['references']}
    return provider, client, output.getvalue(), story, context, receipts, sends, finalized


@pytest.mark.asyncio
async def test_pipeline_comparisons_share_fifteen_minute_permission_and_owned_transport(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    for attempt in ('first', 'second'):
        result = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': attempt})
        assert result['result']['status'] == 'mismatch'
        assert result['receipt']['quota_permission']['expires_at'] == 1900
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1
    assert len(sends) == 2
    assert all(state == 'completed' and receipt['actual_total_tokens'] == 321 for receipt, state in finalized)
    start = next(body for method, body in client.calls if method == 'thread/start')
    assert start['config']['mcp_servers.example.enabled'] is False
    assert start['dynamicTools'] == []
    provider.service.store.time = 1900
    await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'third'})
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 2
    await provider.close()
    assert client.closed and provider.client is None


@pytest.mark.asyncio
async def test_below_reserve_never_sends_model_turn(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    client.used = 98
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    assert not sends and not any(method == 'turn/start' for method, _ in client.calls)
    assert receipts[-1]['phase'] == 'created'


@pytest.mark.asyncio
async def test_resource_denial_preserves_dispatch_phase_and_authority_retry(tmp_path, caplog):
    # The public consumer CI does not install the private resource SDK. Exercise
    # its documented exception contract without making the suite depend on it.
    class ResourceError(RuntimeError):
        resource_failure = True
        code = 'RESOURCE_DAILY_BUDGET'
        retry_after_ms = 120000
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    @asynccontextmanager
    async def denied(binding, workload):
        raise ResourceError('RESOURCE_DAILY_BUDGET')
        yield
    provider.admission = denied
    binding = {'attempt_id': 'denied', 'phase': 'created'}
    with pytest.raises(RetryableProviderError, match='RESOURCE_DAILY_BUDGET') as exc:
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, binding)
    assert exc.value.retry_at == provider.service.store.now() + 120
    assert receipts[-1]['phase'] == 'created'
    assert receipts[-1]['route_failure']['code'] == 'RESOURCE_DAILY_BUDGET'
    assert receipts[-1]['thread_id'] == binding.get('thread_id')
    assert receipts[-1]['turn_id'] == binding.get('turn_id')
    assert not client.calls and not sends and not finalized
    assert 'native_visual_resource_wait' in caplog.text


@pytest.mark.asyncio
async def test_existing_turn_readback_never_reserves_or_sends_another_inference(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    first = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    admission_calls = []
    @asynccontextmanager
    async def unavailable(binding, workload):
        admission_calls.append(binding)
        raise AssertionError('Readback must not acquire fresh inference capacity')
        yield
    provider.admission = unavailable
    binding = {key: first['receipt'][key] for key in ('thread_id', 'turn_id', 'profile_verified', 'quota_permission')}
    binding.update(attempt_id='first', phase='unknown')
    provider.service.store.time = 2000
    resumed = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, binding)
    assert resumed['receipt']['phase'] == 'completed'
    assert resumed['receipt']['quota_permission'] == first['receipt']['quota_permission']
    assert resumed['receipt']['usage']['totalTokens'] == 321
    assert resumed['receipt']['resource_reconciliation'] == 'readback_only_original_reservation_unchanged'
    assert not admission_calls
    assert len(sends) == len(finalized) == 1
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_native_rejection_invalidates_quota_grant_before_expiry(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    client.reject = True
    with pytest.raises(RetryableProviderError):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    assert provider.service.store.cache[provider.permission._key('owner-binding')]['expires_at'] == 0
    client.used = 98
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await provider.permission.ensure(client)
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 2


@pytest.mark.asyncio
async def test_lost_turn_start_response_reconciles_exact_input_without_another_turn(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    result = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    binding = {'attempt_id': 'first', 'phase': 'prompt_intent', 'thread_id': result['receipt']['thread_id'],
               'profile_verified': True}
    provider.service.store.time = 2000
    resumed = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, binding)
    assert resumed['receipt']['turn_id'] == result['receipt']['turn_id']
    assert len(sends) == 1
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_wrong_turn_image_is_never_accepted_or_blindly_repeated(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    client.wrong_image = True
    provider.timeout = .03
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    assert receipts[-1]['phase'] == 'unknown' and finalized[-1][1] == 'unknown'
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_shape', ['missing_status', 'missing_input', 'missing_output'])
async def test_partial_native_readback_waits_on_same_turn_without_new_send(tmp_path, pending_shape):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    reads = 0
    async def request(method, params, timeout=30):
        nonlocal reads
        result = await original(method, params, timeout)
        if method == 'thread/read':
            reads += 1
            if reads == 1:
                turn = result['thread']['turns'][0]
                if pending_shape == 'missing_status':
                    turn['status'] = None
                elif pending_shape == 'missing_input':
                    turn['items'] = []
                else:
                    turn['items'] = turn['items'][:1]
        return result
    client.request = request
    result = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    assert result['receipt']['phase'] == 'completed' and reads == 2
    assert len(sends) == 1 and sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_worker_cancellation_interrupts_owned_turn_and_keeps_unknown_receipt(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    client.block_read = True
    task = asyncio.create_task(provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'}))
    await client.read_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sum(method == 'turn/interrupt' for method, _ in client.calls) == 1
    assert receipts[-1]['phase'] == 'unknown' and finalized[-1][1] == 'unknown'
