"""Planner outages must not prevent durable independent search work."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story import article_media, identity_discovery
from street_story.gemini import GeminiUnavailable
from street_story.identity_source_selection import regional_source_profile
from test_visual_search_continuation import prepared


@pytest.mark.asyncio
async def test_saved_plan_reaches_independent_search_without_google_planner(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    snapshot = svc._identity_snapshot(story['id'])[0]
    identity_discovery._retain_article_discovery(svc, snapshot, [],
        planned_queries=['Mapped city First street 1', 'Mapped city Second street 3'],
        query_results={'Mapped city First street 1': {'status': 'completed', 'sources': []}})
    svc.providers.gemini = SimpleNamespace()  # no Google capability or executor
    async def forbidden(*args, **kwargs):
        pytest.fail('A saved plan must be loaded before any planner is addressed')
    monkeypatch.setattr(identity_discovery, 'suggest', forbidden)
    calls = []
    async def search(service, entity, visual, *, story, first_ready):
        calls.append(story['_identity_search_query'])
        return [{'url': 'https://example.com/independent-building'}]
    async def articles(service, snapshot, sources, excluded, *, receipts, first_ready):
        receipts.append({'url': sources[0]['url'], 'status': 'completed'})
        return [{'candidate_id': 'web:observed', 'url': sources[0]['url'],
                 'reference_image_urls': ['https://example.com/facade.jpg']}]
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    assert await identity_discovery.recover(svc, snapshot, '', [], set())
    assert calls == ['Mapped city Second street 3']
    # A wake consumes the exact saved query result and article, without a send.
    snapshot = svc._identity_snapshot(story['id'])[0]
    assert await identity_discovery.recover(svc, snapshot, '', [], set())
    assert calls == ['Mapped city Second street 3']


@pytest.mark.asyncio
async def test_google_unavailable_uses_qualified_existing_text_planner_once(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    class Closed:
        async def execute(self, operation, call):
            raise GeminiUnavailable(None, 'daily_admission_closed')
    svc.providers.gemini.executor = Closed()
    svc.providers.gemini._generate = object()
    svc.providers.gemini.research_routes = []
    plans, searches = [], []
    async def qualified(snapshot, prompt, schema):
        plans.append(prompt)
        assert 'SOURCE image is unavailable' in prompt
        assert 'regional_source_profile' in prompt
        return {'result': {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '',
            'commons_query': '', 'article_queries': ['Observed city Observed road 4']}}
    svc.providers.research = SimpleNamespace(plan_identity_search=qualified)
    async def search(service, entity, visual, *, story, first_ready):
        searches.append(story['_identity_search_query'])
        assert svc._identity_snapshot(story['id'])[1]['identity_article_discovery']['search_plan']['route'] == 'qualified_text_fallback'
        return []
    async def no_wiki(*args, **kwargs):
        return []
    async def no_articles(*args, **kwargs):
        return []
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(identity_discovery, 'retrieve', no_wiki)
    monkeypatch.setattr(article_media, 'article_candidates', no_articles)
    for _ in range(2):
        snapshot = svc._identity_snapshot(story['id'])[0]
        assert await identity_discovery.recover(svc, snapshot, '', [], set()) is None
    assert len(plans) == 1 and searches == ['Observed city Observed road 4']


@pytest.mark.asyncio
async def test_first_wave_uses_different_queries_routes_and_returns_before_slow_tail(tmp_path, monkeypatch):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    snapshot = svc._identity_snapshot(story['id'])[0]
    plan = ['First observed address', 'Second observed address', 'Observed facade hypothesis']
    identity_discovery._retain_article_discovery(svc, snapshot, [], planned_queries=plan)
    tail = asyncio.Event()
    all_started = asyncio.Event()
    calls, observers = [], []
    svc.providers.research = SimpleNamespace(search_articles=lambda *args: None,
        retain_search_observer=observers.append)
    svc.providers.gemini = SimpleNamespace(discover_article_urls=lambda *args: None,
        _public_web_search=lambda *args: None)
    async def search(service, entity, visual, *, story, first_ready):
        calls.append((story['_identity_search_query'], story['_identity_search_route']))
        if len(calls) == 3:
            all_started.set()
        await all_started.wait()
        if story['_identity_search_query'] == plan[0]:
            await tail.wait()
            return []
        return [{'url': 'https://example.com/ready'}]
    async def articles(service, snapshot, sources, excluded, *, receipts, first_ready):
        receipts.append({'url': sources[0]['url'], 'status': 'completed'})
        return [{'candidate_id': 'web:ready', 'url': sources[0]['url'],
                 'reference_image_urls': ['https://example.com/facade.jpg']}]
    monkeypatch.setattr(identity_discovery, 'web_image_sources', search)
    monkeypatch.setattr(article_media, 'article_candidates', articles)
    result = await asyncio.wait_for(identity_discovery.recover(svc, snapshot, '', [], set()), 2)
    assert result and not tail.is_set()
    assert calls == list(zip(plan, ['opencode', 'google', 'public_web']))
    assert len(observers) == 1
    tail.set()
    await observers[0]


def test_region_profile_requires_observed_locality_and_never_uses_tenant_names():
    assert 'regional_sources' not in regional_source_profile({}, [{'name': 'Калининградский ресторан'}])
    assert 'regional_sources' not in regional_source_profile({'_identity_search_context': {
        'reverse_address': {'city': 'Another city'}}})
    profile = regional_source_profile({'_identity_search_context': {'reverse_address': {
        'city': 'Калининград', 'road': 'Observed street', 'house_number': '7'}}})
    assert 'prussia39.ru' in {item['domain'] for item in profile['regional_sources']}
    assert 'Observed street' not in json.dumps(profile)


def test_ineligible_entrance_remains_observed_address_anchor_without_identity_promotion():
    entrance = {'candidate_id': 'osm:node:7', 'identity_eligible': False,
        'map_address': {'street': 'Observed street', 'house_number': '7', 'scope': 'mapped_entry_only'}}
    context = identity_discovery._map_query_context({'_identity_observed_candidates': [entrance]}, [])
    assert context['nearby_address_hypotheses'] == [{'candidate_id': entrance['candidate_id'],
        'map_address': entrance['map_address']}]


@pytest.mark.asyncio
async def test_planner_can_promote_existing_building_outside_active_shortlist(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    physical = {'candidate_id': 'osm:way:999', 'name': 'Observed physical building',
        'map_object': {'tags': {'building': 'yes'}}, 'map_address': {'street': 'Observed street'},
        'map_coordinates': {'latitude': 54.7, 'longitude': 20.5}}
    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)
    async def generate(key, timeout, contents, config, **kwargs):
        context = json.loads(contents[1].split('Данные ниже — только контекст:\n')[1])
        rows = context['location_search_context']['observed_physical_candidates']['rows']
        assert physical['candidate_id'] in [row[0] for row in rows]
        assert config.response_json_schema['properties']['observed_candidate_ids']['items']['enum'] == [physical['candidate_id']]
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': '', 'commons_query': '', 'article_queries': ['Observed city Observed street'],
            'observed_candidate_ids': [physical['candidate_id']]}))
    svc.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    snapshot = {**svc._identity_snapshot(story['id'])[0], '_identity_observed_candidates': [physical]}
    active = [{'candidate_id': 'osm:way:1', 'name': 'Original hypothesis'}]
    await identity_discovery.suggest(svc, snapshot, '', active)
    assert active[1]['candidate_id'] == physical['candidate_id']
    assert active[1]['shortlist_bucket'] == 'observed_promotion'
    assert 'identity_status' not in active[1]  # promotion supplies no proof
