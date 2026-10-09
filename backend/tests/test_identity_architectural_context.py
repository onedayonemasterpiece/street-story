"""The model chooses a scope; one literal lookup supplies text without images."""
import hashlib
from types import SimpleNamespace

import pytest

from street_story.identity_architectural_context import (
    acquire_regional_text, regional_preparation_query,
    catalogue_physical_address_links, bounded_physically_linked_article_ids,
    catalogue_model_context)
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


def test_t_article_selection_sees_real_entrance_links_before_reading_body():
    story, building, _request = inputs()
    cards = [
        {'article_id':'prussia39:sid:61',
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=61',
            'title':'Нейтральный фасад', 'address_text':'Город, Тестовая улица, 22А'},
        {'article_id':'prussia39:sid:62',
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=62',
            'title':'Соседний фасад', 'address_text':'Город, Тестовая улица, 22Б'},
        {'article_id':'prussia39:sid:61',
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=61',
            'title':'Тот же каталог', 'address_text':'Город, Тестовая улица, 22А'}]
    linked = catalogue_physical_address_links(story, [building], cards)
    assert linked['prussia39:sid:61']['matched_observed_physical_subjects'] == [{
        'candidate_id':'osm:way:7', 'mapped_entry_ids':['osm:node:8'],
        'verified_compound_entrance_ids':[],
        'join_policy':'publisher_card_and_observed_footprint_or_entrance',
        'identity_inferred':False}]
    assert linked['prussia39:sid:62']['matched_observed_physical_subjects'] == []
    catalogue={'results':cards, 'physical_address_links':linked}
    prefetch=bounded_physically_linked_article_ids(catalogue)
    assert prefetch['prefetch_article_ids'] == ['prussia39:sid:61']
    assert prefetch['physical_identity_inferred'] is False
    model=catalogue_model_context(catalogue)
    assert model['results'][0]['publisher_address_to_OSM_hypotheses'][0]['candidate_id']=='osm:way:7'
    assert model['results'][1]['publisher_address_to_OSM_hypotheses'] == []
    assert model['physical_prefetch_plan'] == {}  # Never invent a retrieval call.


def test_t_retrieval_does_not_pick_first_two_of_three_physically_linked_articles():
    story,building,_ = inputs()
    cards=[{'article_id':f'prussia39:sid:{sid}',
        'canonical_url':f'https://www.prussia39.ru/sight/index.php?sid={sid}',
        'address_text':'Город, Тестовая улица, 22А'}
        for sid in [7,8,9]]
    links=catalogue_physical_address_links(story,[building],cards)
    result=bounded_physically_linked_article_ids({
        'results':cards,'physical_address_links':links})
    assert result['candidate_article_ids'] == [
        'prussia39:sid:7','prussia39:sid:8','prussia39:sid:9']
    assert result['prefetch_article_ids'] == []
    assert result['ambiguous_excess_article_count']==1
    assert result['physical_identity_inferred'] is False


def test_t_shared_publisher_postal_address_keeps_separate_physical_bodies():
    story,building,_=inputs()
    other={'candidate_id':'osm:way:9',
        'map_address':{'city':'Город','street':'Тестовая улица','house_number':'22А'},
        'map_object':{'tags':{'building':'yes'}}}
    card={'article_id':'prussia39:sid:64',
        'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=64',
        'address_text':'Город, Тестовая улица, 22А'}
    result=catalogue_physical_address_links(story,[building,other],[card])
    bound=result['prussia39:sid:64']['matched_observed_physical_subjects']
    assert {link['candidate_id'] for link in bound} == {'osm:way:7','osm:way:9'}
    assert all(link['identity_inferred'] is False for link in bound)



def test_complex_catalogue_presents_article_before_exact_corpus_resolution():
    story, building, _request = inputs()
    card={'article_id':'prussia39:sid:707',
        'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=707',
        'address_text':'Город, Тестовая улица, 22А, 24'}
    linked=catalogue_physical_address_links(story,[building],[card])
    assert linked['prussia39:sid:707']['matched_observed_physical_subjects'] == []
    candidates=linked['prussia39:sid:707']['complex_postal_membership_hypotheses']
    assert [candidate['candidate_id'] for candidate in candidates] == ['osm:way:7']
    assert candidates[0]['corpus_identity_inferred'] is False
    catalogue={'results':[card],'physical_address_links':linked}
    plan=bounded_physically_linked_article_ids(catalogue)
    assert plan['candidate_article_ids']==['prussia39:sid:707']
    assert plan['physical_identity_inferred'] is False
    context=catalogue_model_context(catalogue)
    assert context['results'][0]['publisher_complex_postal_membership_not_identity']==candidates
