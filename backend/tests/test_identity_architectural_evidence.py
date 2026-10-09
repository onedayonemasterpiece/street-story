"""The host checks evidence provenance; the LLM interprets architecture/addresses.

No house-number tokenization or corpus-specific street spelling is required.
"""
import copy

import pytest
from jsonschema import Draft202012Validator

from street_story.identity_architectural_evidence import (
    literal_evidence_inventory, physical_link_schema, validate_model_physical_links)


def _scene():
    main={'candidate_id':'osm:way:7','identity_eligible':True,
        'map_address':{'city':'Musterstadt','street':'Schloßstraße',
            'house_number':'6-Б / строение II'},
        'map_object':{'tags':{'building':'yes','name':'West wing',
            'old_addr:street':'Viktoriaallee'}}}
    other={'candidate_id':'osm:way:8','identity_eligible':True,
        'map_address':{'city':'Musterstadt','street':'Schloßstraße',
            'house_number':'6'},
        'map_object':{'tags':{'building':'yes','name':'East wing'}}}
    story={'_identity_observed_candidates':[main,other]}
    article={'article_id':'prussia39:sid:11',
        'url':'https://www.prussia39.ru/sight/index.php?sid=11',
        'input_kind':'acquired_article_text',
        'raw_body_sha256_verified':True,
        'source_sha256':'a'*64,
        'text':'The west wing has three pointed arches.',
        'address':'Музейный проезд, корп. Б (бывш. Viktoriaallee 6-8)',
        'address_provenance':'publisher_article_metadata_table',
        'card_variants':[{
            'article_id':'prussia39:sid:11',
            'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=11',
            'address_text':'Schloßstraße 6 Б / Alte Nr. 8'}]}
    return story,[main,other],[article]


def _linked(inventory):
    pub=next(iter(inventory['publisher_refs']))
    osm=next(k for k,v in inventory['osm_refs'].items()
        if v['candidate_id']=='osm:way:7')
    return {'article_id':'prussia39:sid:11','candidate_id':'osm:way:7',
        'publisher_ref':pub,'osm_ref':osm,
        'relationship':'historical_address_relation',
        'subject_scope':'specific_photographed_OSM_body',
        'architectural_scope_explanation':
            'The SOURCE-specific three-arch west wing is the photographed physical body.',
        'postal_interpretation':
            'LLM links the observed old and current literal descriptions.'}


def test_unusual_orthographies_pass_unmodified_to_model_no_postal_regex():
    story,candidates,articles=_scene()
    inventory=literal_evidence_inventory(story,candidates,articles,
        candidate_ids=['osm:way:7','osm:way:8'])
    assert inventory['host_address_parser_used'] is False
    all_values=[r['literal_value'] for r in inventory['publisher_refs'].values()]
    assert articles[0]['address'] in all_values
    assert articles[0]['card_variants'][0]['address_text'] in all_values
    own=[r['literal_value'] for r in inventory['osm_refs'].values()
         if r['kind']=='observed_OSM_postal_entry']
    assert any(r['house_number']=='6-Б / строение II' for r in own)
    assert any(r['house_number']=='6' for r in own)
    assert all('postal_match' not in r for r in inventory['physical_subjects'])


def test_llm_chooses_scope_with_real_article_and_observed_osm_pointers():
    story,candidates,articles=_scene()
    inventory=literal_evidence_inventory(story,candidates,articles,
        candidate_ids=['osm:way:7','osm:way:8'])
    claim=_linked(inventory)
    schema=physical_link_schema(['prussia39:sid:11'],
        ['osm:way:7','osm:way:8'],
        inventory['publisher_refs'],inventory['osm_refs'])
    assert Draft202012Validator(schema).is_valid([claim])
    decision={'decision':'accepted_architectural_text',
        'candidate_id':'osm:way:7',
        'article_bindings':[{'article_id':'prussia39:sid:11'}]}
    closure=validate_model_physical_links(inventory,[claim],decision)
    assert closure['supported'] and closure['applicable']
    assert closure['host_address_parser_used'] is False
    assert closure['verified_bindings'][0]['publisher_ref']==claim['publisher_ref']
    assert closure['verified_bindings'][0]['osm_ref']==claim['osm_ref']


@pytest.mark.parametrize('tamper,expected',[
    ('other_body','osm_ref_does_not_belong_to_nominated_physical_body'),
    ('other_article','publisher_ref_does_not_point_to_bound_acquired_article'),
    ('complex_scope','llm_did_not_resolve_individual_physical_body'),
    ('unresolved','llm_did_not_resolve_individual_physical_body'),
])
def test_wrong_reference_or_unresolved_corpus_cannot_be_promoted(tamper,expected):
    story,candidates,articles=_scene()
    another={**articles[0], 'article_id':'prussia39:sid:12',
        'url':'https://www.prussia39.ru/sight/index.php?sid=12',
        'card_variants':[]}
    inv=literal_evidence_inventory(story,candidates,[*articles,another],
        candidate_ids=['osm:way:7','osm:way:8'])
    claim=_linked(inv)
    if tamper=='other_body':
        claim['osm_ref']=next(k for k,v in inv['osm_refs'].items()
            if v['candidate_id']=='osm:way:8')
    elif tamper=='other_article':
        claim['publisher_ref']=next(k for k,v in inv['publisher_refs'].items()
            if v['article_id']=='prussia39:sid:12')
    elif tamper=='complex_scope':
        claim['relationship']='documented_complex_component'
        claim['subject_scope']='historical_complex_only'
    else:
        claim['relationship']='ambiguous_or_insufficient'
        claim['subject_scope']='unresolved'
    decision={'decision':'accepted_architectural_text',
        'candidate_id':'osm:way:7',
        'article_bindings':[{'article_id':'prussia39:sid:11'}]}
    proof=validate_model_physical_links(inv,[claim],decision)
    assert proof['supported'] is False
    assert proof['reason']==expected


def test_unobserved_candidate_and_unverified_article_rejected_before_model():
    story,candidates,articles=_scene()
    with pytest.raises(ValueError,match='unobserved_physical_subject'):
        literal_evidence_inventory(story,candidates,articles,candidate_ids=['osm:way:404'])
    corrupted=copy.deepcopy(articles)
    corrupted[0]['raw_body_sha256_verified']=False
    with pytest.raises(ValueError,match='unverified_article'):
        literal_evidence_inventory(story,candidates,corrupted,candidate_ids=['osm:way:7'])
