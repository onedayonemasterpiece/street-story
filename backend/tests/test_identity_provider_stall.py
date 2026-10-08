"""Owner stall regressions: transport, independent routes and visible waits."""
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.errors import RetryableProviderError
from street_story.headless_identity import HeadlessIdentity
from street_story.live_visual_comparison import LiveVisualComparisonMixin
from street_story.providers import GeminiClient, GeminiUnavailable
from street_story.service import ConflictError, canonical
from test_article_source_retention import images
from test_identity_lifecycle import create, make_service
from test_visual_search_continuation import prepared


@pytest.mark.asyncio
async def test_model_search_failures_use_existing_public_discovery(tmp_path):
    svc, _adapter, topic, _session = prepared(tmp_path)
    calls = []

    async def opencode(*args):
        calls.append('opencode')
        raise RetryableProviderError('RESOURCE_DAILY_BUDGET', retry_at=10000)

    async def google(*args, **kwargs):
        calls.append('google')
        raise RetryableProviderError('google_quota', retry_at=5000)

    sources = [{'url': 'https://news.example/building', 'title': 'Historical building'}]

    async def public(*args):
        calls.append('public')
        return SimpleNamespace(grounding_sources=sources)

    async def select(query, observed, story):
        assert observed == sources
        calls.append('semantic_selection')
        return {'sources': sources, 'source_selection': {'status': 'model_selected'}}

    svc.providers.research = SimpleNamespace(search_articles=opencode, select_identity_sources=select)
    svc.providers.gemini.discover_article_urls = google
    svc.providers.gemini._public_web_search = public
    assert await identity_discovery.web_image_sources(svc, 'building', '', story=topic) == sources
    assert calls == ['opencode', 'google', 'public', 'semantic_selection']
    assert svc.story(topic['id'])['place_name'] is None  # snippets are not proof


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown', [False, True])
async def test_legacy_unsupported_pending_advances_only_after_known_unsent_outcome(tmp_path, unknown):
    svc, adapter, topic, session = prepared(tmp_path)
    candidate = {'candidate_id': 'gate', 'name': 'Gate', 'url': 'https://example.com/article',
                 'reference_image_urls': ['https://example.com/first.jpg', 'https://example.com/second.jpg']}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': [candidate]}}), topic['id']))
    loaded = images(svc)
    s = session()
    first = await adapter._compare_place_images(s, {})
    state = s.state['visual_comparison']
    state['pending']['candidates'][0]['reference_image_urls'] = ['https://example.com/flag.svg']
    adapter._save_visual_queue(s, state)
    if unknown:
        with svc.store.tx() as db:
            receipt = {'phase': 'unknown', 'provider_send_state': 'possibly_sent',
                       'comparison_id': first['comparison_id'], 'binding': {'visual_scope': True, 'generation': 0}}
            db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
                       ('unknown', 'unit', topic['id'], 'vision_native', canonical(receipt), svc.store.now(), svc.store.now()))
    s.state = {}
    loaded.clear()
    next_unit = await adapter._compare_place_images(s, {})
    if unknown:
        assert next_unit['comparison_id'] == first['comparison_id']
        assert not loaded and not s.state['visual_comparison'].get('skipped_reference_ids')
    else:
        assert next_unit['comparison_id'] != first['comparison_id']
        assert loaded == ['https://example.com/second.jpg']
        assert s.state['visual_comparison']['skipped_reference_ids']
    assert not svc.story(topic['id']).get('identity_progress', {}).get('images_reviewed_count')


@pytest.mark.asyncio
async def test_failed_live_image_delivery_cannot_record_even_an_uncertain_verdict(tmp_path, monkeypatch):
    svc, adapter, topic, session = prepared(tmp_path)
    candidate = {'candidate_id': 'gate', 'name': 'Gate', 'url': 'https://example.com/article',
                 'reference_image_urls': ['https://example.com/photo.jpg']}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical({
            'visual_identity': {'status': 'uncertain', 'candidates': [candidate]}}), topic['id']))
    images(svc)
    s = session()
    first = await adapter._compare_place_images(s, {})
    pending = s.state['visual_comparison']['pending']

    async def unavailable(*args):
        raise ValueError('reference_format')

    monkeypatch.setattr('street_story.article_media.fetch_public', unavailable)
    reply = await LiveVisualComparisonMixin._comparison_result(adapter, pending)
    assert reply['image_delivery_status'] == 'preparation_failed'
    with pytest.raises(ConflictError, match='Изображения не переданы'):
        adapter._record_place_comparison(s, 'not-a-verdict', {
            'comparison_id': first['comparison_id'], 'status': 'uncertain',
            'confidence': 0, 'observations': ['Images unavailable']})
    assert not svc.story(topic['id']).get('identity_progress', {}).get('images_reviewed_count')


@pytest.mark.asyncio
async def test_provider_hour_wait_does_not_delay_other_work_and_is_visible(tmp_path, monkeypatch):
    svc, _gemini = make_service(tmp_path)
    svc.store.now = lambda: 1000
    topic = create(svc)

    async def waiting(*args):
        raise RetryableProviderError('provider_quota', retry_at=4600)

    monkeypatch.setattr(identity_discovery, 'recover', waiting)
    # Defer normal comparison so discovery is required.
    async def deferred(*args):
        return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                'observations': [], '_references_sent': [], '_comparison_deferred': True}
    svc._identify_photo = deferred
    svc.ensure_identity(topic['id'])
    assert await svc.run_once()
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity'", (topic['id'],)).fetchone())
    assert job['state'] == 'retry' and job['available_at'] == 1060
    processing = svc.story(topic['id'])['processing']
    assert processing['status'] == 'processing_delayed'
    assert processing['retry_in_seconds'] == 60
    assert not await svc.run_once()  # bounded wait, no hot loop
    with svc.store.tx() as db:
        db.execute('UPDATE jobs SET available_at=4600 WHERE id=?', (job['id'],))
    svc.recover_jobs()
    with svc.store.connection() as db:
        assert db.execute('SELECT available_at FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 1060


def test_explicit_vector_urls_never_become_vision_attachments():
    candidate = {'candidate_id': 'candidate', 'reference_image_urls': [
        'https://example.com/a.svg', 'https://example.com/photo?id=1', 'https://example.com/a.jpg']}
    assert [entry['reference_image_urls'][0] for entry in HeadlessIdentity._image_entries(candidate)] == [
        'https://example.com/photo?id=1', 'https://example.com/a.jpg']


@pytest.mark.asyncio
async def test_google_search_can_use_configured_lite_when_other_model_quota_fails(tmp_path):
    import json
    svc, _ = make_service(tmp_path)
    client = GeminiClient(svc.settings, svc.store)
    attempted = []

    class Executor:
        def __init__(self, model):
            self.model = model

        async def execute(self, operation, call):
            attempted.append(self.model)
            if self.model != svc.settings.gemini_model:
                raise GeminiUnavailable(4600, 'quota_exhausted')
            return await call('fixture', 5)

    client.web_search_routes = [(model, pool, quota, Executor(model))
                                for model, pool, quota, _executor in client.web_search_routes]

    async def generate(*args, **kwargs):
        assert kwargs['model'] == svc.settings.gemini_model
        return SimpleNamespace(text=json.dumps({'summary': 'Concrete building source', 'selected_sources': [
            {'url': 'https://news.example/building', 'reason': 'Useful material about the building'}]}),
            candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(
                uri='https://news.example/building', title='Building'))]))])

    client._generate = generate
    found = await client.discover_article_urls('nearby historical building')
    assert attempted == [svc.settings.gemini_web_search_model, svc.settings.gemini_model]
    assert found.grounding_sources[0]['model'] == svc.settings.gemini_model


@pytest.mark.asyncio
async def test_search_terms_receive_nearby_address_distance_and_camera_context(tmp_path):
    import json
    svc, _adapter, topic, _session = prepared(tmp_path)
    query = 'Примерная улица историческое здание prussia39'
    topic.update(_identity_search_context={'reverse_address': {'road': 'Примерная улица'},
        'nearby': [{'distance_m': 18, 'tags': {'name': 'Примерная улица'}}]},
        _camera_hints={'focal_length_35mm': 24})

    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)

    async def generate(key, timeout, contents, config, **kwargs):
        context = json.loads(contents[1].split('Данные ниже — только контекст:\n')[1])
        assert context['location_search_context']['nearby'][0]['distance_m'] == 18
        assert context['camera_hints']['focal_length_35mm'] == 24
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': 'red brick building', 'commons_query': '', 'article_queries': [query]}))

    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    assert await identity_discovery.suggest(svc, topic, '', []) == ('', [], 'red brick building', '')
    assert topic['_identity_article_queries'] == [query, 'red brick building']
    queries = []

    async def public(q):
        queries.append(q)
        return SimpleNamespace(grounding_sources=[{'url': 'https://news.example/building'}])

    async def empty_search(*args):
        return {'sources': [], 'source_selection': {'status': 'model_selected'}}

    async def select(q, observed, story):
        assert q == query
        return {'sources': observed, 'source_selection': {'status': 'model_selected'}}

    svc.providers.research = SimpleNamespace(select_identity_sources=select, search_articles=empty_search)
    svc.providers.gemini._public_web_search = public
    await identity_discovery.web_image_sources(svc, 'wrong distant guess', 'building',
        story={**topic, '_identity_search_query': topic['_identity_article_queries'][0]})
    assert queries == [query]
