"""T-only regression: retrieve physical evidence, not a neighboring name."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story import prussia39
from street_story.identity_architectural_comparison import (
    combine_architectural_decision, prepare_architectural_comparison, publisher_address_relation,
    source_subject_competition_guard, verified_publisher_physical_scope)
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
async def test_received_neighbor_text_is_read_as_unconfirmed_evidence_without_postal_filter(monkeypatch, number, returned):
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
            card = next(row for row in received if row['canonical_url'] == url)
            text = 'A neighboring facade has a different bay arrangement.'
            return {'status':'completed', 'article_id':card['article_id'], 'canonical_url':url,
                'text':text, 'raw_body_sha256_verified':True,
                'raw_content_sha256':hashlib.sha256(text.encode()).hexdigest()}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [building], request)
    assert len(articles) == len(received)
    assert all(article['physical_binding_claimed'] is False for article in articles)
    assert len(calls) == 1
    assert len(receipt['results']) == len(received)
    assert receipt['source_acquisition_selection']['host_address_parser_used'] is False
    assert receipt['source_acquisition_selection']['identity_inferred'] is False


@pytest.mark.asyncio
async def test_larger_partial_catalogue_preserves_all_records_for_model_selection(monkeypatch):
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
    assert calls == [] and articles == []
    assert lookup['results'] == received
    assert lookup['inventory_complete'] is False
    assert 'requires model source selection' in lookup['limitation']


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
    from street_story.identity_architectural_evidence import literal_evidence_inventory
    inventory = literal_evidence_inventory(story, candidates, receipt['articles'], candidate_ids=[main, neighbor['candidate_id']])
    receipt['physical_link_inventory'] = inventory
    decision['physical_link_evidence'] = [{
        'article_id': receipt['articles'][0]['article_id'], 'candidate_id': main,
        'publisher_ref': next(iter(inventory['publisher_refs'])),
        'osm_ref': next(ref for ref, row in inventory['osm_refs'].items() if row['candidate_id'] == main),
        'relationship': 'same_individual_physical_body', 'subject_scope': 'specific_photographed_OSM_body',
        'architectural_scope_explanation': 'The distinct bay and window-axis combination belongs to the individual body.',
        'postal_interpretation': 'The received source and physical record denote the individually described building.'}]
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
    import json
    transmitted = json.loads(packet['prompt'].rsplit('\n', 1)[-1])
    hypotheses = transmitted['previous_model_hypotheses_not_evidence']['geometry_hypotheses_not_evidence']
    assert hypotheses['status'] == 'unconfirmed' and 'decision' not in hypotheses
    assert 'Coverage may be incomplete' in transmitted['coverage_limit']
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


def test_compact_positive_contract_requires_each_prior_alternative_but_uncertain_does_not():
    from jsonschema import Draft202012Validator
    story, candidates, decision, receipt = _comparison_fixture()
    packet = prepare_architectural_comparison(story, candidates, receipt)
    validator = Draft202012Validator(packet['schema'])
    decision['material_alternatives'] = []
    assert list(validator.iter_errors(decision))
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:88',
        'reason': 'The visible bay layout differs from this received neighboring body.'}]
    assert not list(validator.iter_errors(decision))
    decision.update(decision='uncertain', material_alternatives=[], material_alternatives_resolved=False)
    assert not list(validator.iter_errors(decision))


@pytest.mark.parametrize('damage', [None, 'foreign_article', 'foreign_body', 'fabricated_inventory'])
def test_physical_binding_uses_model_scope_and_actual_record_provenance(damage):
    story, candidates, decision, receipt = _comparison_fixture()
    decision['material_alternatives'] = [{'candidate_id':'osm:way:88',
        'reason':'The neighboring body lacks the SOURCE bay configuration.'}]
    # Different literal spellings remain model evidence, never a postal veto.
    receipt['articles'][0]['address'] = 'Историческая улица, 6—6А'
    decision['physical_link_evidence'][0]['postal_interpretation'] = (
        'The historic complex label requires the article architecture to distinguish this individual body.')
    if damage == 'foreign_article':
        decision['physical_link_evidence'][0]['publisher_ref'] = 'unreceived-source'
    elif damage == 'foreign_body':
        inventory = receipt['physical_link_inventory']
        decision['physical_link_evidence'][0]['osm_ref'] = next(ref for ref, row in inventory['osm_refs'].items()
            if row['candidate_id'] == 'osm:way:88')
    elif damage == 'fabricated_inventory':
        ref = decision['physical_link_evidence'][0]['osm_ref']
        receipt['physical_link_inventory']['osm_refs'][ref]['literal_value'] = 'Invented source alias'
    proof = freeze_architectural_text_proof(story, decision, receipt, candidates)
    assert (proof is not None) == (damage is None)


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


def test_reserve_pointers_do_not_expand_positive_bindings_or_force_reserve_enumeration():
    story, candidates, _decision, receipt = _comparison_fixture()
    candidates.extend({'candidate_id': f'osm:way:{i}', 'map_object': {'tags': {'building': 'yes'}}}
        for i in range(100, 300))
    prepared = prepare_architectural_comparison(story, candidates, receipt)
    properties = prepared['schema']['properties']
    assert properties['candidate_id']['enum'] == [*prepared['candidate_ids'], '']
    assert properties['article_bindings']['items']['properties']['candidate_id']['enum'] == properties['candidate_id']['enum']
    alternatives = properties['material_alternatives']
    assert 'osm:way:299' in alternatives['items']['properties']['candidate_id']['enum']
    assert alternatives['maxItems'] == 8
    assert len(json.loads(prepared['prompt'].rsplit('\n', 1)[-1])['physical_reserve']['rows']) == 200


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
    assert receipt['source_acquisition_selection']['identity_inferred'] is False
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
                'address_text':card['address_text'],
                'address_provenance':'publisher_article_metadata_table',
                'raw_content_sha256':hashlib.sha256(text.encode('cp1251')).hexdigest()}

    monkeypatch.setattr(prussia39, 'Prussia39Adapter', Adapter)
    articles, receipt = await acquire_regional_text(
        SimpleNamespace(store=object()), story, [body], request)
    assert calls == [('search', 'Калининград', 'Тестовая улица, 22А'),
                     ('body', card['canonical_url'])]
    assert articles[0]['card_variants'][0]['address_text'] == card['address_text']
    assert articles[0]['physical_binding_claimed'] is False
    assert articles[0]['address_provenance'] == 'publisher_article_metadata_table'
    assert articles[0]['address'] == card['address_text']
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
    assert 'publisher_and_OSM_literal_records_NOT_prejoined' in packet['prompt']
    assert 'no_exact_publisher_address_join_observed' not in packet['prompt']
    assert 'individual-body scope' in packet['prompt']
    assert packet['utf8_bytes'] < 20_000


def test_article_page_address_joins_without_catalogue_result_metadata():
    # Coordinate search cards lack postal data; the actual acquired article
    # includes the modern publisher address in a separate metadata table.
    article = {'article_id':'prussia39:sid:51',
        'url':'https://www.prussia39.ru/sight/index.php?sid=51',
        'address':'Калининградская область, г. Калининград, ул. Житомирская, 22, 24',
        'address_provenance':'publisher_article_metadata_table'}
    bodies = [
        {'candidate_id':'osm:way:101','literal_address_entries':[{
            'entry_id':'osm:node:1',
            'address':{'street':'Житомирская улица','house_number':'22/24'}}]},
        {'candidate_id':'osm:way:102','literal_address_entries':[{
            'entry_id':'osm:node:2',
            'address':{'street':'Житомирская улица','house_number':'22'}}]},
    ]
    links=publisher_address_relation([article],bodies)[0]
    assert links['physical_links'][0]['exact_literal_entry_ids']==['osm:node:1']
    assert links['physical_links'][1]['exact_literal_entry_ids']==[]
    assert all(not x['physical_identity_inferred'] for x in links['physical_links'])
    assert links['publisher_modern_address_metadata']==[article['address']]


def test_exact_compound_publisher_group_can_cover_two_verified_osm_entrances_without_identity():
    article={'article_id':'prussia39:sid:51',
        'url':'https://www.prussia39.ru/sight/index.php?sid=51',
        'address':'Калининградская область, г. Калининград, ул. Житомирская, 22, 24',
        'address_provenance':'publisher_article_metadata_table'}
    def entrance(node, number):
        return {'entry_id':f'osm:node:{node}',
            'address':{'street':'Житомирская улица','house_number':number},
            'provenance':'osm_closed_way_node_membership'}
    bodies=[
        {'candidate_id':'osm:way:1','literal_address_entries':[
            entrance(101,'22'),entrance(102,'24')]},
        {'candidate_id':'osm:way:2','literal_address_entries':[
            entrance(103,'22')]},
        {'candidate_id':'osm:way:3','literal_address_entries':[
            entrance(104,'22'),entrance(105,'26')]},
        {'candidate_id':'osm:way:4','literal_address_entries':[
            dict(entrance(106,'22'),provenance='osm_physical_own_address'),
            entrance(107,'24')]},
    ]
    result=publisher_address_relation([article],bodies)[0]
    links=result['physical_links']
    assert links[0]['publisher_full_group_covered_by_distinct_verified_entrances']==[
        'osm:node:101','osm:node:102']
    assert links[0]['link_kind']=='publisher_compound_group_matches_verified_entrances'
    assert all(not link['physical_identity_inferred'] for link in links)
    assert all(not link['publisher_full_group_covered_by_distinct_verified_entrances']
        for link in links[1:])


def test_only_inert_schema_type_echo_is_normalized_without_changing_llm_semantics():
    story, candidates, decision, receipt = _comparison_fixture()
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:88',
        'reason': 'SOURCE has a distinct bay layout from this received neighbor.'}]
    packet = prepare_architectural_comparison(story, candidates, receipt)
    raw = {'type':'object', **decision}
    result = combine_architectural_decision({}, raw, packet['schema'])
    assert result['accepted_architectural_text']==decision
    assert raw['type']=='object'  # The original provider result remains immutable.
    with pytest.raises(ValueError,match='architectural_comparison_model_response_invalid'):
        combine_architectural_decision({}, {'type':'building', **decision}, packet['schema'])
    with pytest.raises(ValueError,match='architectural_comparison_model_response_invalid'):
        combine_architectural_decision({}, {'type':'object','made_up_identity':True, **decision}, packet['schema'])


def test_closer_map_contour_is_only_an_observed_competitor_not_a_T_veto():
    def body(cid, meters):
        return {'candidate_id':cid,'identity_eligible':True,
            'map_object':{'tags':{'building':'yes'}},
            'boundary_distance_m':meters}
    target=body('osm:way:101',50.1)
    nearer=body('osm:way:102',43.6)
    monument={'candidate_id':'osm:node:1','identity_eligible':True,
        'map_object':{'tags':{'historic':'memorial'}},'boundary_distance_m':1.0}
    decision={'decision':'accepted_architectural_text','candidate_id':target['candidate_id']}
    result=source_subject_competition_guard(
        {'_camera_position_verified':True},[target,nearer,monument],decision)
    assert result['supported'] is True and result['applicable'] is False
    assert result['reason']=='proximity_is_not_SOURCE_subject_evidence'
    assert result['potential_competitor_count']==1
    assert result['potential_competitors'][0]['candidate_id']==nearer['candidate_id']
    assert result['potential_competitors'][0]['proximity_not_visibility'] is True


def test_unknown_GPS_and_unmeasured_distances_do_not_block_architectural_T():
    def body(cid,meters):
        return {'candidate_id':cid,'identity_eligible':True,
            'map_object':{'tags':{'building':'yes'}},
            **({'boundary_distance_m':meters} if meters is not None else {})}
    decision={'decision':'accepted_architectural_text','candidate_id':'osm:way:7',
        'material_alternatives_resolved':True}
    for camera_verified in (True,False):
        for distances in ((22.0,18.5),(18.5,22.0),(None,18.5)):
            a,b=body('osm:way:7',distances[0]),body('osm:way:8',distances[1])
            result=source_subject_competition_guard(
                {'_camera_position_verified':camera_verified},[a,b],decision)
            assert result['supported'] is True
            assert result['applicable'] is False
            assert result['potential_competitor_count']==1


def test_model_self_rejection_never_turns_distance_into_physical_proof():
    target={'candidate_id':'osm:way:7','identity_eligible':True,
        'map_object':{'tags':{'building':'yes'}},'boundary_distance_m':40.0}
    nearer={'candidate_id':'osm:way:8','identity_eligible':True,
        'map_object':{'tags':{'building':'yes'}},'boundary_distance_m':25.0}
    for reason in ('Clearly differs in gable geometry',''):
        decision={'decision':'accepted_architectural_text','candidate_id':'osm:way:7',
            'material_alternatives':[{'candidate_id':'osm:way:8','reason':reason}]}
        result=source_subject_competition_guard(
            {'_camera_position_verified':True},[target,nearer],decision)
        assert result['supported'] is True
        assert result['potential_competitors'][0]['candidate_id']==nearer['candidate_id']
        # Whether the model's architecture-based alternative rejection is valid
        # remains a question for LLM semantics + strict source quote proof.



def _observed_publisher_link_case(article_url='https://www.prussia39.ru/sight/index.php?sid=123'):
    article={'article_id':'prussia39:sid:123', 'url':article_url,
        'raw_body_sha256_verified':True,'input_kind':'acquired_article_text',
        'text':'A subject with individually described facade.', 'address':'',
        'address_provenance':'unavailable'}
    body={'candidate_id':'osm:way:1234','identity_eligible':True,
        'map_object':{'tags':{'building':'yes','ref:prussia39':'123'}},
        'map_address':{'street':'Текущая улица','house_number':'8'}}
    story={'_identity_observed_candidates':[body]}
    decision={'decision':'accepted_architectural_text',
        'candidate_id':'osm:way:1234',
        'article_bindings':[{'article_id':article['article_id'],
            'candidate_id':'osm:way:1234','scope':'Physical building',
            'binding_basis':'Publisher link is explicitly recorded on the OSM body.',
            'physical_binding_resolved':True}]}
    return story,[body],{'articles':[article]},decision


def test_observed_osm_direct_publisher_ref_is_independent_physical_link():
    story,bodies,receipt,decision=_observed_publisher_link_case()
    outcome=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert outcome['supported'] is True
    assert outcome['verified_bindings'][0]['mechanical_binding']=='observed_OSM_explicit_publisher_ref'
    story,bodies,receipt,decision=_observed_publisher_link_case()
    bodies[0]['map_object']['tags'].pop('ref:prussia39')
    bodies[0]['map_object']['tags']['website:prussia39']='https://www.prussia39.ru/sight/index.php?sid=123'
    outcome=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert outcome['supported'] is True
    assert outcome['verified_bindings'][0]['mechanical_binding']=='observed_OSM_explicit_publisher_URL'


def test_a_model_claimed_crosslink_cannot_replace_observed_osm_evidence():
    story,bodies,receipt,decision=_observed_publisher_link_case()
    bodies[0]['map_object']['tags'].pop('ref:prussia39')
    decision['article_bindings'][0]['binding_basis']='I am sure ref:prussia39 123 belongs to OSM way 1234.'
    result=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert result['supported'] is False
    assert result['reason']=='publisher_modern_address_not_observed'


def test_explicit_publisher_link_on_two_bodies_leaves_corpus_unresolved():
    story,bodies,receipt,decision=_observed_publisher_link_case()
    other={'candidate_id':'osm:way:1235','identity_eligible':True,
        'map_object':{'tags':{'building':'yes','ref:prussia39':'123'}}}
    bodies.append(other)
    result=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert result['supported'] is False
    assert result['reason']=='publisher_explicit_ref_still_ambiguous_between_physical_corpora'
    assert set(result['matching_candidate_ids'])=={'osm:way:1234','osm:way:1235'}


def test_literal_historic_osm_address_is_valid_without_merging_6_and_6a():
    story,bodies,receipt,decision=_observed_publisher_link_case()
    tags=bodies[0]['map_object']['tags']
    tags.pop('ref:prussia39')
    tags.update({'old_addr:street':'Историческая улица',
        'old_addr:housenumber':'6А'})
    receipt['articles'][0].update(address='Город, Историческая улица, 6А',
        address_provenance='publisher_article_metadata_table')
    result=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert result['supported'] is True
    assert result['verified_bindings'][0]['mechanical_binding']=='observed_OSM_explicit_historical_postal_tags'
    receipt['articles'][0]['address']='Город, Историческая улица, 6'
    denied=verified_publisher_physical_scope(story,bodies,receipt,decision)
    assert denied['supported'] is False
    assert denied['reason']=='article_modern_address_not_bound_to_nominated_physical_body'


def test_T_can_compare_received_reserve_but_cannot_invent_a_body():
    from jsonschema import Draft202012Validator
    story, candidates, decision, receipt = _comparison_fixture()
    candidates.append({'candidate_id': 'osm:way:89', 'identity_eligible': True,
        'map_object': {'tags': {'building': 'yes'}}})
    packet = prepare_architectural_comparison(story, candidates, receipt)
    decision['material_alternatives'] = [
        {'candidate_id': 'osm:way:88', 'reason': 'Earlier nominated neighboring wing differs.'},
        {'candidate_id': 'osm:way:89', 'reason': 'Another received body is material despite no acquired article.'}]
    validator = Draft202012Validator(packet['schema'])
    assert not list(validator.iter_errors(decision))
    decision['material_alternatives'][1]['candidate_id'] = 'osm:way:999'
    assert list(validator.iter_errors(decision))
