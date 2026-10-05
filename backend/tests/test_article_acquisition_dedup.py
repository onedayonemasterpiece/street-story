"""Concurrent text/media readers share one bounded HTTP article acquisition."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from street_story import article_media
from street_story.db import Store
from street_story.research_runs import begin_research_run
from test_live_editor import make_service, mark_identity_ready

URL = 'https://archive.example/gate-history'
BODY = '<main><h1>Gate archive</h1><p>A sufficiently long literal public article about the history and architectural details of this historic gate.</p><img src="/gate.jpg" width="800" height="600"></main>'


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
