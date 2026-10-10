"""Concurrent text/media readers share one bounded HTTP article acquisition."""
import asyncio
import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from street_story import article_media
from street_story.db import Store
from street_story.research_runs import begin_research_run, persist_source_version
from test_live_editor import make_service, mark_identity_ready

URL = 'https://archive.example/gate-history'
BODY = '<main><h1>Gate archive</h1><p>A sufficiently long literal public article about the history and architectural details of this historic gate.</p><img src="/gate.jpg" width="800" height="600"></main>'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'empty', 'encoding', 'no_body', 'short_body', 'redirect'])
async def test_cached_publisher_body_uses_actual_article_structure_or_fails_closed(tmp_path, failure):
    from street_story.providers import GeminiClient
    svc, _, session, _ = make_service(tmp_path)
    reader = GeminiClient(svc.settings, svc.store)
    url = 'https://www.prussia39.ru/sight/index.php?sid=1'
    passage = 'Здание построено в XIX веке. Фасад имеет лучковые перемычки и венчающий карниз. ' * 3
    body = '<td style="text-align: justify">' + passage + '</td>'
    if failure == 'no_body':
        body = ''
    if failure == 'short_body':
        body = '<td style="text-align: justify">Коротко.</td>'
    raw = ('<html><head><meta charset="windows-1251"></head><body>'
           '<table><td>Вход Регистрация Каталог пользователей ' * 5 + '</td>' + body + '</table></body></html>').encode('cp1251')
    if failure == 'empty':
        raw = b''
    if failure == 'encoding':
        raw = raw.replace(b'windows-1251', b'unsupported')
    key = 'public-article-acquisition-v1:' + hashlib.sha256(url.encode()).hexdigest()
    entry = {'body': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
             'final_url': url.replace('sid=1', 'sid=2') if failure == 'redirect' else url, 'mime': 'text/html'}
    svc.store.cache_put(key, entry, 86400)
    with svc.store.tx() as db:
        story = svc._story_row(db, session.resource_id)
        for run_id in ('prior-publisher-run', 'publisher-run'):
            begin_research_run(db, story_id=story['id'], poi_key='wiki:77', goal='History',
                expected_story_revision=story['revision'], identity_generation=0, run_id=run_id, now=svc.store.now())
        old = persist_source_version(db, run_id='prior-publisher-run', requested_url=url, final_url=url,
            title='Old generic read', content_type='text/html', http_status=200, redirect_chain=[],
            normalized_text='Previously frozen navigation and page text.', read_status='complete', now=svc.store.now()-86401)

    async def forbidden(request):
        pytest.fail('Cached reader must not make an HTTP request')

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        reader.search_http = client
        documents = await reader._fetch_page_documents([url], {'research_run_id': 'publisher-run'})
    with svc.store.connection() as db:
        assert db.execute('SELECT normalized_text FROM source_versions WHERE source_version_id=?',
                          (old['source_version_id'],)).fetchone()[0] == 'Previously frozen navigation and page text.'
        if failure:
            assert documents == {}
            assert db.execute('SELECT status FROM research_run_sources WHERE run_id=? AND url=?',
                              ('publisher-run', url)).fetchone()[0] == 'failed'
        else:
            document = documents[url]
            assert document['normalized_text'] == passage.strip()
            assert 'Регистрация' not in document['normalized_text']
            assert document['raw_content_sha256'] == entry['sha256']
            assert document['content_sha256'] == hashlib.sha256(passage.strip().encode()).hexdigest()
            assert document['source_encoding'] == 'windows-1251'
            assert document['read_status'] == 'complete' and document['chunks']
            assert document['source_version_id'] != old['source_version_id']
    assert svc.store.cache_get(key) == entry


@pytest.mark.asyncio
async def test_cached_windows1251_source_reaches_frozen_fact_reader_without_corruption(tmp_path, monkeypatch):
    from street_story.providers import GeminiClient
    import hashlib
    svc, _, session, _ = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    reader = GeminiClient(svc.settings, svc.store)
    passage = 'Центральный эркер завершается полукругом. Фасад имеет три оконные оси. ' * 3
    raw = ('<html><head><meta charset="windows-1251"></head><body><main>'
        '<h1>Описание здания</h1><p>' + passage + '</p></main></body></html>').encode('cp1251')
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html'}, content=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reader.search_http = client
        await article_media.cached_public_page(svc.store, client, URL)
        with svc.store.tx() as db:
            story = dict(svc._story_row(db, session.resource_id))
            begin_research_run(db, story_id=story['id'], poi_key='wiki:77', goal='Architecture',
                expected_story_revision=story['revision'], identity_generation=0, run_id='encoded-run', now=svc.store.now())
        documents = await reader._fetch_page_documents([URL], {'research_run_id': 'encoded-run'})
    document = documents[URL]
    assert len(requests) == 1
    assert passage.strip() in document['normalized_text']
    assert document['source_encoding'] == 'windows-1251'
    assert document['raw_content_sha256'] == hashlib.sha256(raw).hexdigest()
    assert document['source_version_id'] and document['chunks']


@pytest.mark.asyncio
async def test_simultaneous_real_text_and_media_paths_share_acquisition(tmp_path, monkeypatch):
    from street_story.providers import GeminiClient
    svc, _, session, _ = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    reader = GeminiClient(svc.settings, svc.store)
    request_started, second_reader_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, calls = [], 0
    original = article_media.cached_public_page

    async def watched(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_reader_started.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(article_media, 'cached_public_page', watched)

    async def handle(request):
        requests.append(request)
        request_started.set()
        await release.wait()
        return httpx.Response(200, headers={'content-type': 'text/html'}, text=BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as text_client, httpx.AsyncClient(transport=httpx.MockTransport(handle)) as media_client:
        reader.search_http = text_client
        with svc.store.tx() as db:
            story = dict(svc._story_row(db, session.resource_id))
            begin_research_run(db, story_id=story['id'], poi_key='wiki:77', goal='History',
                               expected_story_revision=story['revision'], identity_generation=0, run_id='text-run', now=svc.store.now())
        # Distinct Store objects must still lock the same physical cache database.
        media_service = SimpleNamespace(store=Store(svc.store.path))
        text = asyncio.create_task(reader._fetch_page_documents([URL], {'research_run_id': 'text-run'}))
        await asyncio.wait_for(request_started.wait(), 2)
        media = asyncio.create_task(article_media.article_candidates(media_service, story, [{'url': URL}], set(), http=media_client))
        await asyncio.wait_for(second_reader_started.wait(), 2)
        release.set()
        documents, candidates = await asyncio.gather(text, media)
    assert len(requests) == 1
    assert URL in documents and 'literal public article' in documents[URL]['normalized_text']
    assert candidates[0]['reference_image_urls'] == ['https://archive.example/gate.jpg']
    assert requests[0].headers['host'] == 'archive.example'
    assert requests[0].extensions['sni_hostname'] == 'archive.example'


@pytest.mark.asyncio
async def test_cancelled_reader_releases_acquisition_for_waiter(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handle(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, headers={'content-type': 'text/html'}, text=BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as first_client, httpx.AsyncClient(transport=httpx.MockTransport(handle)) as second_client:
        first = asyncio.create_task(article_media.cached_public_page(store, first_client, URL))
        await asyncio.wait_for(entered.wait(), 2)
        waiter = asyncio.create_task(article_media.cached_public_page(store, second_client, URL + '#media'))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await asyncio.wait_for(waiter, 2)
        cached = await article_media.cached_public_page(store, second_client, URL)
    assert len(requests) == 2  # Cancelled acquisition + one surviving request.
    assert cached == result and result[2] == BODY.encode()


@pytest.mark.asyncio
async def test_failed_acquisition_does_not_poison_followup_or_hold_a_lock(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    attempts = 0

    async def handle(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, headers={'content-type': 'text/html'}, text=BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await article_media.cached_public_page(store, client, URL)
        result = await article_media.cached_public_page(store, client, URL)
        assert await article_media.cached_public_page(store, client, URL + '#text') == result
    assert attempts == 2
