"""T uses actual G-v3 handoff shape; SOURCE+TEXT model owns narrowing/REF.

The semantic model fixtures are synthetic contracts, not measured recognition.
No postal parsing, distance threshold, forced crop, or proximity veto.
"""
import copy

import pytest
from jsonschema import Draft202012Validator

from street_story.identity_architectural_funnel import (
    close_t_g_funnel, prepare_t_g_funnel, t_g_funnel_schema)
from street_story.identity_architectural_pool import (
    close_architectural_pool_response, prepare_architectural_pool)
from test_identity_architectural_pool import _closed_answer, _three_documents


def _inputs():
    story,candidates,decision,receipt=_three_documents()
    for n in (8,9,10):
        candidates.append({'candidate_id':f'osm:way:{n}', 'identity_eligible':True,
            'map_object':{'tags':{'building':'yes'}},
            'map_address':{'street':'Observed unrelated name',
                'house_number':str(n)}})
    story['_identity_observed_candidates']=candidates
    g={'status':'active_shortlist','source_and_map_bound':True,
        'model_answer_sha256':'b'*64, 'initial_body_count':4,
        'active_count':2,'reserve_count':2,'accepted':False,
        'active':[{'candidate_id':'osm:way:7',
            'source_match':'main bay visible','unresolved_difference':'left arch cropped',
            'observed_source_links':{'wikipedia':'ru:Observed'},
            'literal_address':{'addr:housenumber':'7'}},
           {'candidate_id':'osm:way:8',
            'source_match':'adjacent portal might fit','unresolved_difference':'gable count unknown'}],
        'reserve':[{'candidate_id':'osm:way:9',
             'review_state':'not_rejected_not_active'},
            {'candidate_id':'osm:way:10',
             'review_state':'explicit_model_contradiction',
             'contradiction':{'source_vs_map_conflict':'G sees other roof',
                 'conditions':'only if same SOURCE crop'}}],
        'model_scene_pattern':'frontage with adjacent facade',
        'model_uncertainties':['camera orientation uncertain'],
        't_distinguishing_question':'Which building owns the central bay?'}
    return story,candidates,decision,receipt,g


def _t_result(*, effect='narrowed', retained=None, contradict=True):
    if retained is None:
        retained=['osm:way:7']
    return {'effect':effect,'retained_active_candidate_ids':retained,
        'explicitly_contradicted':[{
            'candidate_id':'osm:way:8',
            'source_vs_article_reason':'Visible second body has a different portal pattern',
            'contradiction_conditions':'Assumes SOURCE shows full lower portal',
            'article_ids':['catalog:physical-building']
        }] if contradict else [],
        'supporting_observations':[{
            'candidate_id':cid,
            'source_visible_features':'Central bay with adjacent window groups',
            'matching_or_missing_article_details':'The actual article mentions the bay',
            'supporting_article_ids':['catalog:physical-building'],
            'remaining_uncertainty':'Portal below the crop needs a view'} for cid in retained],
        'new_map_evidence_needed':False, 'reserve_expansion_reason':'',
        'next_distinguishing_question':'Which photographed body owns the ground-level portal?',
        'targeted_images':[{'target_candidate_ids':retained,
            'needed_view_or_feature':'Ground-level portal beside the projecting bay',
            'how_this_image_would_distinguish_bodies':'Different portal positions in the two physical bodies',
            'reuse_actual_article_ids':['catalog:physical-building']}]
        if retained else []}


def test_g_to_t_shortlist_keeps_all_original_map_entries_and_observations():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    assert prepared['original_active_ids']==['osm:way:7','osm:way:8']
    assert prepared['original_reserve_ids']==['osm:way:9','osm:way:10']
    assert prepared['original_map_count']==4
    assert prepared['model_input']['G_next_distinguishing_question'].startswith('Which')
    assert prepared['model_input']['G_reserve_remains_accessible'] is True
    assert prepared['model_input']['G_active_physical_body_hypotheses_not_ground_truth'][0][
        'G_source_match_hypothesis_not_fact']=='main bay visible'
    assert 'osm:way:9' not in str(prepared['model_input'])  # held on host


def test_t_reduction_preserves_contradictions_unexamined_and_precise_ref_request():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    result=_t_result()
    assert Draft202012Validator(t_g_funnel_schema(prepared)).is_valid(result)
    closed=close_t_g_funnel(prepared,result,
        source_sha256=receipt['original_source_sha256'])
    assert closed['status']=='active_shortlist' and not closed['T_proof_accepted']
    assert closed['before_T_active_count']==2 and closed['after_T_active_count']==1
    assert closed['after_T_reserved_count']==3
    assert set(closed['reserve_physical_candidate_ids'])=={
        'osm:way:8','osm:way:9','osm:way:10'}
    assert closed['t_explicit_contradictions']==[{
        'candidate_id':'osm:way:8','review_state':'model_explicit_contradiction',
        'reason':result['explicitly_contradicted'][0]['source_vs_article_reason'],
        'conditions':result['explicitly_contradicted'][0]['contradiction_conditions']}]
    assert closed['identity_authorized_by_shortlist_count_alone'] is False
    assert closed['downstream_REF']['target_candidate_ids']==['osm:way:7']
    assert closed['downstream_REF']['image_research_goals'][0][
        'needed_view_or_feature']=='Ground-level portal beside the projecting bay'


def test_no_help_and_unexamined_candidates_never_become_rejected_or_accepted():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    nohelp=_t_result(effect='no_useful_text',retained=[],contradict=False)
    nohelp['supporting_observations']=[]
    answer=close_t_g_funnel(prepared,nohelp,
        source_sha256=receipt['original_source_sha256'])
    assert answer['status']=='no_useful_text'
    assert answer['active_physical_candidate_ids']==['osm:way:7','osm:way:8']
    assert answer['after_T_reserved_count']==2
    assert not answer['T_proof_accepted']
    narrow=_t_result(contradict=False)
    narrowed=close_t_g_funnel(prepared,narrow,
        source_sha256=receipt['original_source_sha256'])
    assert narrowed['t_explicit_contradictions'][0]=={
        'candidate_id':'osm:way:8','review_state':'not_selected_not_refuted'}
    assert 'osm:way:8' in narrowed['reserve_physical_candidate_ids']


def test_model_not_frozen_proof_cannot_approve_an_individual_building():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    proposed=_t_result(effect='confirmed')
    bad=close_t_g_funnel(prepared,proposed,
        source_sha256=receipt['original_source_sha256'],
        t_accepted=False,accepted_candidate_id='osm:way:7')
    assert bad['status']=='unconfirmed_model_claim'
    assert bad['after_T_active_count']==2
    assert bad['request_reserve_expansion'] is True
    assert bad['T_proof_accepted'] is False
    assert bad['reserve_physical_candidate_ids']==['osm:way:9','osm:way:10']
    accepted=close_t_g_funnel(prepared,proposed,
        source_sha256=receipt['original_source_sha256'],
        t_accepted=True,accepted_candidate_id='osm:way:7')
    assert accepted['T_proof_accepted'] is True and accepted['accepted_physical_id']=='osm:way:7'


def test_nonmatching_photo_or_unreceived_body_rejected_before_model():
    story,candidates,decision,receipt,g=_inputs()
    with pytest.raises(ValueError,match='G_and_T_original_SOURCE'):
        prepare_t_g_funnel(g,candidates,receipt['articles'],
            source_sha256=receipt['original_source_sha256'],
            g_source_sha256='f'*64)
    candidate_missing=candidates[:-1]
    with pytest.raises(ValueError,match='G_shortlist_does_not_cover'):
        prepare_t_g_funnel(g,candidate_missing,receipt['articles'],
            source_sha256=receipt['original_source_sha256'],
            g_source_sha256=receipt['original_source_sha256'])
    invalid=copy.deepcopy(g)
    invalid['active'][0]['candidate_id']='osm:way:999'
    with pytest.raises(ValueError,match='G_shortlist_does_not_cover'):
        prepare_t_g_funnel(invalid,candidates,receipt['articles'],
            source_sha256=receipt['original_source_sha256'],
            g_source_sha256=receipt['original_source_sha256'])


def test_real_G_needs_no_reduction_or_independent_accepted_is_not_T_barrier():
    story,candidates,decision,receipt,g=_inputs()
    g['status']='no_useful_reduction'
    g['reserve']=[*g['active'],*g['reserve']]
    g['active']=[]
    nohelp=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    assert nohelp['stage']=='G_no_useful_reduction'
    assert len(nohelp['reserve_candidate_ids'])==4
    g['status']='accepted_identity_proposal'
    g['accepted']=True
    g['accepted_id']='osm:way:7'
    g['active']=g['reserve'][:1]
    g['reserve']=g['reserve'][1:]
    result=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    assert result['stage']=='already_accepted_G'
    assert result['g_accepted_physical_id']=='osm:way:7'


def test_same_actual_SOURCE_text_model_response_yields_shortlist_without_extra_call():
    story,candidates,decision,receipt,g=_inputs()
    packet=prepare_architectural_pool(story,candidates,receipt,
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'])
    assert packet['candidate_ids']==['osm:way:7','osm:way:8']
    assert 't_funnel' in packet['schema']['required']
    assert packet['t_g_funnel_context']['original_reserve_ids']==[
        'osm:way:9','osm:way:10']
    output=_closed_answer(decision,packet['article_ids'],packet)
    output['decision']='uncertain'
    output['candidate_id']=''
    output['article_bindings']=[]
    output['correspondences']=[]
    output['physical_link_evidence']=[]
    output['material_alternatives_resolved']=False
    output['t_funnel']=_t_result()
    final=close_architectural_pool_response(story,candidates,packet,output,
        source_text_receipt=receipt)
    assert final['accepted'] is False
    assert final['T_shortlist_and_REF_plan']['status']=='active_shortlist'
    assert final['T_shortlist_and_REF_plan']['after_T_active_count']==1
    assert final['T_shortlist_and_REF_plan']['after_T_reserved_count']==3
    assert final['T_shortlist_and_REF_plan']['next_distinguishing_question']


def test_single_MODEL_shortlist_body_does_not_promote_identity_via_T():
    story,candidates,decision,receipt,g=_inputs()
    g['active']=g['active'][:1]
    g['reserve'].append({'candidate_id':'osm:way:8'})
    g['active_count']=1
    g['reserve_count']=3
    packet=prepare_architectural_pool(story,candidates,receipt,
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'])
    output=_closed_answer(decision,packet['article_ids'],packet)
    output.update(decision='uncertain',candidate_id='',article_bindings=[],
        correspondences=[],physical_link_evidence=[],
        material_alternatives_resolved=False)
    output['t_funnel']=_t_result(effect='no_useful_text',
        retained=[],contradict=False)
    output['t_funnel']['supporting_observations']=[]
    final=close_architectural_pool_response(story,candidates,packet,output,
        source_text_receipt=receipt)
    assert final['accepted'] is False
    assert final['T_shortlist_and_REF_plan']['after_T_active_count']==1
    assert final['T_shortlist_and_REF_plan']['identity_authorized_by_shortlist_count_alone'] is False



def test_independent_closed_model_nomination_reopens_G_reserve_without_new_inference():
    story,candidates,decision,receipt,g=_inputs()
    prior={'original_source_sha256':receipt['original_source_sha256'],
        'provider_send_state':'response_closed','provider_outcome':'completed',
        'original_model_response_sha256':'d'*64,
        'original_model_response':{'decision':'uncertain',
            'candidate_id':'osm:way:9'},
        'raw_response_byte_verified':False}
    packet=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'],
        independent_closed_T_leads=[prior])
    assert packet['G_original_active_ids']==['osm:way:7','osm:way:8']
    assert packet['original_active_ids']==['osm:way:7','osm:way:8','osm:way:9']
    assert packet['original_reserve_ids']==['osm:way:10']
    assert packet['model_input']['G_active_count']==2
    assert packet['model_input']['combined_active_count']==3
    assert packet['model_input']['independent_T_model_leads_reopened_not_verified'][0][
        'source']=='separate_CLOSED_model_nomination_not_physical_proof'
    assert packet['original_map_count']==4
    prepared=prepare_architectural_pool(story,candidates,receipt,
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'],
        independent_closed_T_leads=[prior])
    assert prepared['candidate_ids']==['osm:way:7','osm:way:8','osm:way:9']
    assert 'osm:way:9' in prepared['prompt']
    assert prepared['t_g_funnel_context']['original_reserve_ids']==['osm:way:10']


def test_forged_or_unclosed_independent_model_lead_cannot_expand_G_active():
    story,candidates,decision,receipt,g=_inputs()
    genuine={'original_source_sha256':receipt['original_source_sha256'],
        'provider_send_state':'response_closed','provider_outcome':'completed',
        'original_model_response_sha256':'d'*64,
        'original_model_response':{'candidate_id':'osm:way:9'}}
    for mutate,expected in [
        ({'provider_send_state':'unknown'},'not_closed'),
        ({'original_source_sha256':'f'*64},'provenance_invalid'),
        ({'original_model_response_sha256':'invalid'},'provenance_invalid'),
        ({'original_model_response':{'candidate_id':'osm:way:999'}},'not_in_original_observed_map')]:
        changed={**genuine,**mutate}
        with pytest.raises(ValueError,match=expected):
            prepare_t_g_funnel(g,candidates,receipt['articles'],
                source_sha256=receipt['original_source_sha256'],
                g_source_sha256=receipt['original_source_sha256'],
                independent_closed_T_leads=[changed])
