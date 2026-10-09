"""T-only regression: retrieve physical evidence, not a neighboring name."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from street_story import prussia39
from street_story.identity_architectural_comparison import (
    combine_architectural_decision, prepare_architectural_comparison,
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
    candidates[0]['map_address']={'city':'Город',
        'street':'Тестовая улица','house_number':'6'}
    neighbor = {'candidate_id': 'osm:way:88', 'map_object': {'tags': {'building': 'yes'}},
        'map_address': {'city': 'Город', 'street': 'Тестовая улица', 'house_number': '6А'}}
    candidates.append(neighbor)
    story['_identity_observed_candidates'] = candidates
    receipt['articles'][0]['lookup_candidate_ids'] = [main]
    receipt['articles'][0]['address_provenance']='publisher_article_metadata_table'
    receipt['articles'][0]['address']='Город, Тестовая улица, 6'
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




def _actual_link_claim(packet, decision):
    inv=packet['literal_evidence_inventory']
    aid=decision['article_bindings'][0]['article_id']
    cid=decision['candidate_id']
    publisher_ref=next(ref for ref,row in inv['publisher_refs'].items()
        if row['article_id']==aid
        and row['provenance']=='observed_publisher_article_metadata')
    osm_ref=next(ref for ref,row in inv['osm_refs'].items()
        if row['candidate_id']==cid
        and row['kind']=='observed_OSM_postal_entry')
    return {'article_id':aid,'candidate_id':cid,'publisher_ref':publisher_ref,
        'osm_ref':osm_ref,'relationship':'same_individual_physical_body',
        'subject_scope':'specific_photographed_OSM_body',
        'architectural_scope_explanation':
            'SOURCE uniquely depicts this particular observed building facade.',
        'postal_interpretation':
            'LLM compared original publisher evidence and actual OSM address.'}

def test_compact_source_article_packet_reuses_original_text_and_keeps_alternatives():
    story, candidates, decision, receipt = _comparison_fixture()
    packet = prepare_architectural_comparison(story, candidates, receipt,
        require_grounded_refs=True)
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
    assert packet['literal_evidence_inventory']['host_address_parser_used'] is False
    assert 'publisher_and_OSM_literal_evidence_unjoined' in packet['prompt']
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:88',
        'reason': 'SOURCE shows a different arrangement of bay and gable.'}]
    plan = {'entity_name': '', 'first_wave_hypotheses': [],
        'accepted_geometry': {'decision': 'uncertain'}}
    answer={**decision, 'physical_link_evidence':[_actual_link_claim(packet,decision)]}
    adopted = combine_architectural_decision(plan, answer, packet['schema'],
        literal_evidence_inventory=packet['literal_evidence_inventory'])
    assert adopted['accepted_geometry'] == plan['accepted_geometry']
    assert adopted['accepted_architectural_text']['candidate_id']==decision['candidate_id']
    assert adopted['accepted_architectural_text']['physical_link_evidence']==(
        answer['physical_link_evidence'])
    assert all(adopted['accepted_architectural_text'][key]==value
        for key,value in decision.items())
    assert 'accepted_architectural_text' not in plan
    assert freeze_architectural_text_proof(story, decision, receipt, candidates) is not None


def test_unresolved_complex_and_mutable_facade_cannot_be_host_promoted():
    story, candidates, decision, receipt = _comparison_fixture()
    decision['scope'] = 'Article describes a complex covering house 6 and neighboring 6A.'
    decision['article_bindings'][0]['physical_binding_resolved'] = False
    decision['material_alternatives_resolved'] = False
    packet = prepare_architectural_comparison(story, candidates, receipt,
        require_grounded_refs=True)
    decision['decision'] = 'uncertain'
    result = combine_architectural_decision({}, {**decision,
        'physical_link_evidence':[]}, packet['schema'],
        literal_evidence_inventory=packet['literal_evidence_inventory'])
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


def test_model_receives_physical_address_uncertainty_as_data_not_a_verdict():
    story, candidates, _decision, receipt = _comparison_fixture()
    url = receipt['articles'][0]['url']
    receipt['articles'][0]['card_variants'] = [
        {'canonical_url':url, 'address_text':'Город, Тестовая улица, 6'}]
    packet = prepare_architectural_comparison(story, candidates, receipt,
        require_grounded_refs=True)
    assert 'publisher_and_OSM_literal_evidence_unjoined' in packet['prompt']
    assert 'postal_relationship_decision_by' in packet['prompt']
    assert 'physical_link_evidence' in packet['schema']['properties']
    assert packet['utf8_bytes'] < 20_000


def test_only_inert_schema_type_echo_is_normalized_without_changing_llm_semantics():
    story, candidates, decision, receipt = _comparison_fixture()
    packet = prepare_architectural_comparison(story, candidates, receipt,
        require_grounded_refs=True)
    raw = {'type':'object', **decision,
        'physical_link_evidence':[_actual_link_claim(packet,decision)]}
    result = combine_architectural_decision({}, raw, packet['schema'],
        literal_evidence_inventory=packet['literal_evidence_inventory'])
    assert result['accepted_architectural_text']==decision
    assert raw['type']=='object'  # The original provider result remains immutable.
    with pytest.raises(ValueError,match='architectural_comparison_model_response_invalid'):
        combine_architectural_decision({}, {'type':'building', **decision,
            'physical_link_evidence':[_actual_link_claim(packet,decision)]}, packet['schema'],
            literal_evidence_inventory=packet['literal_evidence_inventory'])
    with pytest.raises(ValueError,match='architectural_comparison_model_response_invalid'):
        combine_architectural_decision({}, {'type':'object','made_up_identity':True,
            **decision,'physical_link_evidence':[_actual_link_claim(packet,decision)]}, packet['schema'],
            literal_evidence_inventory=packet['literal_evidence_inventory'])


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



def test_legacy_postal_proof_cannot_be_promoted_without_llm_refs():
    story,candidates,decision,receipt=_comparison_fixture()
    admission=verified_publisher_physical_scope(
        story,candidates,receipt,decision)
    assert admission['supported'] is False
    assert admission['reason']=='llm_first_publisher_and_osm_evidence_receipt_required'
