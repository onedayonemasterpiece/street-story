import json
from types import SimpleNamespace

import httpx
import pytest

from street_story.article_media import extract_media, fetch_public, public_url, load_article_reference
from street_story.identity_progress import advance
from street_story.live import StreetStoryLiveAdapter
from street_story.service import ConflictError
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


def test_article_gallery_keeps_later_views_but_excludes_other_objects_and_ads():
    title, images = extract_media('''<h1>Ворота</h1><main><article>
      <a href="/front.jpg"><img src="/small-front.jpg"></a>
      <figure><img data-src="/rear.jpg"></figure>
      <img srcset="/small.jpg 300w, /detail.jpg 1200w">
      <aside><img src="/other-building.jpg"></aside>
      <div class="poster-item__container"><img src="/another-gate.jpg"></div>
      <a href="/other-place/"><img src="/unrelated.jpg"></a>
      <div class="ad-banner"><img src="/sale.jpg"></div>
      <footer><img src="/logo.jpg"></footer>
    </article></main>''', 'https://example.com/gate/')
    assert title == 'Ворота'
    assert [i['image_url'] for i in images] == [
        'https://example.com/front.jpg', 'https://example.com/rear.jpg', 'https://example.com/detail.jpg']
    assert all(i['article_url'] == 'https://example.com/gate/' for i in images)


def test_hero_background_and_empty_structured_image():
    _, images = extract_media('''<section class="hero--detail" style="background-image:url('/gate.jpg')"></section>
      <script type="application/ld+json">{"@type":"Article"}</script>
      <a class="related" href="/other/" style="background-image:url('/other.jpg')"></a>''', 'https://example.com/article/')
    assert [i['image_url'] for i in images] == ['https://example.com/gate.jpg']


def test_wikipedia_inline_gallery_file_links_are_kept_but_other_articles_are_excluded():
    _title, media = extract_media('''<div class="mw-parser-output">
      <figure><a href="/wiki/File:Front.jpg"><img src="https://upload.wikimedia.org/front.jpg"></a></figure>
      <figure><a href="/wiki/File:Rear.jpg"><img src="https://upload.wikimedia.org/rear.jpg"></a></figure>
      <a href="/wiki/Other_building"><img src="https://upload.wikimedia.org/other.jpg"></a>
    </div>''', 'https://ru.wikipedia.org/wiki/Gate')
    assert [item['image_url'] for item in media] == ['https://upload.wikimedia.org/front.jpg', 'https://upload.wikimedia.org/rear.jpg']


@pytest.mark.parametrize('url', ['http://example.com/i.jpg', 'https://127.0.0.1/i.jpg',
    'https://169.254.169.254/i.jpg', 'https://[::1]/i.jpg', 'https://localhost/i.jpg',
    'https://example.com:8188/i.jpg', 'https://user:secret@example.com/i.jpg'])
def test_nonpublic_image_url_rejected(url):
    assert public_url(url) is None


@pytest.mark.asyncio
async def test_redirect_and_dns_are_validated_before_network_access():
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://127.0.0.1/private'})
    async def resolver(host):
        return '93.184.216.34'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='unsafe_url'):
            await fetch_public(client, 'https://example.com/image', 1000, resolver=resolver)
        assert len(calls) == 1 and calls[0].url.host == '93.184.216.34'
        assert calls[0].headers['host'] == 'example.com'
        async def private(host):
            return '10.0.0.1'
        with pytest.raises(ValueError, match='private_address'):
            await fetch_public(client, 'https://example.com/image', 1000, resolver=private)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_reference_must_have_article_provenance_and_be_decodable():
    url = 'https://example.com/gate.jpg'
    candidate = {'article_media': [{'image_url': url, 'article_url': 'https://example.com/gate', 'kind': 'article_img'}]}
    async def resolver(host):
        return '93.184.216.34'
    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=jpeg((640, 480)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        image, receipt = await load_article_reference(client, candidate, url, resolver=resolver)
        assert image[0] == 'image/jpeg' and receipt['article_url'] == 'https://example.com/gate'
        with pytest.raises(ValueError, match='not_extracted'):
            await load_article_reference(client, candidate, 'https://example.com/ad.jpg', resolver=resolver)


def test_count_increases_after_comparison_including_nonmatches_and_survives_readback():
    state = advance({}, 'identity_reference_loaded', {}, 1)
    assert state.get('images_reviewed_count', 0) == 0
    state = advance(state, 'identity_images_reviewed', {'reference_ids': ['a', 'b']}, 2)
    assert state['images_reviewed_count'] == 2
    state = advance(state, 'identity_images_reviewed', {'reference_ids': ['b', 'c']}, 3)
    assert state['images_reviewed_count'] == 3
    state = advance(state, 'identity_finished', {'status': 'uncertain'}, 4)
    assert not state['visual_comparison_verified']
    state = advance(state, 'identity_live_comparison_sent', {}, 5)
    assert not state['finished'] and state['images_reviewed_count'] == 3
    state = advance(state, 'identity_finished', {'status': 'match', 'reference_verified': True}, 6)
    assert state['visual_comparison_verified'] and state['images_reviewed_count'] == 3


@pytest.mark.asyncio
async def test_live_verdict_is_bound_to_sent_references_upload_and_generation(tmp_path, monkeypatch):
    svc, _ = make_service(tmp_path)
    photo = jpeg()
    story = svc.create_story(key='media', client_story_id='media', photo_sha256='opaque-upload-token',
        photo_mime_type='image/jpeg', photo_bytes=photo, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps({'visual_identity': {'status': 'uncertain',
            'candidates': [{'candidate_id': 'wiki:1', 'name': 'Gate', 'url': 'https://example.com/gate',
                            'reference_image_urls': ['https://upload.wikimedia.org/1.jpg']} ]}}), story['id']))
    sent = []
    adapter = StreetStoryLiveAdapter(svc, lambda *a: None, lambda s, data: sent.append(data))
    session = SimpleNamespace(id='live_1234567890abcdef', resource_id=story['id'], model='gemini-3.8-live', state={})
    async def images(candidates, limit, *, story_id, evidence):
        evidence.append({'candidate_id': 'wiki:1', 'source_url': 'https://upload.wikimedia.org/1.jpg',
            'article_url': 'https://example.com/gate'})
        return [('wiki:1', 'image/jpeg', 'https://upload.wikimedia.org/1.jpg')]
    svc._candidate_reference_images = images
    from street_story import article_media
    async def fetch(*args, **kwargs):
        return 'https://upload.wikimedia.org/1.jpg', 'image/jpeg', photo
    monkeypatch.setattr(article_media, 'fetch_public', fetch)
    reply = await adapter.execute_tool(session, {'name': 'compare_place_images', 'id': 'image-call', 'args': {}})
    from live_interaction.tool_parts import function_response
    response = function_response('compare_place_images', 'image-call', reply)
    assert response['parts'][0]['inlineData']['data']
    assert not sent and reply['references'][0]['label'] == 'REF 1'
    assert svc.story(story['id'])['identity_progress'].get('images_reviewed_count', 0) == 0
    args = {'comparison_id': reply['comparison_id'], 'status': 'match', 'candidate_id': 'evil',
        'confidence': .99, 'observations': ['Distinct details match'], 'alternative_candidate_ids': []}
    with pytest.raises(ConflictError, match='показанные'):
        adapter._record_place_comparison(session, 'bad', args)
    args['candidate_id'] = 'wiki:1'
    with svc.store.tx() as db:
        original = db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0]
        changed = json.loads(original)
        changed['identity_generation'] = 1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(changed), story['id']))
    with pytest.raises(ConflictError, match='изменилось'):
        adapter._record_place_comparison(session, 'stale', args)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (original, story['id']))
    result = adapter._record_place_comparison(session, 'good', args)
    assert result['matched'] and result['story']['visual_identity']['comparison_model'] == 'gemini-3.8-live'
    assert result['story']['identity_progress']['images_reviewed_count'] == 1
    assert result['story']['identity_progress']['visual_comparison_verified']
    with svc.store.connection() as db:
        research = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
        assert research['poi_id'] and research['poi_reused_fact_count'] == 0


@pytest.mark.asyncio
async def test_blocked_direct_image_never_uses_browser_screenshot_fallback(monkeypatch):
    from street_story import article_media
    descriptor = {'image_url':'https://example.com/gate.jpg','article_url':'https://example.com/gate','kind':'article_img'}
    candidate = {'article_media':[descriptor], '_browser_budget':{'remaining':1}}
    async def forbidden(*args):
        raise AssertionError('No browser image fallback')
    monkeypatch.setattr(article_media,'browser_reference',forbidden)
    async def resolver(_host):
        return '93.184.216.34'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(403))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await load_article_reference(client,candidate,descriptor['image_url'],resolver=resolver)
    assert candidate['_browser_budget']['remaining'] == 1


@pytest.mark.asyncio
async def test_wikipedia_mismatch_automatically_searches_and_advances_to_later_article_images(tmp_path, monkeypatch):
    from street_story import article_media, identity_discovery
    svc, _ = make_service(tmp_path)
    photo = jpeg((640, 480))
    story = svc.create_story(key='fallback', client_story_id='fallback', photo_sha256='opaque-upload-token',
        photo_mime_type='image/jpeg', photo_bytes=photo, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    wiki = {'candidate_id': 'wiki:1', 'name': 'Gate', 'url': 'https://example.com/wiki',
            'reference_image_urls': ['https://upload.wikimedia.org/front.jpg']}
    article = {'candidate_id': 'web:gate', 'name': 'Gate gallery', 'url': 'https://example.com/gate',
               'discovery': 'web_article_media', 'reference_image_urls': [
                   'https://example.com/front.jpg', 'https://example.com/rear.jpg']}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (json.dumps({'visual_identity': {'status': 'uncertain', 'candidates': [wiki]}}), story['id']))
    loaded, searched = [], []
    async def images(candidates, limit, *, story_id, evidence):
        candidate = candidates[0]
        url = candidate['reference_image_urls'][0]
        loaded.append(url)
        evidence.append({'candidate_id': candidate['candidate_id'], 'source_url': url,
                         **({'article_url': candidate['url']} if candidate['candidate_id'].startswith('web:') else {})})
        return [(candidate['candidate_id'], 'image/jpeg', url)]
    async def search(service, query, visual_query, **kwargs):
        searched.append(query)
        return [{'url': article['url']}]
    async def articles(*_args, receipts):
        receipts.append({'status': 'completed'})
        return [article]
    svc._candidate_reference_images = images
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    events = []
    adapter = StreetStoryLiveAdapter(svc, lambda _session, event: events.append(event), lambda *a: None)
    session = SimpleNamespace(id='headless:article-fallback', resource_id=story['id'], model='gemini-3.8-live', state={})
    for index, status in enumerate(('mismatch', 'mismatch', 'match'), 1):
        reply = await adapter.execute_tool(session, {'name': 'compare_place_images', 'id': f'load-{index}', 'args': {'query': 'Gate'}})
        assert len(searched) == (0 if index == 1 else 1)
        await adapter.execute_tool(session, {'name': 'record_place_comparison', 'id': f'verdict-{index}', 'args': {
            'comparison_id': reply['comparison_id'], 'candidate_id': reply['references'][0]['candidate_id'],
            'reference_subject_candidate_id': wiki['candidate_id'],
            'status': status, 'confidence': .99, 'observations': ['Visible detail comparison'], 'alternative_candidate_ids': []}})
        current = svc.story(story['id'])
        assert current['identity_progress']['images_reviewed_count'] == index
        assert current['identity_progress']['visual_comparison_verified'] == (status == 'match')
        pushed = [event for event in events if event['type'] == 'product_state'][-1]['state']['identity_progress']
        assert pushed['images_reviewed_count'] == index
        assert pushed['visual_comparison_verified'] == (status == 'match')
        assert 'reviewed_image_sha256s' not in pushed
    assert loaded == [wiki['reference_image_urls'][0], *article['reference_image_urls']]
    assert current['visual_identity']['candidate_id'] == wiki['candidate_id']
    assert current['visual_identity']['candidate_name'] == wiki['name']
    assert current['visual_identity']['reference_subject_binding']['reference_candidate_id'] == article['candidate_id']
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM poi_aliases WHERE namespace='street_story_candidate' AND value=?",
                              (article['candidate_id'],)).fetchone()
