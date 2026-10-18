import copy

import pytest

from street_story.errors import RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from test_native_vision import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('code,category', [(-32600, 'invalid_request'),
                                         (-32601, 'method_not_found'),
                                         (-32602, 'invalid_params')])
async def test_authoritative_rpc_request_rejection_is_closed_before_finalize(tmp_path, code, category, caplog):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    class NativeAppServerError(RuntimeError):
        def __init__(self):
            self.code, self.data = code, {'private': 'secret-payload'}
            super().__init__('secret-payload')
    async def request(method, params, timeout=30):
        if method == 'turn/start':
            client.calls.append((method, copy.deepcopy(params)))
            raise NativeAppServerError()
        return await original(method, params, timeout)
    client.request = request
    with pytest.raises(RetryableProviderError, match='native_visual_waiting'):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    receipt = receipts[-1]
    assert receipt['phase'] == 'failed' and receipt['turn_id'] is None
    assert receipt['provider_send_state'] == 'not_sent' and receipt['retry_safe'] is True
    assert receipt['rpc_error'] == {'response_received': True, 'code': code,
                                    'category': category, 'turn_rejected': True, 'method': 'turn/start', 'message': '[redacted]'}
    assert len(sends) == 1 and finalized[-1][1] == 'aborted'
    assert finalized[-1][0]['actual_total_tokens'] == 0
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert 'secret-payload' not in str(receipt) + caplog.text
    assert f'code={code}' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [-32603, -32000, 429, None])
async def test_unclassified_rpc_response_keeps_original_unknown_and_never_resends(tmp_path, code):
    provider, client, snapshot, story, context, receipts, sends, finalized = setup(tmp_path)
    original = client.request
    class NativeAppServerError(RuntimeError):
        def __init__(self):
            self.code, self.data = code, {'error': 'private'}
            super().__init__('Server error')
    async def request(method, params, timeout=30):
        if method == 'turn/start':
            client.calls.append((method, copy.deepcopy(params)))
            raise NativeAppServerError()
        if method == 'thread/read':
            client.calls.append((method, copy.deepcopy(params)))
            return {'thread': {'turns': []}}
        return await original(method, params, timeout)
    client.request = request
    with pytest.raises(RetryableProviderError):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, {'attempt_id': 'first'})
    pending = receipts[-1]
    assert pending['phase'] == 'prompt_intent' and not pending.get('retry_safe')
    assert finalized[-1][1] == 'unknown'
    binding = {**pending['binding'], **{k: pending[k] for k in ('phase', 'thread_id', 'turn_id', 'profile_verified')}}
    provider.timeout = .02
    with pytest.raises(RetryableProviderError, match='outcome_unknown'):
        await provider.compare_visual(snapshot, story, VERDICT_SCHEMA, context, binding)
    assert sum(method == 'turn/start' for method, _ in client.calls) == 1
    assert len(sends) == 1 and finalized[-1][1] == 'unknown'
    assert receipts[-1]['phase'] == 'unknown'
