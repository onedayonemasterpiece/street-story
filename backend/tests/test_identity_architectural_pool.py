"""T architecture-selection regression; positive proof from more than two real receipts."""
import copy
import hashlib

import pytest

from street_story.identity_architectural_pool import (
    _excerpt, _source_span_options, prepare_architectural_pool, close_architectural_pool_response)
from test_architectural_text_identity import text_inputs


def _article(aid, text):
    return {'article_id':aid,
        'url':'https://archive.example/physical-building',
        'title':'Publisher architecture', 'scope':'Observed physical building',
        'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'raw_body_sha256_verified':True,'input_kind':'acquired_article_text',
        'text':text}


def _three_documents():
    story,candidates,decision,receipt=text_inputs()
    # An observed real OSM address record, not a parser-generated
    # correspondence. The model decides whether it describes the photo.
    candidates[0]['map_address']={'street':'Observed literal street',
        'house_number':'7'}
    story['_identity_observed_candidates']=candidates
    receipt['articles'][0]['address_provenance']='publisher_article_metadata_table'
    first=receipt['articles'][0]
    second=_article('catalog:neighbor-one',
        'The building is historically important but the article describes no visible facade.')
    third=_article('catalog:neighbor-two',
        'An alternative building is on a different historic street and has an unrelated roof.')
    receipt['articles']=[second,third,first]
    return story,candidates,decision,receipt


def _closed_answer(decision,ids,packet):
    result=copy.deepcopy(decision)
    evidence=packet['literal_evidence_inventory']
    aid=result['article_bindings'][0]['article_id']
    physical_id=result['candidate_id']
    publisher_ref=next(item['ref'] for item in evidence['articles']
        if item['article_id']==aid for item in
        item['actual_acquired_publisher_records'])
    osm_ref=next(item['ref'] for body in evidence['physical_subjects']
        if body['candidate_id']==physical_id
        for item in body['literal_observed_evidence'])
    result['physical_link_evidence']=[{
        'article_id':aid,'candidate_id':physical_id,
        'publisher_ref':publisher_ref,'osm_ref':osm_ref,
        'relationship':'same_individual_physical_body',
        'subject_scope':'specific_photographed_OSM_body',
        'architectural_scope_explanation':
            'SOURCE shows the individual gable/window group from this observed body.',
        'postal_interpretation':
            'Model attributes two real literal records, not a parser.'}]
    for relation in result['correspondences']:
        candidates=[ref for ref,span in packet['source_span_refs'].items()
            if span['article_id']==relation['article_id']
            and relation['source_quote'] in span['source_quote']]
        assert candidates
        relation.pop('source_quote')
        relation['source_span_ref']=candidates[0]
    assert result['correspondences']
    second=copy.deepcopy(result['correspondences'][0])
    second['feature_kind']='bay'
    second['source_observation']='The separate central bay projects forward along the three window axes.'
    result['correspondences'].append(second)
    return {**result, 'article_comparisons':[{
        'article_id':cid,
        'visual_fit':'distinctive_match' if cid==decision['article_bindings'][0]['article_id'] else 'generic_only',
        'architectural_difference':'Observed bay and cornice combination differs from neighboring buildings.',
        'visible_SOURCE_specifics':'The SOURCE has an individual central bay and aligned axes.'}
        for cid in ids]}


def test_architecture_excerpt_preserves_verbatim_actual_passages_and_later_renovation():
    introductory='Дом имеет историческую ценность. '*85
    material='Фасад: три оси окон и прямоугольный эркер на верхних этажах. '
    mutable='При реставрации 2010 года часть декора восстановлена. '
    body=introductory+'\n'+material*17+'\n'+mutable*12
    assert len(body)>3800
    segments=_excerpt(body,max_chars=2100)
    assert sum(len(x['text']) for x in segments)<=2100
    assert all(body[x['start']:x['end']]==x['text'] for x in segments)
    joined='\n'.join(x['text'] for x in segments)
    assert 'фасад' in joined.casefold()
    assert 'реставрации' in joined.casefold()
    assert len(joined)<len(body)
    assert segments[0]['start']==0


def test_three_actual_articles_reach_one_contrastive_model_call_and_correct_third_can_win():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    assert packet['article_ids']==[
        'catalog:neighbor-one','catalog:neighbor-two','catalog:physical-building']
    assert packet['input_contract']=='source-multiple-architecture-pool-v2-literal-span-refs'
    assert packet['text_utf8_bytes']<16000
    assert 'source_span_ref' in str(packet['schema'])
    assert 'physical_link_evidence' in packet['schema']['properties']
    assert 'host_postal_address_interpretation' in packet['prompt']
    assert 'source_quote' not in str(packet['schema'])
    assert packet['schema']['properties']['article_comparisons']['minItems']==3
    output=_closed_answer(decision,packet['article_ids'],packet)
    final=close_architectural_pool_response(story,candidates,packet,output,
        source_text_receipt=receipt)
    assert final['accepted'] is True
    proof=final['proof']
    assert proof['candidate_id']=='osm:way:7'
    assert [article['article_id'] for article in proof['source_text_receipt']['articles']] == [
        'catalog:physical-building']
    assert proof['source_text_receipt']['articles'][0]['raw_body_sha256_verified'] is True
    assert final['evidence_model_response_sha256']


def test_incomplete_contrast_and_wrong_article_quote_cannot_yield_identity():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    good=_closed_answer(decision,packet['article_ids'],packet)
    partial=copy.deepcopy(good)
    partial['article_comparisons'].pop()
    with pytest.raises(ValueError,match='model_architectural_pool_response_malformed'):
        close_architectural_pool_response(story,candidates,packet,partial,source_text_receipt=receipt)
    duplicate=copy.deepcopy(good)
    duplicate['article_comparisons'][0]['article_id']=duplicate['article_comparisons'][1]['article_id']
    with pytest.raises(ValueError,match='unassessed_real_publisher_article'):
        close_architectural_pool_response(story,candidates,packet,duplicate,source_text_receipt=receipt)
    missing_support=copy.deepcopy(good)
    missing_support['article_comparisons'][-1]['visual_fit']='generic_only'
    result=close_architectural_pool_response(story,candidates,packet,missing_support,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='positive_binding_not_supported_by_model_contrast'
    invented=copy.deepcopy(good)
    invented['correspondences'][0]['source_span_ref']='not-received-span'
    with pytest.raises(ValueError,match='model_architectural_pool_response_malformed'):
        close_architectural_pool_response(story,candidates,packet,invented,
            source_text_receipt=receipt)
    crossed=copy.deepcopy(good)
    crossed['correspondences'][0]['source_span_ref']=next(ref
        for ref,span in packet['source_span_refs'].items()
        if span['article_id']=='catalog:neighbor-one')
    with pytest.raises(ValueError,match='source_span_ref_wrong_article'):
        close_architectural_pool_response(story,candidates,packet,crossed,
            source_text_receipt=receipt)


def test_verified_architecture_pool_never_silently_drops_excess_articles():
    story,candidates,decision,receipt=_three_documents()
    receipt['articles']=[_article(f'catalog:{i}',f'Building {i} has a visible portal.')
        for i in range(9)]
    with pytest.raises(ValueError,match='bounded_original_SOURCE'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    receipt['articles'][:]=receipt['articles'][:8]
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    assert len(packet['article_ids'])==8
    assert len(packet['checked_articles'])==8
    assert packet['max_article_excerpts']==8


def test_bad_article_hash_or_unobserved_physical_id_stops_before_any_model_call():
    story,candidates,decision,receipt=_three_documents()
    receipt['articles'][0]['text_sha256']='0'*64
    with pytest.raises(ValueError,match='unverified_article'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    receipt['articles'][0]['text_sha256']=hashlib.sha256(receipt['articles'][0]['text'].encode()).hexdigest()
    with pytest.raises(ValueError,match='unobserved_physical_candidate'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:999'])


def test_literal_evidence_refs_are_exact_and_never_semantically_rewritten():
    body='Старинная пристройка. Центральный фасад имеет фигурный эркер. Утрачен декор портала.'
    article=_article('catalog:ref',body)
    result,refs=_source_span_options([article])
    assert result[0]['article_id']=='catalog:ref'
    assert all(body[ref['start']:ref['end']]==ref['source_quote'] for ref in refs.values())
    assert all(ref['source_text_sha256']==article['text_sha256'] for ref in refs.values())
    assert len(result[0]['passages'])>=2
    assert any('Утрачен декор' in span['source_quote'] for span in refs.values())



def test_negative_neighbor_article_quote_is_preserved_not_positive_binding():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    reply=_closed_answer(decision,packet['article_ids'],packet)
    neighbor=next((ref,span) for ref,span in packet['source_span_refs'].items()
        if span['article_id']=='catalog:neighbor-two')
    reply['correspondences'].append({'article_id':'catalog:neighbor-two',
        'source_span_ref':neighbor[0], 'source_observation':'Neighbor has a different roof.',
        'feature_kind':'roof','status':'structural_contradiction',
        'reason':'This rejected article describes the roof of another observed body.'})
    review=close_architectural_pool_response(story,candidates,packet,reply,
        source_text_receipt=receipt)
    assert review['accepted'] is True
    assert review['negative_article_evidence'][0]['article_id']=='catalog:neighbor-two'
    assert all(line['article_id']=='catalog:physical-building'
        for line in review['proof']['decision']['correspondences'])


def test_generic_historical_text_cannot_become_individual_architecture_proof():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    reply=_closed_answer(decision,packet['article_ids'],packet)
    for item in reply['correspondences']:
        item['feature_kind']='historical_fact'
        item['source_observation']='The building is historically important.'
    result=close_architectural_pool_response(story,candidates,packet,reply,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='not_enough_independent_structural_architecture'


def test_unbound_neighbor_stable_match_blocks_premature_accepted():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    reply=_closed_answer(decision,packet['article_ids'],packet)
    ref=next(ref for ref,span in packet['source_span_refs'].items()
        if span['article_id']=='catalog:neighbor-one')
    reply['correspondences'].append({
        'article_id':'catalog:neighbor-one','source_span_ref':ref,
        'source_observation':'Another plausible facade is visible.',
        'feature_kind':'composition','status':'stable_match',
        'reason':'This alternate article has an unresolved visible similarity.'})
    result=close_architectural_pool_response(story,candidates,packet,reply,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='unresolved_stable_match_in_unbound_article'



def test_without_GPS_model_can_nominate_article_but_never_accept_physical_ID():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(
        story,[],receipt,candidate_ids=[],allow_unresolved_physical=True)
    assert packet['candidate_ids']==[]
    assert packet['schema']['properties']['candidate_id']['enum']==['']
    assert 'unresolved_no_GPS_subject' in packet['prompt']
    answer=copy.deepcopy(decision)
    answer.update(decision='uncertain',candidate_id='',
        article_bindings=[],correspondences=[],physical_link_evidence=[],
        material_alternatives=[],material_alternatives_resolved=False,
        article_comparisons=[{
            'article_id':aid,
            'visual_fit':('distinctive_match' if aid=='catalog:physical-building'
                else 'generic_only'),
            'architectural_difference':'SOURCE may depict this architectural style.',
            'visible_SOURCE_specifics':'A central bay is visible.'}
            for aid in packet['article_ids']])
    result=close_architectural_pool_response(story,[],packet,answer,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['physical_OSM_link_still_required'] is True
    assert result['unresolved_article_hypothesis_ids']==[
        'catalog:physical-building']
    with pytest.raises(ValueError,match='model_architectural_pool_response_malformed'):
        answer['decision']='accepted_architectural_text'
        answer['candidate_id']='osm:way:1234567'
        close_architectural_pool_response(story,[],packet,answer,
            source_text_receipt=receipt)



def test_model_physical_binding_cannot_point_to_another_source_or_osm_body():
    story,candidates,decision,receipt=_three_documents()
    other={'candidate_id':'osm:way:8','identity_eligible':True,
        'map_address':{'street':'Observed literal street','house_number':'8'},
        'map_object':{'tags':{'building':'yes'}}}
    candidates.append(other)
    story['_identity_observed_candidates']=candidates
    packet=prepare_architectural_pool(story,candidates,receipt,
        candidate_ids=['osm:way:7','osm:way:8'])
    good=_closed_answer(decision,packet['article_ids'],packet)
    foreign=copy.deepcopy(good)
    refs=packet['literal_evidence_inventory']['osm_refs']
    foreign['physical_link_evidence'][0]['osm_ref']=next(ref
        for ref,row in refs.items() if row['candidate_id']=='osm:way:8')
    result=close_architectural_pool_response(story,candidates,packet,foreign,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='osm_ref_does_not_belong_to_nominated_physical_body'
    foreign2=copy.deepcopy(good)
    foreign2['physical_link_evidence'][0]['publisher_ref']=next(ref
        for ref,row in packet['literal_evidence_inventory']['publisher_refs'].items()
        if row['article_id']=='catalog:neighbor-one')
    result=close_architectural_pool_response(story,candidates,packet,foreign2,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='publisher_ref_does_not_point_to_bound_acquired_article'


def test_model_complex_only_scope_never_promotes_a_physical_building():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,
        candidate_ids=['osm:way:7'])
    claim=_closed_answer(decision,packet['article_ids'],packet)
    claim['physical_link_evidence'][0].update(
        relationship='documented_complex_component',
        subject_scope='historical_complex_only',
        architectural_scope_explanation='This text describes several wings, not a unique photographed corpus.')
    result=close_architectural_pool_response(story,candidates,packet,claim,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='llm_did_not_resolve_individual_physical_body'


def test_no_host_address_parser_can_preselect_a_physical_candidate():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,
        candidate_ids=['osm:way:7'])
    inventory=packet['literal_evidence_inventory']
    assert inventory['host_address_parser_used'] is False
    assert all('raw' not in key for key in inventory['osm_refs'])
    # A publisher postal range is still shown as the raw evidence string,
    # and no parser emits "exact" or "complex" joins ahead of the model.
    receipt['articles'][0]['address']='Observed literal street 7–9'
    receipt['articles'][0]['address_provenance']='publisher_article_metadata_table'
    again=prepare_architectural_pool(story,candidates,receipt,
        candidate_ids=['osm:way:7'])
    addresses=[row['literal_value'] for row in
        again['literal_evidence_inventory']['publisher_refs'].values()]
    assert 'Observed literal street 7–9' in addresses
    assert 'publisher_postal_matches_not_identity' not in again['prompt']
