import base64
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup

from street_story.article_media import article_candidates, collection_cards, collection_reference, extract_media
from test_identity_lifecycle import make_service
from test_independent_article_priority import gallery
from visual_queue_fixture import reference_receipt


def cards(same_location=False):
    return '<main><h2>Publisher photo inventory</h2><div>' + ''.join(
        f'<div><a href="/original-{i}.jpg" data-fancybox="photos"><img src="/thumb-{i}.jpg"></a>'
        f'<p>Caption {i}</p><a href="/map?lat={54 if same_location else 54+i}&amp;lon=20">Location</a>'
        f'<a href="/object?id={i}">Object detail {i}</a></div>' for i in range(4)) + '</div></main>'


def test_independent_collection_cards_are_links_not_a_physical_object_ref_gallery():
    html = cards()
    title, media = extract_media(html, 'https://photos.example/collection')
    assert title == '' and media == []
    found, links = collection_cards(BeautifulSoup(html, 'html.parser'), 'https://photos.example/collection')
    assert len(found) == 4
    assert [v['url'] for v in links] == [f'https://photos.example/object?id={i}' for i in range(4)]
    assert all(v['collection_url'] == 'https://photos.example/collection' for v in links)


def test_object_gallery_same_explicit_place_keeps_all_views():
    _title, media = extract_media(cards(same_location=True), 'https://photos.example/object?id=4')
    assert len(media) == 4


def test_explicit_article_body_keeps_own_gallery_even_with_individual_capture_locations():
    html = '<article><h1>One object</h1>' + cards() + '</article>'
    title, media = extract_media(html, 'https://photos.example/object')
    assert title == 'One object' and len(media) == 4


def test_unlinked_gallery_captions_never_require_collection_classifier():
    html = '<main><h1>Object</h1><div>' + ''.join(
        f'<figure><img src="/view-{i}.jpg"><figcaption>View {i}</figcaption></figure>' for i in range(6)) + '</div></main>'
    assert len(extract_media(html, 'https://photos.example/object')[1]) == 6


@pytest.mark.asyncio
async def test_collection_completes_with_safe_detail_links_without_browser_or_ref_candidate(tmp_path):
    svc, _ = make_service(tmp_path)
    story = svc.create_story(key='collection', client_story_id='collection', photo_sha256='opaque-upload',
        photo_mime_type='image/jpeg', photo_bytes=b'source', voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    async def resolver(host):
        return '93.184.216.34'
    async def browser(_url):
        pytest.fail('A proved collection must not expand another browser gallery')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            headers={'content-type': 'text/html'}, content=cards()))) as client:
        receipts = []
        result = await article_candidates(svc, story, [{'url': 'https://photos.example/collection'}], set(),
            http=client, resolver=resolver, browser=browser, receipts=receipts)
    assert result == []
    assert receipts[0]['status'] == 'completed' and receipts[0]['image_count'] == 0
    assert receipts[0]['collection_boundary'] == 'independent_item_locations'
    assert len(receipts[0]['detail_sources']) == 4
    assert collection_reference({'url': 'https://photos.example/collection'}, svc.store)
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM live_diagnostics WHERE story_id=? AND event_type='identity_article_collection'", (story['id'],)).fetchone()[0] == 1


def test_existing_cached_collection_guard_does_not_ban_host_or_named_article(tmp_path):
    def cache(key):
        if 'collection' in key:
            raise AssertionError('Acquisition key remains opaque existing text key')
        return {'body': base64.b64encode(cards().encode()).decode(), 'final_url': 'https://photos.example/collection'}
    store = SimpleNamespace(cache_get=cache)
    assert collection_reference({'url': 'https://photos.example/collection'}, store)
    store.cache_get = lambda key: {'body': base64.b64encode(b'<article><h1>Old history, current facade</h1><img src="/current.jpg"></article>').decode()}
    assert not collection_reference({'url': 'https://photos.example/object'}, store)
    assert not collection_reference({'url': 'https://photos.example/object'})


@pytest.mark.asyncio
async def test_saved_collection_frames_are_excluded_before_new_send_but_pending_operation_is_preserved(tmp_path, monkeypatch):
    svc, adapter, story, session = gallery(tmp_path, 0)
    bad = {'candidate_id': 'web:archive', 'name': 'Archive', 'url': 'https://photos.example/collection',
        'reference_image_urls': ['https://photos.example/old.jpg'], 'discovery': 'web_article_media'}
    state = session.state['visual_comparison']
    ready_physical = state['queue']
    state['queue'] = [next(adapter._image_entries(bad))]
    sent = []
    async def images(batch, limit, *, story_id, evidence):
        candidate = batch[0]
        sent.append(candidate['candidate_id'])
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'], 'image/jpeg', candidate['reference_image_urls'][0])]
    svc._candidate_reference_images = images
    async def no_acquisition(*args, **kwargs):
        return []
    monkeypatch.setattr('street_story.article_media.article_candidates', no_acquisition)
    first = await adapter._compare_place_images(session, {})
    assert sent == ['web:archive']
    monkeypatch.setattr('street_story.article_media.collection_reference',
        lambda candidate, store: candidate.get('url') == bad['url'])
    sent.clear()
    repeated = await adapter._compare_place_images(session, {})
    assert repeated['comparison_id'] == first['comparison_id']
    assert sent == []  # Observe the addressed operation, never replace its unknown outcome.
    state['pending'] = None  # The product receipt has closed in this second half.
    state['queue'] = [next(adapter._image_entries(bad)), *ready_physical]
    reply = await adapter._compare_place_images(session, {})
    assert reply['references'][0]['candidate_id'] == 'wiki:0'
    assert sent == ['wiki:0']
