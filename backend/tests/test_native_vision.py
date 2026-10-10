import asyncio
import base64
import copy
import io
import json
import hashlib
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
    async def load_reference(url):
        return 'image/jpeg', output.getvalue()
    provider.public_image_loader = load_reference
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
@pytest.mark.parametrize('stage', ['config/read', 'account/rateLimits/read'])
@pytest.mark.parametrize('external_stop', [False, True])
async def test_unsent_setup_deadline_is_local_but_external_cancellation_propagates(tmp_path, stage, external_stop):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    entered = asyncio.Event()
    async def blocked(method, params, timeout=30):
        if method == stage:
            entered.set()
            await asyncio.Event().wait()
        return await original(method, params, timeout)
    client.request = blocked
    provider.setup_timeout = 1 if external_stop else .01
    task = asyncio.create_task(provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context,
        {'attempt_id': 'setup'}))
    await entered.wait()
    if external_stop:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RetryableProviderError, match='native_setup_timeout'):
            await task
    assert not sends and not any(method == 'turn/start' for method, _ in client.calls)
    assert receipts[-1]['provider_send_state'] == 'not_sent'
    assert receipts[-1].get('error_code') == (None if external_stop else 'native_setup_timeout')
    assert finalized[-1][1] == 'aborted' and finalized[-1][0]['actual_total_tokens'] == 0
    # The same independently authorized provider can still perform healthy
    # work. No shared account cooldown or inferred negative model verdict.
    client.request = original
    result = await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context,
        {'attempt_id': 'healthy'})
    assert result['receipt']['phase'] == 'completed' and len(sends) == 1


@pytest.mark.asyncio
async def test_below_reserve_never_sends_model_turn(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    client.used = 98
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    assert not sends and not any(method == 'turn/start' for method, _ in client.calls)
    assert receipts[-1]['phase'] == 'created'
    assert receipts[-1]['provider_send_state'] == 'not_sent'
    assert finalized[-1][1] == 'aborted'
    assert finalized[-1][0]['actual_total_tokens'] == 0


@pytest.mark.asyncio
async def test_source_map_preserves_exact_map_pixels_and_reads_original_turn_without_fresh_quota(tmp_path):
    provider, client, source, story, _context, receipts, sends, _finalized = setup(tmp_path)
    map_file = io.BytesIO()
    Image.new('RGB', (67, 13), 'blue').save(map_file, format='PNG')
    images = [('SOURCE', 'image/jpeg', source), ('MAP', 'image/png', map_file.getvalue())]
    host = {'source_map_receipt': {'manifest': {'image_sha256': 'frozen-map'},
        'model_source_sha256': hashlib.sha256(source).hexdigest(),
        'map_image_sha256': hashlib.sha256(map_file.getvalue()).hexdigest()}, 'schema': VERDICT_SCHEMA}
    client.turn_status = 'inProgress'
    provider.timeout = .03
    with pytest.raises(RetryableProviderError, match='native_turn_outcome_unknown'):
        await provider.compare_source_map(story, VERDICT_SCHEMA, 'SOURCE and MAP geometry', images,
                                          {'attempt_id': 'spatial'}, host)
    first = next(params for method, params in client.calls if method == 'turn/start')
    assert [part['text'] for part in first['input'] if part['type'] == 'text'][1:] == ['SOURCE', 'MAP']
    pixels = [base64.b64decode(part['url'].split(',', 1)[1]) for part in first['input'] if part['type'] == 'image']
    assert pixels == [source, map_file.getvalue()]
    receipt = receipts[-1]
    assert receipt['phase'] == 'unknown'
    binding = {**receipt['binding'], **{key: receipt[key] for key in (
        'thread_id', 'turn_id', 'phase', 'profile_verified', 'image_transport', 'frozen_source_map')}}
    client.used = 99
    client.turn_status = 'completed'
    provider.timeout = 1
    readback = await provider.compare_source_map(story, {}, 'different incoming request', [], binding, {})
    assert readback['host_context'] == host
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert len(sends) == 1
    await provider.close()


@pytest.mark.asyncio
async def test_source_map_quota_denial_is_unsent_and_large_input_reaches_native(tmp_path):
    provider, client, source, story, _context, receipts, sends, _finalized = setup(tmp_path)
    images = [('SOURCE', 'image/jpeg', source), ('MAP', 'image/jpeg', source)]
    host = {'source_map_receipt': {'model_source_sha256': hashlib.sha256(source).hexdigest(),
                                 'map_image_sha256': hashlib.sha256(source).hexdigest()}}
    client.used = 99
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await provider.compare_source_map(story, VERDICT_SCHEMA, 'bounded geometry', images,
                                         {'attempt_id': 'spatial'}, host)
    assert not sends and receipts[-1]['provider_send_state'] == 'not_sent'
    client.used = 0
    large_prompt = 'x' * 70000
    result = await provider.compare_source_map(story, VERDICT_SCHEMA, large_prompt, images,
                                              {'attempt_id': 'large-input'}, host)
    assert result['receipt']['input_utf8_bytes'] > 65536
    assert result['receipt']['phase'] == 'completed' and len(sends) == 1
    turn = next(params for method, params in client.calls if method == 'turn/start')
    assert turn['input'][0]['text'] == large_prompt
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('violation', [None, 'uniqueItems', 'allOf'])
async def test_source_map_native_subset_preserves_frozen_host_constraints(tmp_path, violation):
    from jsonschema import ValidationError
    provider, client, source, story, _context, receipts, sends, _finalized = setup(tmp_path)
    schema = copy.deepcopy(VERDICT_SCHEMA)
    schema['properties']['observations']['uniqueItems'] = True
    schema['allOf'] = [{'if': {'properties': {'status': {'const': 'mismatch'}}},
        'then': {'properties': {'confidence': {'maximum': 1 if violation != 'allOf' else .5}}}}]
    original_schema = copy.deepcopy(schema)
    original_request = client.request
    async def request(method, params, timeout=30):
        if method == 'turn/start':
            def check(node):
                if isinstance(node, dict):
                    assert not {'uniqueItems', 'allOf', 'if', 'then'} & node.keys()
                    for value in node.values():
                        check(value)
                elif isinstance(node, list):
                    for value in node:
                        check(value)
            check(params['outputSchema'])
        response = await original_request(method, params, timeout)
        if method == 'thread/read' and violation == 'uniqueItems':
            message = response['thread']['turns'][0]['items'][-1]
            payload = json.loads(message['text'])
            payload['observations'] *= 2
            message['text'] = json.dumps(payload)
        return response
    client.request = request
    images = [('SOURCE', 'image/jpeg', source), ('MAP', 'image/jpeg', source)]
    host = {'source_map_receipt': {'model_source_sha256': hashlib.sha256(source).hexdigest(),
                                 'map_image_sha256': hashlib.sha256(source).hexdigest()}}
    try:
        if violation:
            with pytest.raises(RetryableProviderError, match='native_visual_waiting') as caught:
                await provider.compare_source_map(story, schema, 'Geometry operation', images,
                                                  {'attempt_id': 'native-subset'}, host)
            assert isinstance(caught.value.__cause__, ValidationError)
            assert receipts[-1]['phase'] == 'failed'
        else:
            result = await provider.compare_source_map(story, schema, 'Geometry operation', images,
                                                      {'attempt_id': 'native-subset'}, host)
            assert result['receipt']['phase'] == 'completed'
            receipt = result['receipt']
            binding = {**receipt['binding'], **{key: receipt[key] for key in (
                'thread_id', 'turn_id', 'phase', 'profile_verified', 'image_transport', 'frozen_source_map')}}
            await provider.compare_source_map(story, {}, 'Changed input', [], binding, {})
            assert sum(method == 'turn/start' for method, _ in client.calls) == 1
        frozen = receipts[-1]['frozen_source_map']
        assert frozen['host_contract']['properties']['observations']['uniqueItems'] is True
        assert frozen['host_contract']['allOf'] == original_schema['allOf']
        assert frozen['host_only_constraint_paths']
        assert schema == original_schema and len(sends) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_source_map_native_citation_choice_is_satisfiable_and_host_proof_unchanged(tmp_path):
    from jsonschema import Draft202012Validator, ValidationError
    from street_story.identity_proof import architectural_text_decision_schema
    provider, client, source, story, _context, receipts, sends, _finalized = setup(tmp_path)
    correspondence = architectural_text_decision_schema(
        ['osm:way:1'], ['article:1'], source_span_refs=['article:1:span:0'],
        structural=True)['properties']['correspondences']
    schema = {'type': 'object', 'properties': {'correspondences': correspondence},
              'required': ['correspondences'], 'additionalProperties': False}
    original_schema = copy.deepcopy(schema)
    answer = {'correspondences': [{'article_id': 'article:1',
        'source_span_ref': 'article:1:span:0', 'source_observation': 'Observed facade',
        'status': 'stable_match', 'reason': 'Model comparison', 'feature_kind': 'roof_form'}]}
    # Use an actually issued structural feature, without changing the proof schema.
    answer['correspondences'][0]['feature_kind'] = correspondence['items']['properties']['feature_kind']['enum'][0]
    original_request = client.request
    async def request(method, params, timeout=30):
        if method == 'turn/start':
            issued = params['outputSchema']['properties']['correspondences']['items']
            assert set(issued['required']) == set(issued['properties'])
            assert 'source_quote' not in issued['properties'] and 'oneOf' not in issued
            Draft202012Validator(params['outputSchema']).validate(answer)
            both = copy.deepcopy(answer)
            both['correspondences'][0]['source_quote'] = 'Another form'
            with pytest.raises(ValidationError):
                Draft202012Validator(params['outputSchema']).validate(both)
            with pytest.raises(ValidationError):
                Draft202012Validator(schema).validate(both)
        response = await original_request(method, params, timeout)
        if method == 'thread/read':
            response['thread']['turns'][0]['items'][-1]['text'] = json.dumps(answer)
        return response
    client.request = request
    host = {'source_map_receipt': {'model_source_sha256': hashlib.sha256(source).hexdigest(),
                                 'map_image_sha256': hashlib.sha256(source).hexdigest()}}
    result = await provider.compare_source_map(story, schema, 'Compare received evidence',
        [('SOURCE', 'image/jpeg', source), ('MAP', 'image/jpeg', source)],
        {'attempt_id': 'citation-choice'}, host)
    assert result['result'] == answer
    frozen = result['receipt']['frozen_source_map']
    assert frozen['host_contract'] == original_schema and schema == original_schema
    assert frozen['citation_transport'] == 'issued_span_ref'
    binding = {**result['receipt']['binding'], **{key: result['receipt'][key] for key in (
        'thread_id', 'turn_id', 'phase', 'profile_verified', 'image_transport', 'frozen_source_map')}}
    await provider.compare_source_map(story, {}, 'Changed input', [], binding, {})
    assert receipts[-1]['frozen_source_map'] == frozen
    assert len(sends) == 1 and sum(method == 'turn/start' for method, _ in client.calls) == 1
    await provider.close()


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
    assert receipts[-1]['provider_send_state'] == 'not_sent'
    assert receipts[-1]['retry_safe'] is True
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
    binding = {key: first['receipt'][key] for key in ('thread_id', 'turn_id', 'profile_verified', 'quota_permission', 'image_transport', 'image_preparation')}
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
    binding = {'image_preparation': result['receipt']['image_preparation'], 'image_transport': 'inline_data_uri_v1', 'attempt_id': 'first', 'phase': 'prompt_intent', 'thread_id': result['receipt']['thread_id'],
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


@pytest.mark.asyncio
@pytest.mark.parametrize('source_map', [False, True])
async def test_admission_counts_actual_schema_instructions_labels_and_unicode_separately(tmp_path, source_map):
    provider, client, source, story, context, _, _, _ = setup(tmp_path)
    workloads = []
    original = provider.admission
    @asynccontextmanager
    async def observed(binding, workload):
        workloads.append(dict(workload))
        async with original(binding, workload) as lease:
            yield lease
    provider.admission = observed
    schema = copy.deepcopy(VERDICT_SCHEMA)
    schema['description'] = 'Архитектурное описание. ' * 300
    context['observation'] = 'Кириллица остаётся Unicode'
    if source_map:
        host = {'source_map_receipt': {'model_source_sha256': hashlib.sha256(source).hexdigest(),
                                     'map_image_sha256': hashlib.sha256(source).hexdigest()}}
        result = await provider.compare_source_map(story, schema, 'Описание SOURCE и MAP',
            [('SOURCE', 'image/jpeg', source), ('MAP', 'image/jpeg', source)], {'attempt_id': 'full-envelope'}, host)
    else:
        result = await provider.compare_visual(None, story, schema, context, {'attempt_id': 'full-envelope'})
    thread = next(params for method, params in client.calls if method == 'thread/start')
    turn = next(params for method, params in client.calls if method == 'turn/start')
    # Compare admission with the actual two emitted RPCs, excluding inline
    # image data which the same installed estimator accounts separately.
    actual = {'input': [part for part in turn['input'] if part['type'] == 'text'],
              'outputSchema': turn['outputSchema'], 'baseInstructions': thread['baseInstructions'],
              'developerInstructions': thread['developerInstructions']}
    serialized = json.dumps(actual, ensure_ascii=False, separators=(',', ':'))
    assert workloads[0]['input_chars'] == len(serialized) > len(turn['input'][0]['text'])
    assert workloads[0]['max_output_tokens'] == 8192
    metadata = result['receipt']['request_input']
    assert metadata['text_chars'] == len(serialized)
    assert metadata['text_utf8_bytes'] == len(serialized.encode()) > metadata['text_chars']
    assert metadata['image_count'] == 2
    assert metadata['image_bytes'] == sum(len(base64.b64decode(part['url'].split(',', 1)[1]))
        for part in turn['input'] if part['type'] == 'image')
    assert metadata['unexposed_provider_context'] == 'unknown'
    await provider.close()
