"""The model chooses a scope; one literal lookup supplies text without images."""
import hashlib
from types import SimpleNamespace

import pytest

from street_story.identity_architectural_context import acquire_regional_text, regional_preparation_query
from street_story import prussia39


def inputs():
    entrance = {'candidate_id': 'osm:node:8', 'map_address': {'city': 'Город',
        'street': 'Тестовая улица', 'house_number': '22А'}, 'map_object': {'tags': {'entrance': 'yes'}}}
    building = {'candidate_id': 'osm:way:7', 'map_coordinates': {'latitude': 54.7, 'longitude': 20.5},
        'map_object': {'tags': {'building': 'yes'}, 'building_entrances': {
            'proof': 'osm_closed_way_node_membership', 'candidate_ids': ['osm:node:8']}}}
    story = {'id': 'fixture', 'latitude': 54.7, 'longitude': 20.5,
        '_identity_observed_candidates': [building, entrance]}
    request = {'route': 'address', 'candidate_ids': ['osm:way:7'], 'address_entry_id': 'osm:node:8',
        'reason': 'A literal description can distinguish the cropped facade.'}
    return story, building, request


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'completed_empty', 'parse_failed', 'transport_failed', 'partial'])
async def test_exact_received_entrance_lookup_and_truthful_body_admission(monkeypatch, outcome):
    story, building, request = inputs()
    calls = []
    text = 'Фасад имеет центральный эркер и полукруглое завершение.'
    class Adapter:
        def __init__(self, *args): pass
        async def address_search(self, city, address):
            calls.append(('address', city, address))
            return {'status': 'completed' if outcome == 'partial' else outcome,
                'inventory_complete': outcome != 'partial',
                'results': [{'canonical_url': 'https://www.prussia39.ru/sight/index.php?sid=7',
                    'address_text': 'Город, Тестовая улица, д. 22А'}]}
        async def article(self, url):
            calls.append(('article', url))
            return {'status': 'completed', 'text': text, 'article_id': 'prussia39:sid:7',
                'canonical_url': url, 'raw_content_sha256': hashlib.sha256(text.encode('cp1251')).hexdigest(),
                'raw_body_sha256_verified': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, lookup = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert calls[0] == ('address', 'Город', 'Тестовая улица, 22А')
    # Even a partial publisher catalogue may contain an explicitly exact
    # address card; it is safe to read its body without declaring identity.
    assert len(calls) == (2 if outcome in {'completed', 'partial'} else 1)
    assert len(articles) == (1 if outcome in {'completed', 'partial'} else 0)
    if articles:
        assert articles[0]['text'] == text and articles[0]['lookup_candidate_ids'] == ['osm:way:7']
        assert articles[0]['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert lookup['status'] == ('completed' if outcome == 'partial' else outcome)


@pytest.mark.asyncio
async def test_neighbor_entrance_is_not_used_for_target_query():
    story, building, request = inputs()
    building['map_object']['building_entrances']['candidate_ids'] = ['osm:node:9']
    articles, lookup = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert not articles and lookup['status'] == 'not_sent'
    assert lookup['reason'] == 'address_entry_not_bound_to_nominated_footprint'


def test_preparation_uses_verified_subject_entries_not_camera_street():
    story, building, _ = inputs()
    story['_identity_search_context'] = {'reverse_address': {'city': 'Город', 'road': 'Камерная улица'}}
    other = {'candidate_id': 'osm:node:9', 'map_address': {'city': 'Город',
        'street': 'Тестовая улица', 'house_number': '24'}, 'map_object': {'tags': {'entrance': 'yes'}}}
    story['_identity_observed_candidates'].append(other)
    building['map_object']['building_entrances']['candidate_ids'].append(other['candidate_id'])
    query = regional_preparation_query(story, [building])
    assert query['street'] == 'Тестовая улица'
    assert query['address_entry_ids'] == ['osm:node:8', 'osm:node:9']
    assert query['candidate_ids'] == ['osm:way:7'] and query['target_identity_established'] is False
    unknown = {'candidate_id':'osm:way:10', 'map_object':{'tags':{'building':'yes'}}}
    assert regional_preparation_query(story, [building, unknown]) is None
    # A source query is scoped to supplied bodies; neither camera nor an
    # unrelated addressed entrance can become the missing subject address.
    building['map_object']['building_entrances']['proof'] = 'unverified_nearby'
    assert regional_preparation_query(story, [building]) is None
    assert regional_preparation_query(story, []) is None
    assert regional_preparation_query(story, [other]) is None


@pytest.mark.asyncio
async def test_anonymous_subject_uses_sole_verified_address_without_extra_osm(monkeypatch):
    story, building, request = inputs()
    request.pop('address_entry_id')
    story['_identity_search_context'] = {'reverse_address': {'city': 'Город', 'road': 'Другая улица'}}
    calls = []
    class Adapter:
        def __init__(self, *args): pass
        async def address_search(self, city, address):
            calls.append((city, address))
            return {'status': 'completed_empty', 'results': [], 'inventory_complete': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert not articles and calls == [('Город', 'Тестовая улица, 22А')]
    assert receipt['query_scope']['address_entry_ids'] == ['osm:node:8']
    assert receipt['query_scope']['candidate_ids'] == ['osm:way:7']
    assert receipt['query_scope']['target_identity_established'] is False


@pytest.mark.asyncio
async def test_multiple_bound_house_numbers_require_exact_entry_selection(monkeypatch):
    story, building, request = inputs()
    request.pop('address_entry_id')
    other = {'candidate_id': 'osm:node:9', 'map_address': {'city': 'Город',
        'street': 'Тестовая улица', 'house_number': '24'}, 'map_object': {'tags': {'entrance': 'yes'}}}
    story['_identity_observed_candidates'].append(other)
    building['map_object']['building_entrances']['candidate_ids'].append(other['candidate_id'])
    calls = []
    class Adapter:
        def __init__(self, *args): pass
        async def address_search(self, city, address):
            calls.append((city, address))
            return {'status': 'completed_empty', 'results': [], 'inventory_complete': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert not articles and not calls
    assert receipt['reason'] == 'one_lookup_requires_one_literal_address'
    request['address_entry_id'] = other['candidate_id']
    _, receipt = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert calls == [('Город', 'Тестовая улица, 24')]
    assert receipt['query_scope']['address_entry_ids'] == ['osm:node:9']


@pytest.mark.asyncio
async def test_addressless_subject_coordinate_lookup_uses_body_not_camera(monkeypatch):
    story, building, request = inputs()
    story['_identity_observed_candidates'] = [building]
    story['latitude'], story['longitude'] = 54.8, 20.6
    request.update(route='coordinate')
    request.pop('address_entry_id')
    calls = []
    class Adapter:
        def __init__(self, *args): pass
        async def coordinate_search(self, lat, lon):
            calls.append((lat, lon))
            return {'status': 'completed_empty', 'results': [], 'inventory_complete': True}
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    _, receipt = await acquire_regional_text(SimpleNamespace(store=object()), story, [building], request)
    assert calls == [(54.7, 20.5)]
    assert receipt['query_scope']['position_kind'] == 'subject_point_not_camera'
