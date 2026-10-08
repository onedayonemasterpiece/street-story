import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest


from street_story import article_media as media, identity_discovery as discovery


class Store:
    def __init__(self, path):
        self.path, self.values = path, {}
    def cache_get(self, key):
        return self.values.get(key)
    def cache_put(self, key, value, _ttl):
        self.values[key] = value
    def now(self):
        return 1000


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(media, 'record_identity_event', lambda *_: None)
    monkeypatch.setattr(discovery, 'record_identity_event', lambda *_: None)
    return SimpleNamespace(store=Store(tmp_path / 'fixture-store'),
        _identity_snapshot=lambda _sid: ({'photo_sha256': 'fixture-upload'}, {'identity_generation': 0}))


async def resolver(_host):
    return '93.184.216.34'


@pytest.mark.asyncio
async def test_static_media_returns_before_js_and_resume_keeps_cursors(service):
    called = []
    async def browser(url):
        called.append(url)
        return 'Rendered gate', media.RenderedMedia([
            {'image_url': url + '/rear.jpg', 'article_url': url, 'kind': 'article_img'}],
            cursor=3, partial=False, slide_cursor=2)
    async def handler(_):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            content='<h1>Gate</h1><article class="swiper"><img src="/front.jpg"></article>')
    source = {'url': 'https://history.example/gate', 'gallery_cursor': 1, 'gallery_slide_cursor': 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = []
        result = await asyncio.wait_for(media.article_candidates(service, {'id': 'fixture'}, [source], set(),
            http=client, resolver=resolver, browser=browser, receipts=first), 0.5)
        assert not called
        assert result[0]['enumeration_status'] == 'partial'
        assert first[0]['static_media_delivered'] is True
        assert first[0]['gallery_cursor'] == 1
        assert result[0]['discovery_provenance']['static_media_delivered'] is True
        resumed = {**source, **{key: first[0][key] for key in
            ('gallery_cursor', 'gallery_slide_cursor', 'static_media_delivered')}}
        second = []
        result = await media.article_candidates(service, {'id': 'fixture'}, [resumed], set(),
            http=client, resolver=resolver, browser=browser, receipts=second)
        assert called == [source['url']]
        assert second[0]['status'] == 'completed'
        assert second[0]['gallery_cursor'] == 3
        assert second[0]['gallery_slide_cursor'] == 2
        assert len(result[0]['article_media']) == 2


@pytest.mark.asyncio
async def test_first_ready_does_not_wait_for_slow_page_or_lose_unreturned_urls(service):
    cancelled, active, peak = [], 0, 0
    gate = asyncio.Event()
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if request.url.path == '/fast':
                await asyncio.sleep(0)
                return httpx.Response(200, headers={'content-type': 'text/html'},
                    content='<article><img src="/gate.jpg"></article>')
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.append(request.url.path)
                raise
        finally:
            active -= 1
    sources = [{'url': 'https://history.example/' + name} for name in
               ['slow', 'fast', 'slow2', 'slow3', 'later4', 'later5']]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipts = []
        result = await asyncio.wait_for(media.article_candidates(service, {'id': 'fixture'}, sources, set(),
            http=client, resolver=resolver, receipts=receipts, first_ready=True), 0.5)
    assert len(result) == 1
    assert result[0]['url'] == sources[1]['url']
    assert peak <= 4 and active == 0
    assert not gate.is_set()
    assert set(cancelled) == {'/slow', '/slow2', '/slow3'}
    assert {r['url'] for r in receipts} == {s['url'] for s in sources}
    assert all(r['status'] == ('completed' if r['url'] == sources[1]['url'] else 'deferred')
               for r in receipts)
    assert service.store.values  # Completed immutable HTTP body survives.


@pytest.mark.asyncio
async def test_all_ready_receipts_survive_first_completion_slice(service):
    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            content='<article><img src="/gate.jpg"></article>')
    sources = [{'url': 'https://history.example/' + str(i)} for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipts = []
        result = await media.article_candidates(service, {'id': 'fixture'}, sources, set(),
            http=client, resolver=resolver, receipts=receipts, first_ready=True)
    assert {item['url'] for item in result} == {s['url'] for s in sources}
    assert all(r['status'] == 'completed' for r in receipts)
    assert len(receipts) == 3


@pytest.mark.asyncio
async def test_reader_completed_after_wait_snapshot_keeps_candidate_with_receipt(service, monkeypatch):
    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            content='<article><img src="/gate.jpg"></article>')
    original_wait = asyncio.wait
    async def completion_snapshot(tasks, **_):
        done, pending = await original_wait(tasks)
        first = next(iter(done))
        return {first}, pending | (done - {first})
    monkeypatch.setattr(media.asyncio, 'wait', completion_snapshot)
    sources = [{'url': 'https://history.example/' + str(i)} for i in range(2)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipts = []
        result = await media.article_candidates(service, {'id': 'fixture'}, sources, set(),
            http=client, resolver=resolver, receipts=receipts, first_ready=True)
    assert {item['url'] for item in result} == {s['url'] for s in sources}
    assert all(r['status'] == 'completed' for r in receipts)


@pytest.mark.asyncio
async def test_cancelled_prefetch_closes_owned_client(service, monkeypatch):
    closed = []
    gate = asyncio.Event()
    class Client:
        async def aclose(self):
            closed.append(True)
    monkeypatch.setattr(media.httpx, 'AsyncClient', lambda **_: Client())
    async def acquire(*_, **__):
        await gate.wait()
    monkeypatch.setattr(media, 'cached_public_page', acquire)
    receipts = []
    task = asyncio.create_task(media.article_candidates(service, {'id': 'fixture'},
        [{'url': 'https://history.example/slow'}], set(), receipts=receipts, first_ready=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert receipts == [{'url': 'https://history.example/slow', 'status': 'deferred'}]


@pytest.mark.asyncio
async def test_failed_js_keeps_partial_static_and_cursor(service):
    async def handler(_):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            content='<article class="swiper"><img src="/gate.jpg"></article>')
    async def browser(_):
        raise TimeoutError('fixture slow JS')
    source = {'url': 'https://history.example/gate', 'static_media_delivered': True,
              'gallery_cursor': 4, 'gallery_slide_cursor': 2}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipts = []
        result = await media.article_candidates(service, {'id': 'fixture'}, [source], set(),
            http=client, resolver=resolver, browser=browser, receipts=receipts)
    assert result[0]['enumeration_status'] == 'partial'
    assert receipts[0]['gallery_cursor'] == 4
    assert receipts[0]['static_media_delivered'] is True


@pytest.mark.asyncio
async def test_recovery_uses_first_ready_and_returns_useful_page(service, monkeypatch):
    service.providers = SimpleNamespace(gemini=SimpleNamespace(_generate=lambda: None,
        executor=SimpleNamespace(execute=lambda *args: None)))
    sources = [{'url': 'https://history.example/' + str(i)} for i in range(30)]
    history = {'sources': sources, 'pages': {}}
    async def suggest(*_):
        return '', [], 'visual', ''
    async def search(*_, **__):
        return sources
    retained = []
    def retain(*_, receipts=(), articles=(), **_updates):
        retained.append((list(receipts), list(articles)))
        return history
    async def articles(*_, receipts, first_ready):
        assert first_ready is True
        receipts.extend([{'url': s['url'], 'status': 'completed' if i == 1 else 'deferred'}
                         for i, s in enumerate(sources)])
        return [{'candidate_id': 'web:fixture', 'url': sources[1]['url'],
                 'discovery_provenance': sources[1], 'reference_image_urls': ['https://history.example/gate.jpg']}]
    monkeypatch.setattr(discovery, 'suggest', suggest)
    monkeypatch.setattr(discovery, 'web_image_sources', search)
    monkeypatch.setattr(discovery, '_retain_article_discovery', retain)
    monkeypatch.setattr(discovery, '_claim_article_query', lambda *_: ('fixture-claim', {}))
    import street_story.article_media as actual
    monkeypatch.setattr(actual, 'article_candidates', articles)
    result, candidates = await discovery.recover(service, {'id': 'fixture', 'photo_sha256': 'fixture-upload'}, '', [], set())
    assert result['_article_media_pending'] is True and result['status'] == 'uncertain'
    assert candidates[0]['url'] == sources[1]['url']
    assert len(retained[-1][0]) == 30


@pytest.mark.asyncio
async def test_recovery_does_not_hold_cached_partial_for_new_reads(service, monkeypatch):
    service.providers = SimpleNamespace(gemini=SimpleNamespace(_generate=lambda: None,
        executor=SimpleNamespace(execute=lambda *args: None)))
    ready = {'candidate_id': 'web:ready', 'url': 'https://history.example/ready'}
    history = {'sources': [{'url': ready['url']}, {'url': 'https://history.example/slow'}],
               'pages': {ready['url']: {'status': 'partial', 'candidates': [ready],
                          'source': {'gallery_cursor': 3, 'static_media_delivered': True}}}}
    async def suggest(*_):
        return '', [], 'visual', ''
    async def search(*_, **__):
        return []
    monkeypatch.setattr(discovery, 'suggest', suggest)
    monkeypatch.setattr(discovery, 'web_image_sources', search)
    monkeypatch.setattr(discovery, '_retain_article_discovery', lambda *_args, **_kwargs: history)
    monkeypatch.setattr(discovery, '_claim_article_query', lambda *_: ('fixture-claim', {}))
    async def forbidden(*_, **__):
        raise AssertionError('cached partial must return before other acquisition')
    import street_story.article_media as actual
    monkeypatch.setattr(actual, 'article_candidates', forbidden)
    result, candidates = await discovery.recover(service, {'id': 'fixture', 'photo_sha256': 'fixture-upload'}, '', [], set())
    assert candidates == [ready] and result['_article_media_pending'] is True
    assert history['pages'][ready['url']]['status'] == 'partial'


def test_partial_static_continuation_is_durable_and_deferred_does_not_fake_failure(tmp_path):
    import hashlib
    from test_identity_lifecycle import make_service
    from test_reference_image_codec import jpeg
    from street_story.service import ConflictError
    service, _ = make_service(tmp_path)
    photo = jpeg()
    created = service.create_story(key='r3-media', client_story_id='r3-media',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg',
        photo_bytes=photo, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    story, _ = service._identity_snapshot(created['id'])
    url = 'https://history.example/gate'
    discovery._retain_article_discovery(service, story, [{'url': url}])
    candidate = {'candidate_id': 'web:fixture', 'url': url, 'discovery_provenance': {'url': url}}
    receipt = {'url': url, 'status': 'partial', 'gallery_cursor': 2,
               'gallery_slide_cursor': 1, 'static_media_delivered': True}
    saved = discovery._retain_article_discovery(service, story, [], receipts=[receipt], articles=[candidate])
    assert saved['pages'][url]['source']['static_media_delivered'] is True
    assert saved['pages'][url]['candidates'] == [candidate]
    saved = discovery._retain_article_discovery(service, story, [],
        receipts=[{'url': url, 'status': 'deferred'}])
    assert saved['pages'][url]['status'] == 'partial'
    assert saved['pages'][url]['attempts'] == 1
    with service.store.tx() as db:
        raw = db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0]
        research = json.loads(raw)
        research['research_controls'] = {'identity': {'revision': 1, 'state': 'stopped',
            'photo_sha256': story['photo_sha256'], 'identity_generation': 0}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    with pytest.raises(ConflictError):
        discovery._retain_article_discovery(service, story, [], receipts=[receipt], articles=[candidate])
