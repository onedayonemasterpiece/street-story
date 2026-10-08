from test_reference_image_codec import jpeg
from test_opencode_research import sheet
from street_story.reference_image_codec import normalize_reference
import base64
import json
from types import SimpleNamespace

import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.gemini import GeminiUnavailable
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.research_adapter import ProductResearchAdapter
from street_story.visual_attachments import direct_visual_parts, visual_operation_unit
from test_headless_vision import setup as google_setup
from test_native_vision import setup as native_setup
from test_opencode_research import Harness
from test_research_control import fixture


def direct(story, context, count=1):
    refs = [{'label': f'REF {i}', 'reference_id': f'reference-{i}',
             'candidate_id': 'web:gallery', 'source_url': f'https://example.org/ref-{i}.jpg'}
            for i in range(1, count+1)]
    context = {**context, 'references': refs}
    story = {**story, '_visual_image_parts': [
        {'label': 'SOURCE', 'mime_type': 'image/png', 'data': base64.b64encode(jpeg()).decode()},
        *[{'label': ref['label'], 'url': ref['source_url']} for ref in refs]],
        '_visual_reference_mapping': refs}
    return story, context


@pytest.mark.asyncio
async def test_google_source_and_ref_are_separate_prepared_bytes_and_public_url():
    provider, verdict, context, calls, first, second = google_setup()
    story, context = direct({'id': 'story', '_identity_generation': 1}, context)
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context)
    contents = calls[0]['contents']
    assert contents[0] == 'SOURCE' and contents[1].inline_data.data == normalize_reference(jpeg())[1]
    assert contents[1].inline_data.mime_type == 'image/jpeg'
    assert contents[2] == 'REF 1' and contents[3].inline_data.data == normalize_reference(jpeg((64,40)))[1]
    assert result['receipt']['image_attachments'] == 2
    assert 'sha256' not in json.dumps(result['receipt'])
    assert len(calls) == 1 and not second.operations


@pytest.mark.asyncio
async def test_google_group_preserves_order_and_stable_reference_mapping():
    from street_story.headless_identity import grouped_verdict_schema
    provider, verdict, context, calls, first, second = google_setup()
    story, context = direct({'id': 'story'}, context, count=3)
    result = await provider.compare_visual(None, story, grouped_verdict_schema()[0], context)
    contents = calls[0]['contents']
    assert contents[::2][:-1] == ['SOURCE', 'REF 1', 'REF 2', 'REF 3']
    assert result['receipt']['image_attachments'] == 4
    assert result['receipt']['reference_mapping'] == context['references']


@pytest.mark.parametrize('alter', ['missing_source', 'wrong_label', 'duplicate_id', 'local_url'])
def test_attachment_mapping_faults_are_known_unsent(alter):
    story, context = direct({'id': 'story'}, {}, count=2)
    if alter == 'missing_source':
        story['_visual_image_parts'].pop(0)
    if alter == 'wrong_label':
        story['_visual_image_parts'][1]['label'] = 'REF 7'
    if alter == 'duplicate_id':
        context['references'][1]['reference_id'] = 'reference-1'
    if alter == 'local_url':
        story['_visual_image_parts'][1]['url'] = 'file:///disk/image.jpg'
    with pytest.raises(PermanentProviderError, match='visual_direct_attachments_invalid'):
        direct_visual_parts(story, context)


def test_operation_unit_uses_comparison_and_reference_ids_never_image_bytes():
    story, context = direct({'id': 'story', '_identity_generation': 3}, {'comparison_id': 'op-1'})
    before = visual_operation_unit(story, context)
    story['_visual_image_parts'][0]['data'] = base64.b64encode(b'different-bytes').decode()
    assert visual_operation_unit(story, context) == before == ['story', 3, 'op-1', ['reference-1']]
    assert visual_operation_unit(story, {**context, 'comparison_id': 'op-2'}) != before


@pytest.mark.asyncio
async def test_native_direct_image_urls_ram_no_image_file_and_permission_lease(tmp_path):
    provider, client, snapshot, story, context, receipts, sends, finalized = native_setup(tmp_path)
    for attempt in ('first', 'second'):
        result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context, {'attempt_id': attempt})
    turns = [params for method, params in client.calls if method == 'turn/start']
    images = [part for part in turns[0]['input'] if part['type'] == 'image']
    assert images[0]['url'].startswith('data:image/jpeg;base64,')
    assert base64.b64decode(images[0]['url'].split(',', 1)[1]) == normalize_reference(snapshot)[1]
    assert images[1]['url'].startswith('data:image/jpeg;base64,')
    assert base64.b64decode(images[1]['url'].split(',', 1)[1]) == normalize_reference(snapshot)[1]
    assert len(images) == 2 and not list(tmp_path.rglob('*.jpg'))
    assert 'localImage' not in json.dumps(turns) and 'sha256' not in json.dumps(receipts)
    assert sum(method == 'account/rateLimits/read' for method, _ in client.calls) == 1
    assert len(sends) == 2 and result['receipt']['quota_permission']['expires_at'] == 1900


@pytest.mark.asyncio
async def test_google_unknown_blocks_native_even_when_route_unavailable(tmp_path):
    service, sid, _photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    calls = []
    async def unknown(*args):
        calls.append('google')
        error = GeminiUnavailable(1200, 'timeout')
        error.receipt = {'model_attempts': [{'provider_send_state': 'possibly_sent', 'category': 'timeout'}]}
        raise error
    async def forbidden(*args):
        raise AssertionError('unknown cannot send Native')
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=unknown)
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=forbidden)
    story, context = direct({'id': sid}, {'comparison_id': 'owned-op'})
    for index in range(2):
        if index:
            adapter.primary_vision.available = False
        with pytest.raises(RetryableProviderError, match='pair_outcome_unknown'):
            await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
    assert calls == ['google']
    with service.store.connection() as db:
        rows = list(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=?", (sid,)))
    assert len(rows) == 1 and json.loads(rows[0][0])['phase'] == 'unknown'
    assert 'sha256' not in rows[0][0]


@pytest.mark.asyncio
async def test_opencode_direct_two_parts_roundtrip_readback_without_contact_sheet():
    h = Harness()
    h.result = {'status': 'mismatch'}
    story, context = direct({'id': 'story'}, {'comparison_id': 'op'})
    result = await h.adapter().compare_image(story['_visual_image_parts'], {'request_id': 'op'}, {'type': 'object'}, context)
    files = [part for part in h.parts if part['type'] == 'file']
    assert len(files) == 2 and files[0]['mime'] == 'image/jpeg'
    assert base64.b64decode(files[0]['url'].split(',', 1)[1]) == normalize_reference(jpeg())[1]
    assert files[1]['mime'] == 'image/jpeg'
    assert base64.b64decode(files[1]['url'].split(',', 1)[1]) == normalize_reference(sheet())[1]
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert 'sha256' not in json.dumps(result['receipt'])
    assert not result['sources'] and len(h.sends) == 1

@pytest.mark.asyncio
async def test_legacy_hash_bound_unknown_is_not_orphaned_by_new_operation_keys(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    async def forbidden(*args):
        raise AssertionError('legacy unresolved operation forbids a new send')
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=forbidden)
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=forbidden)
    story, context = direct({'id': sid}, {'comparison_id': 'new-direct-id'})
    receipt = {'phase': 'unknown', 'binding': {'story_id': sid, 'generation': 0, 'photo_sha256': photo}}
    with service.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   ('legacy', 'old-hash-key', sid, 'vision_google_pair', json.dumps(receipt), 1, 1))
    with pytest.raises(RetryableProviderError, match='legacy_outcome_unknown'):
        await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)

@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['not_sent', 'response_closed'])
async def test_known_google_failure_allows_native_without_repeating_closed_google(tmp_path, state):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    calls, native_calls = [], []
    async def google(*args):
        calls.append('entry')
        error = GeminiUnavailable(1200, 'refused')
        error.receipt = {'model_attempts': [{'provider_send_state': state,
            'category': 'quota_denied' if state == 'not_sent' else 'malformed_response'}]}
        raise error
    async def native(snapshot, story, schema, context, binding):
        native_calls.append('send')
        result = {'status': 'uncertain'}
        receipt = {'binding': binding, 'phase': 'completed', 'result': result}
        await adapter.checkpoint(binding, receipt)
        return {'result': result, 'receipt': receipt}
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=google)
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    story, context = direct({'id': sid}, {'comparison_id': 'owned-op'})
    for _ in range(2):
        result = await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
        assert result['result']['status'] == 'uncertain'
    assert len(calls) == (2 if state == 'not_sent' else 1)
    assert native_calls == ['send']


@pytest.mark.asyncio
async def test_native_unknown_original_readback_precedes_returning_google_route(tmp_path):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client = service, None
    calls = []
    async def forbidden(*args):
        raise AssertionError('unknown Native must reconcile before new Google send')
    async def readback(snapshot, story, schema, context, binding):
        calls.append(binding)
        assert binding['thread_id'] == 'original-thread' and binding['turn_id'] == 'original-turn'
        return {'result': {'status': 'uncertain'}, 'receipt': {'inference_performed': False}}
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=forbidden)
    adapter.native_vision = SimpleNamespace(available=False, compare_visual=readback)
    story, context = direct({'id': sid}, {'comparison_id': 'owned-op'})
    from street_story.service import canonical
    binding, saved = adapter.attempt(story, 'vision_native', canonical(visual_operation_unit(story, context)))
    await adapter.checkpoint(binding, {'binding': binding, 'phase': 'unknown',
        'thread_id': 'original-thread', 'turn_id': 'original-turn', 'profile_verified': True})
    result = await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
    assert len(calls) == 1 and result['receipt']['inference_performed'] is False


@pytest.mark.asyncio
async def test_direct_google_real_quota_denial_is_known_unsent(tmp_path):
    from types import MethodType
    from street_story.providers import GeminiClient
    provider, verdict, context, google_calls, first, second = google_setup()
    quota_calls, native_calls = [], []
    class Quota:
        async def run(self, key, timeout, size, invoke):
            quota_calls.append('denied')
            raise GeminiUnavailable(1200, 'shared_control_unavailable')
    async def forbidden(*args, **kwargs):
        raise AssertionError('quota denied before provider request')
    provider.client._generate = MethodType(GeminiClient._generate, provider.client)
    provider.client._provider_request = forbidden
    provider.client.research_routes = [('gemini-primary', None, Quota(), first)]
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.client, adapter.primary_vision = service, None, provider
    async def native(snapshot, story, schema, context, binding):
        native_calls.append('native')
        return {'result': {'status': 'uncertain'}, 'receipt': {}}
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    story, context = direct({'id': sid}, context)
    result = await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
    assert result['result']['status'] == 'uncertain'
    assert quota_calls == ['denied'] and native_calls == ['native']
    with service.store.connection() as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM research_provider_attempts WHERE role='vision_google_pair'").fetchone()[0])
    assert receipt['provider_send_state'] == 'not_sent' and receipt['retry_safe'] is True

@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['created', 'submitted'])
async def test_native_unsent_reserve_failure_can_fallback_but_unknown_cannot(tmp_path, phase):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service = service
    adapter.client = SimpleNamespace(model_id='model', endpoint='endpoint')
    service.store.cache_put('research-vision-verification-v1', {'model_id': 'model', 'endpoint': 'endpoint',
        'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}, service.store.now()+1000)
    adapter.primary_vision = SimpleNamespace(available=False)
    async def native(snapshot, story, schema, context, binding):
        await adapter.checkpoint(binding, {'phase': phase, 'binding': binding})
        raise RetryableProviderError('native_denied_or_unknown', retry_at=1200)
    opencode = []
    async def fallback(snapshot, story, schema, context):
        opencode.append('safe fallback')
        return {'result': {'status': 'uncertain'}, 'receipt': {}}
    adapter.native_vision = SimpleNamespace(available=True, compare_visual=native)
    adapter.compare_image = fallback
    story, context = direct({'id': sid}, {'comparison_id': 'op'})
    if phase == 'submitted':
        with pytest.raises(RetryableProviderError, match='native_denied_or_unknown'):
            await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
        assert not opencode
    else:
        result = await adapter.visual_verdict(None, story, VERDICT_SCHEMA, context)
        assert result['result']['status'] == 'uncertain' and opencode == ['safe fallback']

@pytest.mark.asyncio
async def test_google_default_url_loader_uses_existing_bounded_public_fetch_in_ram(monkeypatch):
    from street_story.headless_vision import HeadlessVisionProvider
    import street_story.article_media as media
    from street_story.reference_image_codec import MAX_DOWNLOAD_BYTES
    provider, verdict, context, calls, first, second = google_setup()
    fetched = []
    async def fetch(client, url, limit):
        fetched.append((url, limit))
        return url, 'image/jpeg', jpeg((32,64))
    monkeypatch.setattr(media, 'fetch_public', fetch)
    provider._load_public_reference = HeadlessVisionProvider._load_public_reference.__get__(provider)
    story, context = direct({'id': 'story'}, context)
    result = await provider.compare_visual(None, story, VERDICT_SCHEMA, context)
    assert fetched == [('https://example.org/ref-1.jpg', MAX_DOWNLOAD_BYTES)]
    assert calls[0]['contents'][3].inline_data.data == normalize_reference(jpeg((32,64)))[1]
    assert calls[0]['contents'][3].inline_data.mime_type == 'image/jpeg'
    assert len(calls) == 1 and result['receipt']['image_attachments'] == 2


@pytest.mark.asyncio
async def test_google_failed_reference_http_load_never_sends_partial_or_text_only(monkeypatch):
    from street_story.headless_vision import HeadlessVisionProvider
    import street_story.article_media as media
    provider, verdict, context, calls, first, second = google_setup()
    async def fetch(client, url, limit):
        raise TimeoutError('image read only; no inference sent')
    monkeypatch.setattr(media, 'fetch_public', fetch)
    provider._load_public_reference = HeadlessVisionProvider._load_public_reference.__get__(provider)
    story, context = direct({'id': 'story'}, context)
    with pytest.raises((TimeoutError, GeminiUnavailable)) as error:
        await provider.compare_visual(None, story, VERDICT_SCHEMA, context)
    assert not calls
    assert all(attempt['provider_send_state'] == 'not_sent' for attempt in error.value.receipt['model_attempts'])
