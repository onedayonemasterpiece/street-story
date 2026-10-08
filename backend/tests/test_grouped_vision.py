from io import BytesIO
from PIL import Image
from street_story.reference_image_codec import MAX_EDGE, MAX_LIVE_BYTES, normalize_reference
import base64
from copy import deepcopy
from types import SimpleNamespace
import pytest
from test_headless_vision import setup
from test_reference_image_codec import jpeg
from street_story.headless_identity import grouped_verdict_schema
from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.research_adapter import ProductResearchAdapter
from street_story.gemini import GeminiUnavailable


def grouped():
    provider, verdict, context, calls, first, second = setup()
    images = [jpeg((320, 240)), jpeg((400, 240)), jpeg((420, 240))]
    refs = [{'label': f'REF {i}', 'candidate_id': 'web:gallery', 'reference_id': f'frame-{i}'} for i in (1, 2)]
    context['references'] = refs
    story = {'_visual_reference_mapping': refs,
             '_visual_image_parts': [{'label': label, 'mime_type': 'image/jpeg',
                 'data': base64.b64encode(pixels).decode()} for label, pixels in zip(['SOURCE', 'REF 1', 'REF 2'], images)]}
    verdict['reference_verdicts'] = [{**deepcopy(verdict), 'reference_id': ref['reference_id']} for ref in refs]
    return provider, verdict, context, calls, first, second, story, images


@pytest.mark.asyncio
async def test_group_uses_separate_sdk_parts_and_exact_frame_mapping():
    provider, verdict, context, calls, first, second, story, images = grouped()
    result = await provider.compare_visual(jpeg(), story, grouped_verdict_schema()[0], context)
    contents = calls[0]['contents']
    assert [contents[i] for i in (0, 2, 4)] == ['SOURCE', 'REF 1', 'REF 2']
    assert [contents[i].inline_data.data for i in (1, 3, 5)] == [normalize_reference(data)[1] for data in images]
    for index in (1, 3, 5):
        raw = contents[index].inline_data.data
        assert len(raw) <= MAX_LIVE_BYTES
        with Image.open(BytesIO(raw)) as image:
            assert max(image.size) <= MAX_EDGE
    assert len(calls) == 1 and not second.operations
    assert result['receipt']['image_attachments'] == 3
    assert result['receipt']['reference_mapping'] == context['references']
    assert 'reference_verdicts' in contents[-1]
    assert 'Не придумывай видимый сквозь дверь интерьер' in contents[-1]
    assert 'одновременно в SOURCE и этом REF' in contents[-1]


@pytest.mark.asyncio
async def test_partial_invalid_group_element_is_left_for_common_host_gate():
    provider, verdict, context, calls, first, second, story, images = grouped()
    verdict['reference_verdicts'][1] = {'reference_id': 'frame-2', 'confidence': 'invalid'}
    result = await provider.compare_visual(jpeg(), story, grouped_verdict_schema()[0], context)
    assert result['result']['reference_verdicts'][0]['reference_id'] == 'frame-1'
    assert result['result']['reference_verdicts'][1]['confidence'] == 'invalid'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['permuted_labels', 'duplicate_id', 'bad_base64', 'different_mapping'])
async def test_invalid_group_transport_rejected_before_admission(fault):
    provider, verdict, context, calls, first, second, story, images = grouped()
    if fault == 'permuted_labels':
        story['_visual_image_parts'][1:3] = story['_visual_image_parts'][2:0:-1]
    elif fault == 'duplicate_id':
        story['_visual_reference_mapping'][1]['reference_id'] = 'frame-1'
    elif fault == 'bad_base64':
        story['_visual_image_parts'][1]['data'] = '!!'
    elif fault == 'different_mapping':
        story['_visual_reference_mapping'] = deepcopy(context['references'])
        story['_visual_reference_mapping'][0]['candidate_id'] = 'web:other'
    with pytest.raises(PermanentProviderError, match='visual_direct_attachments_invalid'):
        await provider.compare_visual(jpeg(), story, grouped_verdict_schema()[0], context)
    assert not calls and not first.operations and not second.operations


@pytest.mark.asyncio
async def test_group_fallback_requests_pair_without_native_send(tmp_path):
    provider, verdict, context, calls, first, second, story, images = grouped()
    first.failure = second.failure = GeminiUnavailable(1200, 'blocked')
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.primary_vision = provider
    adapter.native_vision = SimpleNamespace(available=True)
    adapter.service = provider.service
    adapter.client = None
    story.update(id='story', photo_sha256='a' * 64)
    install_ledger(adapter, story, tmp_path)
    with pytest.raises(PermanentProviderError, match='research_visual_group_pair_required'):
        await adapter.visual_verdict(jpeg(), story, grouped_verdict_schema()[0], context)
    assert not calls


def install_ledger(adapter, story, tmp_path):
    from test_research_control import fixture
    service, sid, photo = fixture(tmp_path)
    adapter.service = service
    story['id'] = sid
    durable = {}
    saved_checkpoint = adapter.checkpoint
    async def checkpoint(binding, receipt):
        await saved_checkpoint(binding, receipt)
        durable.clear()
        durable.update(receipt)
    adapter.checkpoint = checkpoint
    return durable


@pytest.mark.asyncio
async def test_unknown_grouped_send_is_durable_and_never_becomes_pair_fallback(tmp_path):
    provider, verdict, context, calls, first, second, story, images = grouped()
    story.update(id='story', photo_sha256='a' * 64)
    async def timed_out(*args, **kwargs):
        kwargs['before_provider_send']()
        calls.append('sent')
        raise TimeoutError
    provider.client._generate = timed_out
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.primary_vision, adapter.native_vision, adapter.service, adapter.client = provider, None, provider.service, None
    durable = install_ledger(adapter, story, tmp_path)
    with pytest.raises(RetryableProviderError, match='group_outcome_unknown'):
        await adapter.visual_verdict(jpeg(), story, grouped_verdict_schema()[0], context)
    assert durable['phase'] == 'unknown' and durable['provider_send_state'] == 'possibly_sent'
    # Availability can change after a failure; that never erases the unknown intent.
    provider.service.store.cache_get = lambda key: {}
    adapter.native_vision = SimpleNamespace(available=True)
    with pytest.raises(RetryableProviderError, match='group_outcome_unknown'):
        await adapter.visual_verdict(jpeg(), story, grouped_verdict_schema()[0], context)
    assert calls == ['sent']


@pytest.mark.asyncio
async def test_group_executor_does_not_retry_unknown_timeout_on_another_key_or_model():
    from street_story.gemini import GeminiExecutor, GeminiPolicy
    provider, verdict, context, calls, first, second, story, images = grouped()
    class Pool:
        policy = GeminiPolicy()
        keys = (SimpleNamespace(get_secret_value=lambda: 'unit-test-key'),) * 2
        ids = ('key0', 'key1')
        finished = []
        def reserve(self, operation, excluded, timeout):
            return next((key for key in self.ids if key not in excluded), None)
        def finish(self, key, operation, failure):
            self.finished.append(key)
        def event(self, *args, **kwargs):
            pass
        def clock(self):
            return 1000
        def all_enabled_keys_blocked_for_model(self, operation):
            return False
        def unavailable(self, operation):
            return GeminiUnavailable(1200, 'all_keys_unavailable')
    pool = Pool()
    provider.client.research_routes[0] = ('gemini-primary', pool, 'quota-primary', GeminiExecutor(pool))
    async def timed_out(*args, **kwargs):
        kwargs['before_provider_send']()
        calls.append('sent')
        raise TimeoutError
    provider.client._generate = timed_out
    with pytest.raises(GeminiUnavailable) as failure:
        await provider.compare_visual(jpeg(), story, grouped_verdict_schema()[0], context)
    assert calls == ['sent'] and pool.finished == ['key0'] and not second.operations
    assert pool.policy.max_failover_keys == 32
    assert failure.value.receipt['category'] == 'visual_outcome_unknown'


@pytest.mark.asyncio
async def test_valid_source_above_live_byte_budget_prepares_group_frames():
    import io
    import random
    from PIL import Image
    provider, verdict, context, calls, first, second, story, images = grouped()
    output = io.BytesIO()
    Image.frombytes('RGB', (1280, 720), random.Random(37).randbytes(1280 * 720 * 3)).save(output, 'JPEG', quality=90)
    pixels = output.getvalue()
    assert 480 * 1024 < len(pixels) < 2 * 1024 * 1024
    story['_visual_image_parts'][0]['data'] = base64.b64encode(pixels).decode()
    response = await provider.compare_visual(jpeg(), story, grouped_verdict_schema()[0], context)
    prepared = calls[0]['contents'][1].inline_data.data
    assert prepared == normalize_reference(pixels)[1]
    assert len(prepared) <= MAX_LIVE_BYTES
    with Image.open(BytesIO(prepared)) as image:
        assert image.size == (1280, 720)
    assert response['receipt']['reference_mapping'] == context['references']
    assert response['receipt']['image_attachments'] == 3


@pytest.mark.asyncio
async def test_too_many_group_parts_are_rejected_before_admission():
    provider, verdict, context, calls, first, second, story, images = grouped()
    story['_visual_image_parts'] *= 2
    with pytest.raises(PermanentProviderError, match='visual_direct_attachments_invalid'):
        await provider.compare_visual(None, story, grouped_verdict_schema()[0], context)
    assert not calls and not first.operations and not second.operations


@pytest.mark.asyncio
async def test_identical_reference_bytes_with_distinct_ids_are_not_pixel_filtered():
    provider, verdict, context, calls, first, second, story, images = grouped()
    story['_visual_image_parts'][2]['data'] = story['_visual_image_parts'][1]['data']
    response = await provider.compare_visual(None, story, grouped_verdict_schema()[0], context)
    assert response['receipt']['image_attachments'] == 3
    assert response['receipt']['reference_mapping'] == context['references']
