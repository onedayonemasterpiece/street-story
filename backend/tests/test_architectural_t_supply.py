"""T-only regression: retrieve physical evidence, not a neighboring name."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from street_story import prussia39
from street_story.identity_architectural_comparison import (
    combine_architectural_decision, prepare_architectural_comparison, publisher_address_relation)
from street_story.identity_architectural_context import (
    _subject_addresses, acquire_regional_text, literal_address_card_selection, regional_preparation_query)
from street_story.identity_source_selection import observed_address_context
from street_story.identity_proof import freeze_architectural_text_proof
from test_architectural_text_identity import text_inputs
from test_identity_architectural_context import inputs


def catalogue_card(sid, address):
    return {'article_id': f'prussia39:sid:{sid}',
            'canonical_url': f'https://www.prussia39.ru/sight/index.php?sid={sid}',
            'address_text': address}


@pytest.mark.asyncio
@pytest.mark.parametrize('number,returned', [
    ('22А', ['Тестовая улица, 22', 'Тестовая улица, 22/24']),
    ('6А', ['Барнаульская улица, 6']),
    ('22', ['Гастелло улица, 21', 'Гастелло улица, 24']),
])
async def test_wrong_neighbor_catalogue_never_read_as_subject_text(monkeypatch, number, returned):
    story, building, request = inputs()
    story['_identity_observed_candidates'][1]['map_address']['house_number'] = number
    received = [catalogue_card(i + 20, address) for i, address in enumerate(returned)]
    calls = []

    class Adapter:
        def __init__(self, *args):
            pass

        async def address_search(self, city, address):
            calls.append(('search', city, address))
            return {'status': 'completed', 'inventory_complete': True,
                'total_count': len(received), 'results': received}

        async def article(self, url):
            pytest.fail('A nearby address is not subject evidence and must not be read')

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [building], request)
    assert articles == []
    assert len(calls) == 1
    assert len(receipt['results']) == len(received)
    assert receipt['literal_address_selection']['matched_rows'] == 0
    assert receipt['literal_address_selection']['identity_inferred'] is False
    assert 'limitation' in receipt


@pytest.mark.asyncio
async def test_partial_catalogue_finds_exact_card_without_first_two_shortcut(monkeypatch):
    story, body, query = inputs()
    received = [
        catalogue_card(11, 'Тестовая улица, 122А'),
        catalogue_card(12, 'Тестовая улица, 22/24'),
        catalogue_card(13, 'Тестовая улица, 22А'),
        catalogue_card(14, 'Тестовая улица, 22'),
    ]
    text = 'На фасаде находится эркер с несколькими различающимися ярусами.'
    calls = []

    class Adapter:
        def __init__(self, *args):
            pass

        async def address_search(self, *args):
            return {'status': 'completed', 'inventory_complete': False, 'results': copy.deepcopy(received)}

        async def article(self, url):
            calls.append(url)
            return {'status': 'completed', 'raw_body_sha256_verified': True,
                'article_id': 'prussia39:sid:13', 'canonical_url': url, 'text': text,
                'raw_content_sha256': hashlib.sha256(text.encode('cp1251')).hexdigest()}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, lookup = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], query)
    assert calls == [received[2]['canonical_url']]
    assert articles[0]['text'] == text
    assert articles[0]['lookup_candidate_ids'] == [body['candidate_id']]
    assert lookup['inventory_complete'] is False
    assert lookup['literal_address_selection']['matched_rows'] == 1


def test_compound_address_and_suffixes_remain_distinct():
    rows = [catalogue_card(n, 'Город, ул. Житомирская, ' + number)
            for n, number in enumerate(('22', '22А', '22/24', '122'), start=1)]
    # Real publisher metadata can enumerate two postal numbers with a comma.
    rows.append(catalogue_card(5, 'Калининградская область, г. Калининград, ул. Житомирская, 22, 24'))
    assert [x['article_id'] for x in literal_address_card_selection(rows, 'Житомирская улица', '22')] == [
        'prussia39:sid:1']
    assert [x['article_id'] for x in literal_address_card_selection(rows, 'Житомирская улица', '22/24')] == [
        'prussia39:sid:3', 'prussia39:sid:5']
    assert literal_address_card_selection(rows, 'Житомирская улица', '24') == []


def _comparison_fixture():
    story, candidates, decision, receipt = text_inputs()
    main = candidates[0]['candidate_id']
    neighbor = {'candidate_id': 'osm:way:88', 'map_object': {'tags': {'building': 'yes'}},
        'map_address': {'city': 'Город', 'street': 'Тестовая улица', 'house_number': '6А'}}
    candidates.append(neighbor)
    story['_identity_observed_candidates'] = candidates
    receipt['articles'][0]['lookup_candidate_ids'] = [main]
    receipt['conditional_initial_decision'] = {
        'policy': 'conditional-initial-joint-v1', 'input_kind': 'model_hypothesis_not_evidence',
        'candidate_ids': [main, neighbor['candidate_id']],
        'source_scene_observations': {
            'observed': ['A facade has a bay.', 'Top gable is cropped.'],
            'inferred': ['The bay could be on the first volume.'],
            'unknown': ['A neighboring wing is outside the frame.']},
        'accepted_geometry': {'decision': 'uncertain', 'candidate_id': main,
            'rejected_alternatives': [{'candidate_id': neighbor['candidate_id'],
                'reason': 'May be the adjoining building.'}]}}
    return story, candidates, decision, receipt


def test_compact_source_article_packet_reuses_original_text_and_keeps_alternatives():
    story, candidates, decision, receipt = _comparison_fixture()
    packet = prepare_architectural_comparison(story, candidates, receipt)
    assert packet['candidate_ids'] == [candidates[0]['candidate_id'], 'osm:way:88']
    assert packet['article_ids'] == [receipt['articles'][0]['article_id']]
    assert packet['utf8_bytes'] < 15_000
    assert receipt['articles'][0]['text'] in packet['prompt']
    assert 'model_hypothesis_not_evidence' not in packet['prompt']  # compact hypotheses, labelled not proof
    assert 'hypotheses_not_evidence' in packet['prompt']
    assert 'osm:way:88' in packet['prompt']
    assert 'SOURCE image is a separate model input' in packet['prompt']
    assert 'No assertion that other MAP bodies do not exist' in packet['prompt']
    assert 'accepted_architectural_text' in packet['schema']['properties']['decision']['enum']
    assert packet['schema']['properties']['correspondences']['items']['properties']['feature_kind']
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:88',
        'reason': 'SOURCE shows a different arrangement of bay and gable.'}]
    plan = {'entity_name': '', 'first_wave_hypotheses': [],
        'accepted_geometry': {'decision': 'uncertain'}}
    adopted = combine_architectural_decision(plan, decision, packet['schema'])
    assert adopted['accepted_geometry'] == plan['accepted_geometry']
    assert adopted['accepted_architectural_text'] == decision
    assert 'accepted_architectural_text' not in plan
    assert freeze_architectural_text_proof(story, decision, receipt, candidates) is not None


def test_unresolved_complex_and_mutable_facade_cannot_be_host_promoted():
    story, candidates, decision, receipt = _comparison_fixture()
    decision['scope'] = 'Article describes a complex covering house 6 and neighboring 6A.'
    decision['article_bindings'][0]['physical_binding_resolved'] = False
    decision['material_alternatives_resolved'] = False
    packet = prepare_architectural_comparison(story, candidates, receipt)
    decision['decision'] = 'uncertain'
    result = combine_architectural_decision({}, decision, packet['schema'])
    assert result['accepted_architectural_text']['decision'] == 'uncertain'
    assert freeze_architectural_text_proof(story, decision, receipt, candidates) is None


def test_no_gps_requirement_for_architectural_semantics_when_article_already_exists():
    story, candidates, _, receipt = _comparison_fixture()
    story.pop('latitude', None)
    story.pop('longitude', None)
    assert prepare_architectural_comparison(story, candidates, receipt)['article_ids']


def test_received_material_candidates_and_verified_text_have_no_new_arbitrary_input_gate():
    story, candidates, _, receipt = _comparison_fixture()
    extra = [{'candidate_id': f'osm:way:{i}', 'map_object': {'tags': {'building': 'yes'}}}
        for i in range(100, 110)]
    candidates.extend(extra)
    receipt['conditional_initial_decision']['candidate_ids'].extend(item['candidate_id'] for item in extra)
    article = receipt['articles'][0]
    article['text'] *= 120
    article['text_sha256'] = hashlib.sha256(article['text'].encode()).hexdigest()
    prepared = prepare_architectural_comparison(story, candidates, receipt)
    assert len(prepared['candidate_ids']) == 12
    assert article['text'] in prepared['prompt']
    assert prepared['schema']['properties']['material_alternatives']['maxItems'] == 12


def test_t_never_makes_up_missing_source_or_discards_material_candidates():
    story, candidates, decision, receipt = _comparison_fixture()
    receipt['articles'][0]['raw_body_sha256_verified'] = False
    with pytest.raises(ValueError, match='unverified_article_input'):
        prepare_architectural_comparison(story, candidates, receipt)
    receipt['articles'][0]['raw_body_sha256_verified'] = True
    prior = receipt['conditional_initial_decision']
    prior['candidate_ids'].extend(f'osm:way:{i}' for i in range(100, 110))
    with pytest.raises(ValueError, match='physical_candidate_scope'):
        prepare_architectural_comparison(story, candidates, receipt)


@pytest.mark.asyncio
async def test_real_publisher_enumerated_address_is_retrievable_not_a_physical_verdict(monkeypatch):
    story, body, request = inputs()
    story['_identity_observed_candidates'][1]['map_address']['house_number'] = '22/24'
    received = [catalogue_card(51, 'Калининградская область, г. Калининград, ул. Тестовая, 22, 24')]
    article_text = 'Эркер: у нижних ярусов прямоугольный, а выше многогранный план.'
    urls = []

    class Adapter:
        def __init__(self, *args):
            pass

        async def address_search(self, *args):
            return {'status': 'completed', 'inventory_complete': True, 'results': received}

        async def article(self, url):
            urls.append(url)
            return {'status': 'completed', 'article_id': 'prussia39:sid:51',
                    'canonical_url': url, 'text': article_text,
                    'raw_body_sha256_verified': True,
                    'raw_content_sha256': hashlib.sha256(article_text.encode('cp1251')).hexdigest()}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], request)
    assert urls == [received[0]['canonical_url']]
    assert articles[0]['text'] == article_text
    assert receipt['literal_address_selection']['identity_inferred'] is False
    assert articles[0]['lookup_candidate_ids'] == [body['candidate_id']]


def test_footprint_own_address_does_not_hide_distinct_verified_entrance():
    story, body, _request = inputs()
    body['map_address'] = {'city':'Калининград', 'street':'Литературная улица', 'house_number':'6'}
    entrance = story['_identity_observed_candidates'][1]
    entrance['map_address'] = {'city':'Калининград', 'street':'Литературная улица', 'house_number':'6А'}
    entries = _subject_addresses(observed_address_context(story, [body]), body)
    assert {item['mapped_entry_id'] for item in entries} == {'osm:way:7', 'osm:node:8'}
    assert {item['address']['house_number'] for item in entries} == {'6', '6А'}
    # No implicit substitution of 6 or 6A, even though both are attached to
    # one actual OSM footprint by distinct literal provenance.
    scope = regional_preparation_query(story, [body])
    assert scope['street'] == 'Литературная улица'
    assert set(scope['address_entry_ids']) == {'osm:way:7', 'osm:node:8'}


@pytest.mark.asyncio
async def test_observed_entrance_city_allows_architecture_lookup_without_camera_gps(monkeypatch):
    story, body, request = inputs()
    story.pop('latitude', None)
    story.pop('longitude', None)
    body.pop('map_coordinates', None)
    story['_identity_observed_candidates'][1]['map_address']['city'] = 'Калининград'
    text = 'На угловом фасаде сохранился гранёный эркер.'
    card = catalogue_card(99, 'Калининград, ул. Тестовая, 22А')
    calls = []

    class Adapter:
        def __init__(self, *args):
            pass

        async def address_search(self, city, address):
            calls.append(('search', city, address))
            return {'status':'completed', 'inventory_complete':True, 'results':[card]}

        async def article(self, url):
            calls.append(('body', url))
            return {'status':'completed', 'article_id':'prussia39:sid:99',
                'canonical_url':url, 'raw_body_sha256_verified':True, 'text':text,
                'raw_content_sha256':hashlib.sha256(text.encode('cp1251')).hexdigest()}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], request)
    assert calls == [('search', 'Калининград', 'Тестовая улица, 22А'),
                     ('body', card['canonical_url'])]
    assert articles[0]['card_variants'][0]['address_text'] == card['address_text']
    assert articles[0]['physical_binding_claimed'] is False
    assert receipt['query_scope']['target_identity_established'] is False


@pytest.mark.asyncio
async def test_no_region_evidence_prevents_spurious_regional_requests_without_gps(monkeypatch):
    story, body, request = inputs()
    story.pop('latitude', None)
    story.pop('longitude', None)
    body.pop('map_coordinates', None)
    class Forbidden:
        def __init__(self, *args):
            pytest.fail('Never contact regional publisher for an unobserved region')
    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Forbidden)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], request)
    assert not articles
    assert receipt['reason'] == 'regional_catalog_outside_observed_area'


@pytest.mark.asyncio
async def test_observed_subject_coordinates_allow_regional_lookup_without_photo_gps(monkeypatch):
    story, body, request = inputs()
    story.pop('latitude', None)
    story.pop('longitude', None)
    story['_identity_observed_candidates'][1]['map_address']['city'] = ''
    request.update(route='coordinate')
    request.pop('address_entry_id')
    card = catalogue_card(46, '')
    observed = []

    class Adapter:
        def __init__(self, *args):
            pass

        async def coordinate_search(self, latitude, longitude):
            observed.append((latitude, longitude))
            return {'status':'completed', 'inventory_complete':True, 'results':[card]}

        async def article(self, url):
            return {'status':'completed', 'article_id':card['article_id'],
                'canonical_url':url, 'raw_body_sha256_verified':True,
                'text':'Описан сложный фасад с ризалитом.',
                'raw_content_sha256': 'a'*64}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], request)
    assert observed == [(54.7, 20.5)]
    assert articles[0]['lookup_candidate_ids'] == [body['candidate_id']]
    assert receipt['query_scope']['position_kind'] == 'subject_point_not_camera'


def test_publisher_card_address_links_are_literally_bound_and_not_identity():
    text = 'An article may describe a historical complex with changed facades.'
    first = {'article_id':'prussia39:sid:100', 'url':'https://www.prussia39.ru/sight/index.php?sid=100',
        'text':text, 'card_variants':[{
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=100',
            'address_text':'г. Калининград, ул. Камерная, 22, 24'}]}
    second = {'article_id':'prussia39:sid:101', 'url':'https://www.prussia39.ru/sight/index.php?sid=101',
        'text':text, 'card_variants':[{
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=101',
            'address_text':'г. Калининград, ул. Камерная, 6'}]}
    bodies = [
        {'candidate_id':'osm:way:1', 'literal_address_entries':[{
            'entry_id':'osm:node:11',
            'address':{'street':'Камерная улица', 'house_number':'22/24'}}]},
        {'candidate_id':'osm:way:2', 'literal_address_entries':[{
            'entry_id':'osm:node:12',
            'address':{'street':'Камерная улица', 'house_number':'6А'}}]},
    ]
    relations = publisher_address_relation([first, second], bodies)
    assert relations[0]['physical_links'][0]['exact_literal_entry_ids'] == ['osm:node:11']
    assert relations[0]['physical_links'][1]['exact_literal_entry_ids'] == []
    assert all(not link['physical_identity_inferred'] for row in relations
        for link in row['physical_links'])
    # A publisher card for 6 does not physically bind the 6A corpus.
    assert relations[1]['physical_links'][1]['link_kind'] == 'no_exact_publisher_address_join_observed'
    assert relations[0]['publisher_modern_address_metadata'] == [
        'г. Калининград, ул. Камерная, 22, 24']


def test_model_receives_physical_address_uncertainty_as_data_not_a_verdict():
    story, candidates, _decision, receipt = _comparison_fixture()
    url = receipt['articles'][0]['url']
    receipt['articles'][0]['card_variants'] = [
        {'canonical_url':url, 'address_text':'Город, Тестовая улица, 6'}]
    packet = prepare_architectural_comparison(story, candidates, receipt)
    assert 'literal_publisher_address_links_not_identity' in packet['prompt']
    assert 'no_exact_publisher_address_join_observed' in packet['prompt']
    assert 'physical-scope uncertainty' in packet['prompt']
    assert packet['utf8_bytes'] < 20_000
