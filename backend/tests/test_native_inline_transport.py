import base64
import copy
import json

import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.native_vision import native_rpc_error
from test_native_vision import setup


@pytest.mark.asyncio
async def test_public_reference_inline_separate_parts_and_accounted_bytes(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    fetched = []
    async def loader(url):
        fetched.append(url)
        return 'image/jpeg', b'raw-reference-bytes'
    provider.public_image_loader = loader
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'inline'})
    inputs = next(p['input'] for method, p in client.calls if method == 'turn/start')
    assert [p['text'] for p in inputs if p['type'] == 'text'][1:] == ['SOURCE', 'REF 1']
    assert inputs[2]['url'] == 'data:image/jpeg;base64,' + base64.b64encode(snapshot).decode()
    assert inputs[4]['url'] == 'data:image/jpeg;base64,' + base64.b64encode(b'raw-reference-bytes').decode()
    assert fetched == ['https://example.org/ref.jpg']
    assert result['receipt']['image_transport'] == 'inline_data_uri_v1'
    assert len(sends) == 1 and finalized[-1][1] == 'completed'
    assert 'data:image' not in json.dumps(receipts)



@pytest.mark.asyncio
async def test_unsupported_public_mime_known_unsent_no_thread_or_model(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    async def loader(url):
        return 'image/svg+xml', b'<svg></svg>'
    provider.public_image_loader = loader
    with pytest.raises(PermanentProviderError, match='reference_not_image'):
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'svg'})
    assert client.calls == [] and sends == [] and finalized == []
    assert receipts[-1]['provider_send_state'] == 'not_sent' and receipts[-1]['retry_safe'] is True


@pytest.mark.asyncio
async def test_legacy_unknown_readback_keeps_original_https_no_fetch_or_send(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    async def request(method, params, timeout=30):
        if method == 'thread/read':
            client.calls.append((method, copy.deepcopy(params)))
            return {'thread': {'turns': []}}
        return await original(method, params, timeout)
    async def no_fetch(url):
        raise AssertionError('Legacy original inputs must not be changed')
    provider.public_image_loader, client.request = no_fetch, request
    provider.timeout = .02
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context,
            {'attempt_id': 'original', 'thread_id': 'original', 'turn_id': 'turn', 'phase': 'unknown', 'profile_verified': True})
    assert all(m in {'thread/read', 'turn/interrupt'} for m, _ in client.calls)
    assert sends == [] and receipts[-1]['phase'] == 'unknown'


def test_rpc_diagnostic_redacts_credentials_image_payload_and_url():
    class NativeAppServerError(RuntimeError):
        code, data = -32600, {'private': 'private-known-value'}
    exc = NativeAppServerError('invalid image data URL: https://example.org/ref.jpg?secret=yes data:image/png;base64,abc private-known-value Bearer token123 api_key=another-secret')
    diagnostic = native_rpc_error(exc)
    assert diagnostic['turn_rejected'] is True
    text = json.dumps(diagnostic)
    for secret in ['https://', 'base64,abc', 'private-known-value', 'token123', 'another-secret']:
        assert secret not in text
    assert 'invalid image data URL:' in diagnostic['message'] and len(diagnostic['message']) <= 512


@pytest.mark.asyncio
async def test_first_read_rpc_failure_observes_same_turn_then_completes(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    reads = 0
    class NativeAppServerError(RuntimeError):
        code, data = -32600, None
    async def request(method, params, timeout=30):
        nonlocal reads
        if method == 'thread/read':
            reads += 1
            if reads == 1:
                client.calls.append((method, copy.deepcopy(params)))
                raise NativeAppServerError('thread read is not ready https://private.invalid/path')
        return await original(method, params, timeout)
    client.request = request
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'read-race'})
    assert result['receipt']['phase'] == 'completed' and reads == 2
    error = result['receipt']['readback_rpc_error']
    assert error['method'] == 'thread/read' and 'turn_rejected' not in error
    assert 'https://' not in error['message']
    assert len(sends) == 1 and finalized[-1][1] == 'completed'
    assert sum(m == 'turn/start' for m, _ in client.calls) == 1
    assert len({p['threadId'] for m, p in client.calls if m == 'thread/read'}) == 1


@pytest.mark.asyncio
async def test_three_read_rpc_failures_leave_original_unknown_and_charged(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    class NativeAppServerError(RuntimeError):
        code, data = -32600, None
    async def request(method, params, timeout=30):
        if method == 'thread/read':
            client.calls.append((method, copy.deepcopy(params)))
            raise NativeAppServerError('thread cannot be read')
        return await original(method, params, timeout)
    client.request = request
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'read-race'})
    assert receipts[-1]['phase'] == 'unknown' and not receipts[-1].get('retry_safe')
    assert receipts[-1]['readback_retry_count'] == 3
    assert sum(m == 'turn/start' for m, _ in client.calls) == 1
    assert sum(m == 'thread/read' for m, _ in client.calls) == 3
    assert len(sends) == 1 and finalized[-1][1] == 'unknown'
    assert finalized[-1][0]['actual_total_tokens'] is None


def test_native_pair_and_group_schema_require_every_property_without_host_mutation():
    from street_story.headless_identity import grouped_verdict_schema
    from street_story.native_vision import visual_request
    original = copy.deepcopy(VERDICT_SCHEMA)
    context = {'references': [{'reference_id': 'r1', 'candidate_id': 'web:r1'},
                              {'reference_id': 'r2', 'candidate_id': 'web:r2'}],
               'physical_candidates': [{'candidate_id': 'osm:1'}]}
    def assert_strict(node):
        if isinstance(node, dict):
            if 'properties' in node:
                assert set(node['required']) == set(node['properties'])
                assert node['additionalProperties'] is False
            for value in node.values():
                assert_strict(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict(value)
    for schema in (VERDICT_SCHEMA, grouped_verdict_schema()[0]):
        before = copy.deepcopy(schema)
        contract, _ = visual_request(schema, context)
        assert_strict(contract)
        assert schema == before
        assert 'reference_subject_candidate_id' in contract['required']
        assert 'reference_subject_observations' in contract['required']
    assert VERDICT_SCHEMA == original


@pytest.mark.asyncio
async def test_terminal_native_schema_rejection_is_retained_without_request_refund(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    async def request(method, params, timeout=30):
        result = await original(method, params, timeout)
        if method == 'thread/read':
            turn = result['thread']['turns'][0]
            turn.update(status='failed', error={'codexErrorInfo': 'other',
                'message': "Invalid schema: Missing reference_subject_candidate_id https://private.invalid"})
        return result
    client.request = request
    with pytest.raises(RetryableProviderError, match='native_turn_failed'):
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'schema'})
    saved = receipts[-1]
    assert saved['phase'] == 'failed' and 'turn_error' in saved
    assert 'Missing reference_subject_candidate_id' in saved['turn_error']['message']
    assert 'https://' not in saved['turn_error']['message']
    assert not saved.get('retry_safe') and not saved.get('provider_send_state')
    assert len(sends) == 1 and finalized[-1][1] == 'completed'
    assert finalized[-1][0]['actual_total_tokens'] == 321
