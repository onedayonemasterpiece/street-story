"""G v3 active shortlist is model-owned; reserve is reversible, not rejected.

These tests do NOT claim new cold recognition. Frozen closed image responses
can be adapted for retrospective shortlist replay, with that provenance
recorded separately from genuinely new model choices.
"""
import copy

from street_story.identity_spatial_funnel import project_g_funnel
from street_story.identity_spatial_choice import visual_spatial_choice_schema

SOURCE='1'*64
MODEL='2'*64
MAP='3'*64

def packet():
    return {'version':'street_story.g_spatial_options.v3',
        'map_sha256':MAP,
        'all_received_physical_bodies':[
            [301,12,20,None,None,None,None,None,False,True],
            [302,14,21,None,None,None,None,None,False,True],
            [303,68,8,None,None,None,None,None,False,False],
            [304,105,4,None,None,None,None,None,False,False]],
        'private_label_to_osm_id':{
            '301':'osm:way:1','302':'osm:way:2',
            '303':'osm:relation:3','304':'osm:way:4'},
        'options':{'F301.0.2':{'kind':'single_frontage','body_label':301}}}

def entries():
    return [
        {'candidate_id':'osm:way:1','tags':{'addr:street':'Примерная',
            'addr:housenumber':'3','wikidata':'Q1'},
            'building_entrance_node_ids':[22]},
        {'candidate_id':'osm:way:2','tags':{'building':'yes','wikipedia':'ru:Тест'}},
        {'candidate_id':'osm:relation:3','tags':{}},
        {'candidate_id':'osm:way:4','tags':{}},
    ]

def response(decision='shortlist'):
    return {'decision':decision,'candidate_label':301,
        'source_pattern':'street frontage with recessed next physical body',
        'crop_scope':'whole','source_observations':['Wide facade at left'],
        'selected_option_ids':['F301.0.2'],
        'contrasted_alternatives':[],
        'active_hypotheses':[
            {'label':301,'source_match':'main street frontage',
             'what_remains_uncertain':'is corner turret part of this volume?'},
            {'label':302,'source_match':'attached body behind facade',
             'what_remains_uncertain':'which physical outline owns the main frontage?'}],
        'explicit_contradictions':[{'label':303,
            'source_vs_map_conflict':'map plan long across other street',
            'conditions':'given original camera hint and full visible facade'}],
        't_distinguishing_question':'Which physical building owns the main corner bay and return wall?',
        'next_useful_step':'T',
        'request_detail_labels':[],'uncertainties':['Camera accuracy unmeasured']}
def run(r):
    return project_g_funnel(r,packet(),entries(),source_sha256=SOURCE,
        model_source_sha256=MODEL,actual_source_sha256=SOURCE,
        actual_map_sha256=MAP)

def test_dynamic_shortlist_preserves_literal_identifiers_addresses_and_reserve():
    out=run(response())
    assert out['status']=='active_shortlist' and out['accepted'] is False
    assert (out['initial_body_count'],out['active_count'],out['reserve_count'])==(4,2,2)
    assert [i['candidate_id'] for i in out['active']]==['osm:way:1','osm:way:2']
    assert out['active'][0]['literal_address']['addr:housenumber']=='3'
    assert out['active'][0]['observed_source_links']=={'wikidata':'Q1'}
    assert out['active'][0]['observed_entrance_node_ids']==[22]
    assert out['reserve'][0]['candidate_id']=='osm:relation:3'
    assert out['reserve'][0]['review_state']=='explicit_model_contradiction'
    assert out['reserve'][0]['contradiction']['conditions'].startswith('given')
    assert out['reserve'][1]['review_state']=='not_rejected_not_active'
    assert out['downstream_T']['next_distinguishing_question'].startswith('Which')
    assert out['downstream_T']['active_physical_candidates']==out['active']
    assert out['canonical_POI_memory_updated'] is False

def test_one_model_shortlist_body_never_grants_identity_by_count():
    r=response()
    r['active_hypotheses']=r['active_hypotheses'][:1]
    out=run(r)
    assert out['active_count']==1
    assert out['accepted'] is False and out['accepted_id'] is None

def test_model_unknown_or_no_reduction_does_not_discard_anything():
    for decision in ('no_reduction','unknown'):
        r=response(decision)
        r['candidate_label']=0
        r['active_hypotheses']=[]
        r['explicit_contradictions']=[]
        out=run(r)
        assert out['status']=='no_useful_reduction'
        assert out['reserve_count']==4 and out['active_count']==0
        assert len(out['reserve'])==4
        assert out['downstream_T']['expandable_on_new_evidence']
        assert out['accepted'] is False

def test_unknown_label_and_conflicting_claims_fail_closed():
    invalid=response()
    invalid['active_hypotheses'][0]['label']=999999
    assert run(invalid)['status']=='invalid'
    invalid=response()
    invalid['explicit_contradictions'][0]['label']=301
    assert run(invalid)['reason_codes']==['same_building_active_and_explicitly_contradicted']
    invalid=response()
    invalid['active_hypotheses'].append(copy.deepcopy(invalid['active_hypotheses'][0]))
    assert run(invalid)['reason_codes']==['duplicate_active_physical_label']
    invalid=response()
    assert project_g_funnel(invalid,packet(),entries(),source_sha256=SOURCE,
       model_source_sha256=MODEL,actual_source_sha256=SOURCE,
       actual_map_sha256='4'*64)['status']=='invalid'

def test_legacy_closed_model_alternative_is_replayed_not_new_G_success():
    old=response('candidate')
    old.pop('active_hypotheses')
    old.pop('explicit_contradictions')
    old.pop('t_distinguishing_question')
    old['contrasted_alternatives']=[
        {'label':302,'source_vs_map_difference':'adjacent uncertain wing',
         'observed_option_ids':[]}]
    assert not list(__import__('jsonschema').Draft202012Validator(
        visual_spatial_choice_schema()).iter_errors(old))
    out=run(old)
    assert out['status']=='active_shortlist'
    assert out['active_count']==2
    assert out['legacy_closed_response_replay'] is True
    assert out['active_group_chosen_by_model'] is False
    assert not out['accepted']

def test_single_accepted_model_claim_with_no_host_evidence_is_not_acceptance():
    item=response('accept')
    item['active_hypotheses']=item['active_hypotheses'][:1]
    item['request_detail_labels']=[301]
    out=run(item)
    assert out['status']=='active_shortlist'
    assert out['accepted'] is False
