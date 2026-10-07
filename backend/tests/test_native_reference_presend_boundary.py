import copy

import httpx
import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.headless_identity import VERDICT_SCHEMA
from test_native_vision import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout])
async def test_reference_network_error_before_inference_closes_only_unsent_reference(tmp_path, caplog, error):
    provider, client, _, story, context, receipts, sends, finalized = setup(tmp_path)
    async def loader(url):
        raise error('Secret private payload https://private.invalid/key?token=secret')
    provider.public_image_loader = loader
    with pytest.raises(PermanentProviderError, match='native_vision:reference_unavailable'):
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': 'failed-ref'})
    assert client.calls == [] and sends == [] and finalized == []
    saved = receipts[-1]
    assert saved['phase'] == 'failed' and saved['provider_send_state'] == 'not_sent'
    assert saved['retry_safe'] is True and saved['error_type'] == 'PermanentProviderError'
    assert saved['reference_error_type'] == error.__name__
    assert saved['error_code'] == 'native_vision:reference_unavailable'
    assert 'submitted=False' in caplog.text and f'error_type={error.__name__}' in caplog.text
    assert 'private.invalid' not in caplog.text and 'token=secret' not in str(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['prompt_intent', 'submitted', 'unknown'])
async def test_reference_network_error_during_original_readback_preserves_unknown_fence(tmp_path, phase):
    provider, client, _, story, context, receipts, sends, finalized = setup(tmp_path)
    binding = {'attempt_id': 'original', 'thread_id': 'original-thread',
        'turn_id': 'original-turn', 'phase': phase, 'profile_verified': True,
        'image_transport': 'inline_data_uri_v1', 'provider_send_state': 'possibly_sent'}
    before = copy.deepcopy(binding)
    async def loader(url):
        raise httpx.RemoteProtocolError('public REF unavailable')
    provider.public_image_loader = loader
    with pytest.raises(RetryableProviderError, match='native_reference_readback_waiting') as error:
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context, binding)
    assert error.value.retry_at == provider.service.store.now() + 60
    assert binding == before and receipts == []  # original durable receipt untouched
    assert client.calls == [] and sends == [] and finalized == []


@pytest.mark.asyncio
async def test_real_headless_queue_closes_bad_ref_keeps_completed_peer_and_next_ref(tmp_path):
    from types import SimpleNamespace
    from test_parallel_identity_pairs import prepare, response
    svc, story, _ = prepare(tmp_path / 'queue', count=3)
    provider, client, _, _, _, receipts, sends, finalized = setup(tmp_path / 'native')
    async def loader(url):
        raise httpx.RemoteProtocolError('reference connection dropped')
    provider.public_image_loader = loader
    calls = []
    async def pair(route, snapshot, item, schema, context):
        calls.append(item['_visual_reference_mapping'][0]['candidate_id'])
        if route == 'native':
            return await provider.compare_visual(None, item, schema, context, {'attempt_id': 'bad-ref'})
        return response(item, 'closed-peer', 'match')
    svc.providers.research = SimpleNamespace(vision_available=True, vision_model='fixture',
        parallel_visual_routes=lambda: ('native', 'google'), visual_pair_route=pair)
    assert await svc.run_once(claim_kind='identity_visual')
    _, research = svc._identity_snapshot(story['id'])
    phases = [p['phase'] for p in research['visual_search_operation']['parallel_pairs']]
    assert phases == ['failed', 'completed']
    assert calls == ['gate:a', 'gate:b']
    assert svc.story(story['id'])['error'] is None
    assert svc.story(story['id'])['visual_identity']['status'] == 'match'
    assert svc.story(story['id'])['visual_identity']['candidate_id'] == 'gate:b'
    assert receipts[-1]['provider_send_state'] == 'not_sent'
    assert not client.calls and not sends and not finalized
    queue = research['visual_search_operation']
    assert any(c['candidate_id'] == 'gate:c' for c in queue['queue'])
