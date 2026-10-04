import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story import article_media, identity_discovery
from street_story.db import Store
from street_story.errors import RetryableProviderError
from street_story.live import StreetStoryLiveAdapter
from street_story.providers import GeminiClient
from test_backend import config
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


@pytest.mark.asyncio
async def test_url_discovery_uses_only_grounding_and_no_fact_semantics(tmp_path):
    settings = replace(config(tmp_path), gemini_api_key=SecretStr('key-a'),
                       gemini_api_keys=(SecretStr('key-a'),))
    client = GeminiClient(settings, Store(tmp_path / 'db'))
    class Executor:
        async def execute(self, operation, call):
            assert operation == 'web_search'
            return await call('key-a', 5)
    client.web_search_routes = [('configured-search-model', None, object(), Executor())]
    calls = []
    async def generate(key, timeout, contents, configuration, **kwargs):
        calls.append(kwargs)
        assert configuration.tools[0].google_search is not None
        assert '32' not in contents[0] and 'known_facts' not in contents[0]
        return SimpleNamespace(text='https://invented.invalid/ignored', candidates=[
            SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(uri='https://example.com/article', title='Gate')),
                SimpleNamespace(web=SimpleNamespace(uri='http://unsafe.invalid', title='Bad'))]))])
    async def forbidden(*args, **kwargs):
        pytest.fail('HTML SERP/fact binding is forbidden in URL discovery')
    client._generate = generate
    client._public_web_search = client._bind_facts_to_evidence = forbidden
    result = await client.discover_article_urls('gate gallery')
    assert [s['url'] for s in result.grounding_sources] == ['https://example.com/article']
    assert result.grounding_sources[0]['grounding_url'] == 'https://example.com/article'
    assert result.grounding_sources[0]['model'] == 'configured-search-model'
    assert calls[0]['operation'] == 'article_url_discovery'


def prepared(tmp_path):
    svc, _ = make_service(tmp_path)
    photo = jpeg()
    story = svc.create_story(key='queue', client_story_id='queue',
        photo_sha256=hashlib.sha256(photo).hexdigest(), photo_mime_type='image/jpeg',
        photo_bytes=photo, voice_protocol='voice-chunks-v2', lat=54.7, lon=20.5)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (json.dumps({'visual_identity': {'status': 'uncertain', 'candidates': []}}), story['id']))
    adapter = StreetStoryLiveAdapter(svc, lambda *args: None, lambda *args: None)
    def session():
        return SimpleNamespace(id='live_test', resource_id=story['id'], model='gemini-3.8-live', state={})
    return svc, adapter, story, session


@pytest.mark.asyncio
async def test_search_failure_survives_restart_then_retries_without_serp(tmp_path, monkeypatch):
    svc, adapter, story, session = prepared(tmp_path)
    calls = []
    async def search(service, query, visual_query):
        calls.append(query)
        if len(calls) == 1:
            raise RetryableProviderError('temporary')
        return [{'url': 'https://example.com/article'}]
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    assert (await adapter._find_place_articles(session(), {'query': 'gate'}))['status'] == 'temporary_failure'
    assert (await adapter._find_place_articles(session(), {'query': 'gate'}))['status'] == 'temporary_failure'
    assert len(calls) == 1
    now = svc.store.now()
    svc.store.now = lambda: now + 20
    assert (await adapter._find_place_articles(session(), {'query': 'gate'}))['status'] == 'completed'
    assert len(calls) == 2
    _, research = svc._identity_snapshot(story['id'])
    assert research['identity_article_discovery']['sources'][0]['url'] == 'https://example.com/article'


@pytest.mark.asyncio
async def test_late_urls_partial_and_completed_verdict_resume(tmp_path, monkeypatch):
    svc, adapter, story, session = prepared(tmp_path)
    loaded = []
    async def articles(service, topic, sources, excluded, *, receipts):
        url = sources[0]['url']
        receipts.append({'status': 'partial' if url.endswith('/first') else 'completed', 'gallery_cursor': 12})
        return [{'candidate_id': url, 'name': 'Gate', 'url': url, 'discovery': 'web_article_media',
                 'reference_image_urls': [url + '/lead.jpg', url + '/late.jpg']}]
    async def images(candidates, limit, *, story_id, evidence):
        candidate = candidates[0]
        url = candidate['reference_image_urls'][0]
        loaded.append(url)
        data = jpeg((300 + len(url), 401))
        evidence.append({'candidate_id': candidate['candidate_id'], 'model_image_sha256': hashlib.sha256(url.encode()).hexdigest()})
        return [(candidate['candidate_id'], 'image/jpeg', data)]
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    svc._candidate_reference_images = images
    first = session()
    reply = await adapter._compare_place_images(first, {'query': 'gate', 'article_urls': ['https://example.com/first']})
    args = {'comparison_id': reply['comparison_id'], 'status': 'mismatch', 'candidate_id': '',
            'confidence': 1, 'observations': ['Different facade'], 'alternative_candidate_ids': []}
    adapter._record_place_comparison(first, 'first-verdict', args)
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1
    resumed = session()
    reply = await adapter._compare_place_images(resumed, {'query': 'gate', 'article_urls': ['https://example.com/second']})
    assert loaded == ['https://example.com/first/lead.jpg', 'https://example.com/first/late.jpg']
    _, research = svc._identity_snapshot(story['id'])
    operation = research['visual_search_operation']
    assert 'https://example.com/second' in operation['sources']
    assert operation['sources']['https://example.com/first']['status'] == 'partial'
    # Replaying the pending image doesn't advance the completed-verdict count.
    again = await adapter._compare_place_images(resumed, {})
    assert again['comparison_id'] == reply['comparison_id']
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1


@pytest.mark.asyncio
async def test_static_lead_still_renders_lazy_gallery_and_keeps_partial_cursor(tmp_path):
    svc, _ = make_service(tmp_path)
    async def resolver(host):
        return '93.184.216.34'
    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            text='<article><img src="/lead.jpg"><div data-gallery="lazy"></div></article>')
    calls = []
    async def browser(url):
        calls.append(url)
        return 'Gate', article_media.RenderedMedia([
            {'image_url': 'https://example.com/late.jpg', 'article_url': url, 'kind': 'article_img'}], 12, True)
    receipts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        candidates = await article_media.article_candidates(svc, {'id': 'unknown'}, [
            {'url': 'https://example.com/article'}], set(), http=http, resolver=resolver,
            browser=browser, receipts=receipts)
    assert calls == ['https://example.com/article']
    assert candidates[0]['reference_image_urls'] == ['https://example.com/lead.jpg', 'https://example.com/late.jpg']
    assert receipts[0]['status'] == 'partial' and receipts[0]['gallery_cursor'] == 12


def test_capability_bundles_preserve_continuation_and_bound_setup(tmp_path):
    svc, adapter, story, session = prepared(tmp_path)
    s = session()
    s.actor = None
    for stage in adapter.CAPABILITY_TOOLS:
        spec = adapter.resolve_capability(s, {'name': 'continue_story', 'args': {'stage': stage, 'intent': 'Continue'}})
        assert spec['capability'] == 'identity'
        assert 'compare_place_images' in {f['name'] for f in spec['configuration']['functions']}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps({
            'visual_identity': {'status': 'match', 'candidate_name': 'Gate'}}), story['id']))
    for stage in adapter.CAPABILITY_TOOLS:
        spec = adapter.resolve_capability(s, {'name': 'continue_story', 'args': {'stage': stage, 'intent': 'Continue'}})
        assert spec['capability'] == stage
        assert len(spec['configuration']['functions']) <= 9
        assert 'continue_story' in {f['name'] for f in spec['configuration']['functions']}


def test_identity_setup_fits_actual_managed_sdk_budget(tmp_path):
    estimator = pytest.importorskip('ai_resource_control.client', reason='Private managed SDK is installed by server deployment; verified in retained runtime acceptance')
    from live_interaction.provider import setup_config
    svc, adapter, story, session = prepared(tmp_path)
    initialized = adapter.initialize(resource_id=story['id'], actor=None, model='gemini-3.8-live')
    setup = setup_config('gemini-3.8-live', initialized['context'],
                         configuration=initialized['configuration'], search=False)
    assert estimator.estimate_input_tokens(setup) + 8255 + 3 * 9016 + 3000 < 60000

@pytest.mark.asyncio
async def test_gallery_next_controls_enumerate_later_frames_with_a_cursor():
    class Page:
        url = 'https://example.com/gallery'
        def __init__(self, total):
            self.index, self.total = 1, total
            self.first = self
        async def goto(self, *args, **kwargs):
            self.index = 1
        def locator(self, selector):
            return self
        async def count(self):
            return int(self.index < self.total)
        async def is_visible(self):
            return True
        async def click(self, **kwargs):
            self.index += 1
        async def wait_for_timeout(self, milliseconds):
            pass
        async def evaluate(self, script, *args):
            return True if script.startswith('window.scrollY') else None
        async def content(self):
            return f'<article><img src="/slide-{self.index}.jpg"></article>'
    _, complete = await article_media.rendered_media(Page(3), 'https://example.com/gallery')
    assert [m['image_url'] for m in complete] == [f'https://example.com/slide-{i}.jpg' for i in (1, 2, 3)]
    assert not complete.partial
    _, partial = await article_media.rendered_media(Page(18), 'https://example.com/gallery')
    assert partial.partial and partial.slide_cursor == 12
    _, resumed = await article_media.rendered_media(Page(18), 'https://example.com/gallery',
        partial.cursor, partial.slide_cursor)
    assert resumed[-1]['image_url'].endswith('slide-18.jpg') and not resumed.partial


def test_identity_continuation_does_not_spin_or_discard_queued_refs(tmp_path):
    svc, adapter, story, session = prepared(tmp_path)
    s = session()
    s.state['visual_comparison'] = {'queue': [{'reference_image_urls': ['https://example.com/late.jpg']}],
        'pending': None, 'seen_images': ['done'], 'sources': {}}
    writes = []
    adapter.write = lambda session, data: writes.append(data)
    adapter._continue_identity(s)
    adapter._continue_identity(s)
    assert len(writes) == 1 and 'compare_place_images' in writes[0]['text']
    with svc.store.tx() as db:
        current = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
        current['identity_article_discovery'] = {'queries': {'gate': {'status': 'temporary_failure', 'retry_at': 123}}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(current), story['id']))
    adapter._continue_identity(s)
    adapter._continue_identity(s)
    assert len(writes) == 2  # A failed search must not consume the prior queue continuation.
    s.state['visual_comparison']['queue'] = []
    adapter._continue_identity(s)
    assert len(writes) == 2


def test_failed_discovery_still_starts_saved_refs_before_first_comparison(tmp_path):
    svc, adapter, story, session = prepared(tmp_path)
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps({
            'visual_identity': {'status': 'uncertain', 'candidates': [{
                'candidate_id': 'saved', 'reference_image_urls': ['https://example.com/late.jpg']}]},
            'identity_article_discovery': {'status': 'temporary_failure'}}), story['id']))
    writes = []
    adapter.write = lambda session, data: writes.append(data)
    s = session()
    adapter._continue_identity(s)
    adapter._continue_identity(s)
    assert len(writes) == 1 and 'compare_place_images' in writes[0]['text']
