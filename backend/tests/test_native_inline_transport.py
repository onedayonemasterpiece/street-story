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
