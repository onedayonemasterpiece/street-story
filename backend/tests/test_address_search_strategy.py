from test_reference_image_codec import jpeg
from street_story.reference_image_codec import normalize_reference
import copy
import json
from types import SimpleNamespace

import pytest

from street_story import article_media, identity_discovery


def anchor(object_id, number, distance):
    return {'candidate_id': f'osm:node:{object_id}', 'distance_m': distance,
            'map_address': {'street': 'Fixture Street', 'house_number': number,
                'scope': 'mapped_entry_only', 'provenance': 'osm.tags'},
            'map_coordinates': {'latitude': 54.7, 'longitude': 20.5, 'provenance': 'osm.position'}}


def test_full_supplied_address_nodes_survive_physical_shortlist_projection():
    a, b = anchor(1, '31', 24), anchor(2, '33', 39)
    story = {'_identity_search_context': {'nearby': [a, b]}}
    original = copy.deepcopy(story)
    # Second anchor is absent from a named landmark shortlist, yet remains a
    # real mapped address hypothesis, never a confirmed SOURCE address.
    context = identity_discovery._map_query_context(story, [a])
    assert context['nearby_address_hypotheses'] == [a, b]
    assert story == original
    assert all(x['map_address']['scope'] == 'mapped_entry_only' for x in context['nearby_address_hypotheses'])


@pytest.mark.asyncio
@pytest.mark.parametrize('feature_already_planned', [False, True])
async def test_model_owned_feature_alternative_reaches_durable_queue_unchanged(feature_already_planned):
    a, b = anchor(1, '31', 24), anchor(2, '33', 39)
    feature = 'brick facade arched windows modern exterior Fixture Region'
    plan = ['Fixture Street 31 modern exterior', 'Fixture Street 33 modern exterior']
    if feature_already_planned:
        plan.append(feature)
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 3)
    async def generate(key, timeout, contents, config, **kwargs):
        prompt = contents[1]
        context = json.loads(prompt.split('Данные ниже — только контекст:\n')[1])
        assert context['location_search_context']['nearby_address_hypotheses'] == [a, b]
        assert 'article_queries' in config.response_json_schema['required']
        assert 'современными внешними фотографиями' in prompt
        assert 'содержательно разные запросы' in prompt
        assert contents[0].inline_data.data == normalize_reference(jpeg())[1]
        return SimpleNamespace(text=json.dumps({'entity_name': 'Hypothesis', 'wikipedia_queries': [],
            'visual_query': feature, 'commons_query': '', 'article_queries': plan}))
    service = SimpleNamespace(_source_photo_bytes=lambda _: jpeg(),
        providers=SimpleNamespace(gemini=SimpleNamespace(executor=Executor(), _generate=generate)))
    story = {'id': 'fixture', '_identity_search_context': {'nearby': [a, b]}}
    await identity_discovery.suggest(service, story, '', [])
    assert story['_identity_article_queries'] == [*plan[:2], feature]
    history = {q: {'status': 'completed'} for q in plan[:2]}
    assert identity_discovery.next_visual_query({}, 'Wrong guess', history, story['_identity_article_queries']) == feature
    assert 'address' not in story


@pytest.mark.asyncio
@pytest.mark.parametrize('tried_article', [False, True])
async def test_initial_recovery_uses_existing_reader_rank_with_attempt_fairness(monkeypatch, tried_article):
    sources = [{'url': 'https://fixture.example/maps', 'title': 'Map'},
        {'url': 'https://fixture.example/news/building', 'title': 'Exterior renovation news'},
        {'url': 'https://fixture.example/catalog', 'title': 'Directory'}]
    pages = {sources[1]['url']: {'status': 'temporary_failure', 'attempts': int(tried_article)}}
    history = {'queries': {}, 'sources': sources, 'pages': pages}
    story = {'id': 'fixture', 'photo_sha256': 'opaque-upload'}
    service = SimpleNamespace(providers=SimpleNamespace(gemini=SimpleNamespace(_generate=object(), executor=object())),
        store=SimpleNamespace(now=lambda: 100), _identity_snapshot=lambda _: (story, {}))
    async def suggest(*args):
        story['_identity_article_queries'] = ['literal model exterior query']
        return 'Hypothesis', [], 'model visual features', ''
    def retain(*args, planned_queries=(), query_results=None, **kwargs):
        if planned_queries:
            history['planned_queries'] = list(planned_queries)
        history['queries'].update(query_results or {})
        return history
    async def search(*args, **kwargs):
        return sources
    async def fetch(svc, snapshot, pending, excluded, *, receipts, first_ready):
        assert first_ready
        assert pending[0]['url'] == sources[int(not tried_article)]['url']
        assert {x['url'] for x in pending} == {x['url'] for x in sources}  # rank never excludes a source
        return [{'candidate_id': 'web:ready', 'url': pending[0]['url'], 'reference_image_urls': ['https://fixture.example/exterior.jpg']}]
    monkeypatch.setattr(identity_discovery, 'suggest', suggest)
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery', retain)
    monkeypatch.setattr(identity_discovery, '_claim_article_query', lambda *args: ('original-claim', {}))
    monkeypatch.setattr(identity_discovery, 'record_identity_event', lambda *args: None)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', fetch)
    result = await identity_discovery.recover(service, story, '', [], set())
    assert result[1][0]['candidate_id'] == 'web:ready'
