from test_reference_image_codec import jpeg
from street_story.reference_image_codec import normalize_reference
import json
from types import SimpleNamespace

import pytest

from street_story.identity_discovery import suggest, region_hint, web_search_hints
from street_story.identity_map_context import map_entry_context
from street_story.mvp_research import MvpResearchStreetStoryService


def test_map_address_coordinates_and_provenance_are_object_scoped_not_inferred_from_name():
    item = {'type': 'way', 'id': 27, 'center': {'lat': 54.71, 'lon': 20.507},
            'tags': {'name': 'Named shop', 'building': 'yes', 'addr:street': 'Test Avenue',
                     'addr:housenumber': '48А'}, 'distance_m': 40, 'selection_bucket': 'nearby'}
    candidate = MvpResearchStreetStoryService._candidate_catalog({'nearby': [item]}, [])[0]
    assert candidate['name'] == 'Named shop'
    assert candidate['map_address'] == {'street': 'Test Avenue', 'house_number': '48А',
        'provenance': 'osm.tags', 'source_url': 'https://www.openstreetmap.org/way/27',
        'scope': 'mapped_entry_only'}
    assert candidate['map_coordinates'] == {'latitude': 54.71, 'longitude': 20.507,
        'provenance': 'osm.center', 'source_url': 'https://www.openstreetmap.org/way/27'}


def test_real_road_name_remains_search_context_without_invented_house_number_or_identity():
    road = {'type': 'way', 'id': 12, 'tags': {'name': 'Nearby Road', 'highway': 'residential'},
            'center': {'lat': 54.71, 'lon': 20.507}, 'distance_m': 9, 'selection_bucket': 'nearby'}
    assert map_entry_context(road)['road_name'] == 'Nearby Road'
    assert 'map_address' not in map_entry_context(road)
    assert MvpResearchStreetStoryService._candidate_catalog({'nearby': [road]}, []) == []


def test_reverse_road_address_does_not_borrow_nearby_house_number():
    reverse = {'osm_type': 'way', 'osm_id': 12, 'lat': '54.71', 'lon': '20.507',
               'address': {'road': 'Nearby Road'}}
    result = map_entry_context(reverse)
    assert result['map_address'] == {'street': 'Nearby Road', 'provenance': 'nominatim.reverse.address',
        'source_url': 'https://www.openstreetmap.org/way/12', 'scope': 'mapped_entry_only'}
    assert 'house_number' not in result['map_address']


def test_reverse_mapped_object_kind_reaches_visual_subject_resolution():
    from street_story.headless_identity import HeadlessIdentity
    reverse = {'osm_type': 'way', 'osm_id': 12, 'category': 'amenity', 'type': 'parking',
               'addresstype': 'amenity', 'display_name': 'Nearby Road, City',
               'lat': '54.71', 'lon': '20.507', 'distance_m': 9,
               'address': {'road': 'Nearby Road'}, 'selection_bucket': 'reverse'}
    candidate = MvpResearchStreetStoryService._candidate_catalog({'reverse': reverse}, [])[0]
    ref = {'candidate_id': 'web:reference', 'reference_id': 'ref-one', 'name': 'Building',
           'url': 'https://example.com/article', 'reference_image_urls': ['https://example.com/ref.jpg']}
    context = HeadlessIdentity._visual_reply('comparison', [ref], {'candidates': [candidate]}, 0)
    mapped = context['physical_candidates'][0]['map_object']
    assert mapped['category'] == 'amenity' and mapped['type'] == 'parking'
    assert mapped['scope'] == 'mapped_entry_only'
    assert context['physical_candidates'][0]['map_address']['street'] == 'Nearby Road'


@pytest.mark.parametrize('item', [
    {'tags': {'name': 'Somewhere 99'}},
    {'tags': {'building': 'yes'}, 'center': {'lat': float('nan'), 'lon': 20}},
    {'lat': True, 'lon': 20}, {},
])
def test_missing_address_or_invalid_position_remains_unknown(item):
    assert 'map_address' not in map_entry_context(item)
    assert 'map_coordinates' not in map_entry_context(item)


@pytest.mark.asyncio
async def test_suggest_receives_structured_anchors_several_roads_and_still_searches_without_number():
    contexts = []
    contents_seen = []

    class Executor:
        async def execute(self, operation, call):
            return await call('fixture', 5)

    async def generate(key, timeout, contents, config, **kwargs):
        contents_seen.append(contents)
        prompt = contents[1]
        context = json.loads(prompt.split('Данные ниже — только контекст:\n')[1])
        contexts.append(context)
        assert 'Адрес — поисковый якорь наравне с названием' in prompt
        assert 'Номер соседнего дома и предположение модели не становятся фактом' in prompt
        assert 'неизвестный адрес не блокирует поиск' in prompt
        return SimpleNamespace(text=json.dumps({'entity_name': '', 'wikipedia_queries': [],
            'visual_query': 'brick building', 'commons_query': '',
            'article_queries': ['First Road brick building', 'Second Road brick building']}))

    map_item = {'type': 'way', 'id': 27, 'center': {'lat': 54.71, 'lon': 20.507},
        'tags': {'building': 'yes', 'addr:street': 'Second Road'},
        'selection_bucket': 'nearby', 'distance_m': 40}
    candidates = MvpResearchStreetStoryService._candidate_catalog({'nearby': [map_item]}, [])
    street_contexts = [map_entry_context({'type': 'way', 'id': i,
        'tags': {'name': name, 'highway': 'residential'}})
        for i, name in [(1, 'First Road'), (2, 'Second Road')]]
    story = {'id': 'fixture', 'photo_mime_type': 'image/jpeg', 'latitude': 54.71, 'longitude': 20.507,
        '_identity_search_context': {'reverse_address': {'road': 'First Road'}, 'nearby': street_contexts}}
    service = SimpleNamespace(_source_photo_bytes=lambda _: jpeg(),
        providers=SimpleNamespace(gemini=SimpleNamespace(executor=Executor(), _generate=generate)))
    result = await suggest(service, story, '', candidates)
    assert result == ('', [], 'brick building', '')
    supplied = contexts[0]['nearby_candidates'][0]
    assert supplied['map_address']['street'] == 'Second Road'
    assert 'house_number' not in supplied['map_address']
    assert supplied['map_coordinates']['provenance'] == 'osm.center'
    assert [item['road_name'] for item in contexts[0]['location_search_context']['nearby']] == ['First Road', 'Second Road']
    assert story['_identity_article_queries'] == ['First Road brick building', 'Second Road brick building', 'brick building']
    assert contents_seen[0][0].inline_data.data == normalize_reference(jpeg())[1]
    assert 'address' not in story  # no model search hint becomes confirmed subject data


def test_wikipedia_map_coordinates_keep_authoritative_source_not_osm_provenance():
    candidate = MvpResearchStreetStoryService._candidate_catalog({}, [{
        'pageid': 77, 'title': 'Named tower', 'url': 'https://ru.wikipedia.org/wiki/Tower',
        'lat': 54.71, 'lon': 20.507, 'distance_m': 25}])[0]
    assert candidate['map_coordinates'] == {'latitude': 54.71, 'longitude': 20.507,
        'provenance': 'wikipedia.geosearch', 'source_url': 'https://ru.wikipedia.org/wiki/Tower'}
    assert candidate['candidate_id'] == 'wiki:77'
    assert 'map_address' not in candidate


@pytest.mark.asyncio
async def test_unknown_geography_is_not_replaced_by_a_default_city():
    calls = []
    async def search(query, context):
        calls.append((query, context))
        return SimpleNamespace(grounding_sources=[])
    service = SimpleNamespace(providers=SimpleNamespace(gemini=SimpleNamespace(search_web=search)))
    await web_search_hints(service, 'white church clock tower', story={})
    assert calls == [('white church clock tower', {'purpose': 'identity_candidate_discovery', 'region': ''})]
    assert region_hint({'_identity_search_context': {'reverse_address': {
        'city': 'Саратов', 'state': 'Саратовская область', 'country': 'Россия'}}}) == 'Саратов, Саратовская область, Россия'
