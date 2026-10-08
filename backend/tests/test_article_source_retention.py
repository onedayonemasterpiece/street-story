"""Per-pass budgets never discard the discovered URL backlog."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from street_story import article_media, identity_discovery
from street_story.research_control import stop_research
from street_story.service import ConflictError
from test_visual_search_continuation import prepared


def article(url, index=0, *, status='completed', cursor=0):
    return {'candidate_id': f'web:fixture-{index}', 'name': 'Fetched article', 'url': url,
            'reference_image_urls': [url + '/image.jpg'], 'discovery': 'web_article_media',
            'enumeration_status': status,
            'discovery_provenance': {'url': url, 'gallery_cursor': cursor}}


def images(svc):
    loaded = []
    async def load(candidates, limit, *, story_id, evidence):
        item = candidates[0]
        url = item['reference_image_urls'][0]
        loaded.append(url)
        evidence.append({'candidate_id': item['candidate_id'], 'source_url': url, 'article_url': item.get('url')})
        return [(item['candidate_id'], 'image/jpeg', url)]
    svc._candidate_reference_images = load
    return loaded


def reject(adapter, session, reply, index=0):
    adapter._record_place_comparison(session, f'retained-verdict-{index}', {
        'comparison_id': reply['comparison_id'], 'status': 'mismatch', 'candidate_id': '',
        'confidence': 1, 'observations': ['Different object in the reference.'], 'alternative_candidate_ids': []})


@pytest.mark.parametrize('state', ['ready', 'retry', 'running', 'done'])
def test_new_selected_source_wakes_only_waiting_visual_job_of_current_generation(tmp_path, state):
    svc, adapter, topic, sessions = prepared(tmp_path)
    with svc.store.tx() as db:
        job = svc._enqueue_job(db, topic['id'], 'identity_visual', 'late-source-test',
                               {'identity_generation': 0})
        old = svc._enqueue_job(db, topic['id'], 'identity_visual', 'old-generation-test',
                               {'identity_generation': 1})
        future = svc.store.now() + 300
        db.execute('UPDATE jobs SET state=?,available_at=? WHERE id IN (?,?)',
                   (state, future, job, old))
    story, _ = svc._identity_snapshot(topic['id'])
    identity_discovery._retain_article_discovery(svc, story, [{'url': 'https://photos.example/new-source'}])
    with svc.store.connection() as db:
        current = dict(db.execute('SELECT state,available_at FROM jobs WHERE id=?', (job,)).fetchone())
        previous = dict(db.execute('SELECT state,available_at FROM jobs WHERE id=?', (old,)).fetchone())
    assert current['state'] == state
    assert (current['available_at'] < future) is (state in {'ready', 'retry'})
    assert previous['available_at'] == future


@pytest.mark.asyncio
async def test_search_keeps_every_discovered_url_and_existing_completed_media(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    batches = [[{'url': f'https://example.com/article-{i}'} for i in range(start, start + 80)]
               for start in (0, 60)]
    async def search(*args, **kwargs):
        return batches.pop(0)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    session = sessions()
    assert len((await adapter._find_place_articles(session, {'query': 'first view'}))['sources']) == 80
    story, _ = svc._identity_snapshot(topic['id'])
    candidate = article('https://example.com/article-0')
    identity_discovery._retain_article_discovery(svc, story, [],
        receipts=[{'url': candidate['url'], 'status': 'completed'}], articles=[candidate])
    assert len((await adapter._find_place_articles(session, {'query': 'second view'}))['sources']) == 80
    _, research = svc._identity_snapshot(topic['id'])
    assert len(research['identity_article_discovery']['sources']) == 140
    assert len(session.state['identity_article_sources']) == 140
    assert research['identity_article_discovery']['pages'][candidate['url']]['candidates'] == [candidate]


@pytest.mark.asyncio
async def test_article_batch_has_deferred_receipt_for_every_unscheduled_url_and_bounded_parallelism(tmp_path, monkeypatch):
    svc, _, _, _ = prepared(tmp_path)
    monkeypatch.setattr(article_media, 'MAX_PAGES', 6)
    active = peak = 0
    requested = []
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        requested.append(request.url.path)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, headers={'content-type': 'text/html'}, text='<article><img src="/image.jpg"></article>')
    async def resolver(host):
        return '93.184.216.34'
    sources = [{'url': f'https://example.com/article-{i}'} for i in range(35)]
    receipts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await article_media.article_candidates(svc, {'id': 'unknown'}, sources, set(),
            http=http, resolver=resolver, receipts=receipts)
    assert len(requested) == len(result) == 6
    assert peak <= 4
    assert {receipt['url'] for receipt in receipts} == {source['url'] for source in sources}
    assert sum(receipt['status'] == 'deferred' for receipt in receipts) == 29


@pytest.mark.asyncio
async def test_api_urls_beyond_twenty_continue_after_failed_pages_without_fake_exhaustion(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    sources = [f'https://example.com/article-{i}' for i in range(85)]
    requests = []
    async def fetch(service, story, batch, excluded, *, receipts):
        url = batch[0]['url']
        requests.append(url)
        if url == sources[-1]:
            receipts.append({'url': url, 'status': 'completed'})
            return [article(url)]
        receipts.append({'url': url, 'status': 'temporary_failure'})
        return []
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    loaded = images(svc)
    session = sessions()
    first = await adapter._compare_place_images(session, {'query': 'gate', 'article_urls': sources})
    assert first['partial'] and len(requests) == 4
    _, research = svc._identity_snapshot(topic['id'])
    assert len(research['visual_search_operation']['sources']) == 85
    for _ in range(25):
        now = svc.store.now()
        svc.store.now = lambda now=now: now + 30
        before = len(requests)
        reply = await adapter._compare_place_images(session, {})
        assert len(requests) - before <= 4
        if 'comparison_id' in reply:
            break
        assert reply['partial'] and not reply.get('exhausted')
    else:
        pytest.fail('Last discovered article was not reached')
    assert requests == sources and loaded == [sources[-1] + '/image.jpg']
    reject(adapter, session, reply)
    assert (await adapter._compare_place_images(session, {}))['partial']


@pytest.mark.asyncio
async def test_pending_comparison_retains_all_later_supplied_urls(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    async def fetch(service, story, batch, excluded, *, receipts):
        url = batch[0]['url']
        receipts.append({'url': url, 'status': 'completed'})
        return [article(url)]
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    images(svc)
    session = sessions()
    first = await adapter._compare_place_images(session, {'article_urls': ['https://example.com/first']})
    late = [f'https://example.com/late-{index}' for index in range(85)]
    replay = await adapter._compare_place_images(session, {'article_urls': late})
    assert replay['comparison_id'] == first['comparison_id']
    _, research = svc._identity_snapshot(topic['id'])
    assert set(late) <= set(research['visual_search_operation']['sources'])
    assert len(research['visual_search_operation']['sources']) == 86
    assert not research['visual_search_operation']['reviewed_reference_ids']


@pytest.mark.asyncio
async def test_completed_recovery_media_survives_catalog_omission_and_partial_cursor_resumes(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    urls = ['https://example.com/completed', 'https://example.com/partial', 'https://example.com/deferred']
    story, _ = svc._identity_snapshot(topic['id'])
    completed = article(urls[0], 0)
    partial = article(urls[1], 1, status='partial', cursor=12)
    identity_discovery._retain_article_discovery(svc, story, [{'url': url} for url in urls], receipts=[
        {'url': urls[0], 'status': 'completed'},
        {'url': urls[1], 'status': 'partial', 'gallery_cursor': 12},
        {'url': urls[2], 'status': 'deferred'}], articles=[completed, partial])
    # The physical candidate catalog contains none of the recovered article rows.
    assert not svc._identity_snapshot(topic['id'])[1]['visual_identity']['candidates']
    loaded = images(svc)
    requested = []
    async def fetch(service, topic, batch, excluded, *, receipts):
        source = batch[0]
        requested.append(dict(source))
        if source['url'] == urls[1]:
            assert source['gallery_cursor'] == 12
            receipts.append({'url': urls[1], 'status': 'completed', 'gallery_cursor': 24})
            value = article(urls[1], 1)
            value['reference_image_urls'] = [urls[1] + '/later.jpg']
            return [value]
        assert source['url'] == urls[2]
        receipts.append({'url': urls[2], 'status': 'completed'})
        return [article(urls[2], 2)]
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    session = sessions()
    for index in range(4):
        reply = await adapter._compare_place_images(session, {})
        assert reply['comparison_id']
        reject(adapter, session, reply, index)
        session = sessions()  # Reconnect uses the same durable backlog/cursor.
    assert loaded[:2] == [urls[0] + '/image.jpg', urls[1] + '/image.jpg']
    assert set(loaded[2:]) == {urls[1] + '/later.jpg', urls[2] + '/image.jpg'}
    assert {source['url'] for source in requested} == set(urls[1:])
    _, research = svc._identity_snapshot(topic['id'])
    assert research['visual_search_operation']['sources'][urls[1]]['source']['gallery_cursor'] == 24


@pytest.mark.asyncio
async def test_pending_partial_and_failed_page_retries_have_no_total_attempt_ceiling(tmp_path, monkeypatch):
    svc, adapter, topic, sessions = prepared(tmp_path)
    urls = ['https://example.com/long-gallery', 'https://example.com/temporarily-failed']
    story, _ = svc._identity_snapshot(topic['id'])
    identity_discovery._retain_article_discovery(svc, story, [{'url': url} for url in urls])
    session = sessions()
    requested = []
    async def fetch(service, topic, batch, excluded, *, receipts):
        source = batch[0]
        requested.append(dict(source))
        receipts.append({'url': source['url'], 'status': 'temporary_failure'})
        return []
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    reply = await adapter._compare_place_images(session, {})
    assert reply['partial']
    # Simulate accumulated bounded passes; old 10/2 ceilings must not freeze URLs.
    for index, url in enumerate(urls):
        page = session.state['visual_comparison']['sources'][url]
        page.update(status='partial' if index == 0 else 'temporary_failure', attempts=40, retry_at=0)
        page['source']['gallery_cursor'] = 480
    requested.clear()
    reply = await adapter._compare_place_images(session, {})
    assert reply['partial'] and not reply.get('exhausted')
    assert [source['url'] for source in requested] == urls
    assert requested[0]['gallery_cursor'] == 480


def test_recovery_source_history_commit_is_fenced_by_stop(tmp_path):
    svc, _, topic, _ = prepared(tmp_path)
    story, _ = svc._identity_snapshot(topic['id'])
    stop_research(svc, story['id'], purpose='identity')
    before = svc._identity_snapshot(topic['id'])[1]
    with pytest.raises(ConflictError):
        identity_discovery._retain_article_discovery(svc, story, [{'url': 'https://example.com/late'}])
    assert svc._identity_snapshot(topic['id'])[1] == before


@pytest.mark.asyncio
async def test_independent_search_route_returns_all_sources(tmp_path):
    svc, _, topic, _ = prepared(tmp_path)
    sources = [{'url': f'https://example.com/article-{i}'} for i in range(90)]
    async def search_articles(query, story):
        return {'sources': sources, 'source_selection': {'status': 'model_selected'}}
    svc.providers.research = SimpleNamespace(search_articles=search_articles)
    assert await identity_discovery.web_image_sources(svc, 'gate', '', story=topic) == sources


@pytest.mark.asyncio
async def test_recovery_reuses_completed_media_and_fetches_deferred_urls_with_gallery_cursor(tmp_path, monkeypatch):
    svc, _, topic, _ = prepared(tmp_path)
    story, _ = svc._identity_snapshot(topic['id'])
    urls = ['https://example.com/completed', 'https://example.com/partial', 'https://example.com/unread']
    completed = article(urls[0], 0)
    identity_discovery._retain_article_discovery(svc, story, [{'url': url} for url in urls], receipts=[
        {'url': urls[0], 'status': 'completed'},
        {'url': urls[1], 'status': 'partial', 'gallery_cursor': 12, 'gallery_slide_cursor': 3}],
        articles=[completed])
    svc.providers.gemini._generate = object()
    svc.providers.gemini.executor = object()
    async def suggest(*args):
        return 'Gate', [], 'pointed arch', ''
    async def search(*args, **kwargs):
        return [{'url': url} for url in urls]
    batches = []
    async def fetch(service, context, sources, excluded, *, receipts):
        batches.append(sources)
        receipts.extend({'url': source['url'], 'status': 'completed'} for source in sources)
        return [article(source['url'], index + 1) for index, source in enumerate(sources)]
    monkeypatch.setattr(identity_discovery, 'suggest', suggest)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    result, discovered = await identity_discovery.recover(svc, story, '', [], set())
    assert result['_article_media_pending']
    assert completed in discovered
    assert not batches  # Ready retained media is delivered without another page barrier.
    pages = svc._identity_snapshot(topic['id'])[1]['identity_article_discovery']['pages']
    assert pages[urls[1]]['source']['gallery_cursor'] == 12
    assert pages[urls[1]]['source']['gallery_slide_cursor'] == 3
    history = svc._identity_snapshot(topic['id'])[1]['identity_article_discovery']
    assert len(history['sources']) == 3
    assert len(history['pages']) == 2  # The unread URL is retained without pretending it was fetched.


@pytest.mark.asyncio
async def test_nonempty_search_without_images_continues_saved_alternative_plan(tmp_path, monkeypatch):
    svc, _, topic, _ = prepared(tmp_path)
    story, _ = svc._identity_snapshot(topic['id'])
    svc.providers.gemini._generate = object()
    svc.providers.gemini.executor = object()
    queries, reads = [], []
    async def suggest(service, context, transcript, candidates):
        context['_identity_article_queries'] = ['address hypothesis A', 'address hypothesis B']
        return '', [], 'visible facade', ''
    async def search(service, name, visual, *, story, first_ready=False):
        query = story['_identity_search_query']
        queries.append(query)
        return [{'url': 'https://example.com/' + ('empty-directory' if query.endswith('A') else 'exterior-article')}]
    async def fetch(service, context, sources, excluded, *, receipts, first_ready=False):
        reads.extend(source['url'] for source in sources)
        receipts.extend({'url': source['url'], 'status': 'completed', 'image_count': int('exterior' in source['url'])}
                        for source in sources)
        return [article(source['url'], 1) for source in sources if 'exterior' in source['url']]
    monkeypatch.setattr(identity_discovery, 'suggest', suggest)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    result, discovered = await identity_discovery.recover(svc, story, '', [], set())
    assert result['_article_media_pending'] and discovered[0]['url'].endswith('exterior-article')
    assert queries == ['address hypothesis A', 'address hypothesis B']
    assert reads == ['https://example.com/empty-directory', 'https://example.com/exterior-article']
    history = svc._identity_snapshot(topic['id'])[1]['identity_article_discovery']
    assert len(history['sources']) == 2 and len(history['pages']) == 2
    assert all(page['status'] == 'completed' for page in history['pages'].values())
    result, retained = await identity_discovery.recover(svc, story, '', [], set())
    assert result['_article_media_pending'] and retained == discovered
    assert len(queries) == 2 and len(reads) == 2  # Closed searches and acquired pages are not repeated.
