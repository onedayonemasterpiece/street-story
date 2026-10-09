"""T uses actual G-v3 handoff shape; SOURCE+TEXT model owns narrowing/REF.

The semantic model fixtures are synthetic contracts, not measured recognition.
No postal parsing, distance threshold, forced crop, or proximity veto.
"""
import copy

import pytest
from jsonschema import Draft202012Validator

from street_story.identity_architectural_funnel import (
    close_t_g_funnel, prepare_t_g_funnel, t_g_funnel_schema,
    project_independent_T_nomination, to_existing_research_priority)
from street_story.identity_architectural_pool import (
    close_architectural_pool_response, prepare_architectural_pool)
from street_story.identity_architectural_comparison import (
    prepare_architectural_comparison,combine_architectural_decision)
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
        'conditions':result['explicitly_contradicted'][0]['contradiction_conditions'],
        'source_article_ids':['catalog:physical-building']}]
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
    assert bad['status']=='conditional_T_shortlist'
    assert bad['after_T_active_count']==1
    assert bad['after_T_reserved_count']==3
    assert bad['T_proof_accepted'] is False
    assert bad['accepted_physical_id'] is None
    assert bad['conditional_T_research_priority_not_identity'] is True
    assert bad['reserve_physical_candidate_ids']==['osm:way:9','osm:way:10','osm:way:8']
    assert bad['t_explicit_contradictions'][0]['review_state']=='model_contradiction_not_authorized'
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



def test_unaccepted_T_reuses_actual_G_model_question_as_targeted_ref_hint():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    claim=_t_result(effect='confirmed')
    claim['targeted_images']=[]
    claim['next_distinguishing_question']='None required.'
    result=close_t_g_funnel(prepared,claim,
        source_sha256=receipt['original_source_sha256'])
    assert result['status']=='conditional_T_shortlist'
    assert result['after_T_active_count']==1
    assert result['after_T_reserved_count']==3
    assert result['accepted_physical_id'] is None
    assert result['T_proof_accepted'] is False
    assert result['downstream_REF']['image_goal_provenance']=='G_previously_closed_model_distinguishing_question'
    assert result['downstream_REF']['image_research_goals'][0][
        'needed_view_or_feature']=='Which building owns the central bay?'
    assert result['downstream_REF']['image_research_goals'][0][
        'target_candidate_ids']==['osm:way:7']
    assert result['identity_authorized_by_shortlist_count_alone'] is False



def test_unaccepted_T_uses_its_own_model_authored_ref_question_if_G_had_none():
    story,candidates,decision,receipt,g=_inputs()
    g['t_distinguishing_question']=None
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    claim=_t_result(effect='confirmed')
    claim['targeted_images']=[]
    claim['next_distinguishing_question']='Are courtyard portals needed to distinguish both wings?'
    result=close_t_g_funnel(prepared,claim,
        source_sha256=receipt['original_source_sha256'])
    assert result['status']=='conditional_T_shortlist'
    assert result['T_proof_accepted'] is False
    assert result['after_T_active_count']==1
    assert result['downstream_REF']['image_goal_provenance']==(
        'T_same_closed_response_distinguishing_question')
    assert result['downstream_REF']['image_research_goals'][0][
        'needed_view_or_feature']=='Are courtyard portals needed to distinguish both wings?'
    assert result['reserve_physical_candidate_ids']==[
        'osm:way:9','osm:way:10','osm:way:8']



def test_already_read_publisher_image_urls_flow_to_targeted_ref_without_refetch():
    story,candidates,decision,receipt,g=_inputs()
    actual_image='https://www.prussia39.ru/files/facade_123.jpg'
    source_article=next(item for item in receipt['articles']
        if item['article_id']=='catalog:physical-building')
    source_article['source_image_links']=[actual_image]
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    assert prepared['existing_image_links']==[{
        'article_id':'catalog:physical-building',
        'source_sha256':source_article['source_sha256'],
        'image_url':actual_image,'image_fetched_and_compared':False}]
    result=close_t_g_funnel(prepared,_t_result(),
        source_sha256=receipt['original_source_sha256'])
    assert result['downstream_REF']['already_acquired_source_image_links']==(
        prepared['existing_image_links'])
    assert result['T_proof_accepted'] is False



def test_independent_SOURCE_T_model_nomination_prioritizes_without_G_or_POI_proof():
    story,candidates,decision,receipt,g=_inputs()
    original_ids=[x['candidate_id'] for x in candidates]
    checked=[{**x,'source_image_links':['https://www.prussia39.ru/source/photo.jpg']}
        for x in receipt['articles'] if x['article_id']=='catalog:physical-building']
    model={**decision,'material_alternatives':[{
        'candidate_id':'osm:way:8','reason':'Neighbor has alternative portal'}]}
    packet={'candidate_ids':original_ids,'checked_articles':checked}
    result=project_independent_T_nomination(packet,model,
        source_sha256=receipt['original_source_sha256'])
    assert result['status']=='conditional_T_shortlist'
    assert result['T_proof_accepted'] is False
    assert result['accepted_physical_id'] is None
    assert result['after_T_active_count']==1
    assert result['after_T_reserved_count']==3
    assert result['reserve_physical_candidate_ids']==[
        'osm:way:8','osm:way:9','osm:way:10']
    assert result['active_physical_candidate_ids']==['osm:way:7']
    assert result['downstream_REF']['target_candidate_ids']==[
        'osm:way:7','osm:way:8']
    assert result['downstream_REF']['image_research_goals'][0][
        'needed_view_or_feature']==decision['discriminating_combination']
    assert result['downstream_REF']['already_acquired_source_image_links'][0][
        'image_url']=='https://www.prussia39.ru/source/photo.jpg'
    assert result['identity_authorized_by_shortlist_count_alone'] is False


def test_independent_T_unknown_or_only_one_body_never_creates_new_authority():
    story,candidates,decision,receipt,g=_inputs()
    ids=[x['candidate_id'] for x in candidates]
    packet={'candidate_ids':ids,'checked_articles':receipt['articles']}
    bad=copy.deepcopy(decision)
    bad['decision']='uncertain'
    assert project_independent_T_nomination(packet,bad,
        source_sha256=receipt['original_source_sha256']) is None
    bad=copy.deepcopy(decision)
    bad['candidate_id']='osm:way:999'
    assert project_independent_T_nomination(packet,bad,
        source_sha256=receipt['original_source_sha256']) is None
    packet['candidate_ids']=ids[:1]
    assert project_independent_T_nomination(packet,decision,
        source_sha256=receipt['original_source_sha256']) is None



def test_T_projects_into_existing_Codex_research_priority_not_second_planner():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    result=close_t_g_funnel(prepared,_t_result(),
        source_sha256=receipt['original_source_sha256'])
    native=to_existing_research_priority(result,receipt['articles'])
    assert native['candidate_ids']==['osm:way:7']
    assert native['next_step']=='targeted_search'
    assert native['next_question'].startswith('Which photographed body')
    assert native['contradictions']==[{
        'candidate_id':'osm:way:8',
        'reason':'Visible second body has a different portal pattern',
        'conditions':'Assumes SOURCE shows full lower portal',
        'scope':'facade','source_url':'https://archive.example/physical-building'}]
    assert set(native)=={'candidate_ids','reason','next_question','next_step',
        'contradictions'}


def test_unaccepted_T_does_not_export_conditional_negative_as_global_blacklist():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    provisional=close_t_g_funnel(prepared,_t_result(effect='confirmed'),
        source_sha256=receipt['original_source_sha256'])
    native=to_existing_research_priority(provisional,receipt['articles'])
    assert native['candidate_ids']==['osm:way:7']
    assert native['contradictions']==[]
    assert provisional['conditional_T_research_priority_not_identity'] is True


def test_uncited_T_model_difference_is_not_exported_as_architecture_refutation():
    story,candidates,decision,receipt,g=_inputs()
    prepared=prepare_t_g_funnel(g,candidates,receipt['articles'],
        source_sha256=receipt['original_source_sha256'],
        g_source_sha256=receipt['original_source_sha256'])
    model=_t_result()
    model['explicitly_contradicted'][0]['article_ids']=[]
    narrowed=close_t_g_funnel(prepared,model,
        source_sha256=receipt['original_source_sha256'])
    assert narrowed['t_explicit_contradictions'][0]['review_state']==(
        'uncited_model_difference_not_refutation')
    assert to_existing_research_priority(narrowed,receipt['articles'])['contradictions']==[]
    assert 'osm:way:8' in narrowed['reserve_physical_candidate_ids']


def test_G_confirmed_without_text_and_G_nohelp_with_text_do_not_block_normal_product():
    story,candidates,decision,receipt,g=_inputs()
    g.update(status='accepted_identity_proposal',accepted=True,accepted_id='osm:way:7')
    no_articles={'original_source_sha256':receipt['original_source_sha256'],
        'articles':[]}
    accepted=prepare_architectural_pool(story,candidates,no_articles,g_funnel=g,
        g_source_sha256=receipt['original_source_sha256'])
    assert accepted['skip_T'] is True
    assert accepted['g_handoff']['stage']=='already_accepted_G'
    g.update(status='no_useful_reduction',accepted=False,active=[],
        reserve=[{'candidate_id':cid} for cid in
            ('osm:way:7','osm:way:8','osm:way:9','osm:way:10')])
    independent=prepare_architectural_pool(story,candidates,receipt,
        g_funnel=g,candidate_ids=['osm:way:7','osm:way:8'],
        g_source_sha256=receipt['original_source_sha256'])
    assert independent.get('skip_T') is not True
    assert independent['candidate_ids']==['osm:way:7','osm:way:8']
    assert independent['t_g_funnel_context'] is None


def test_no_actual_architectural_text_yields_reversible_G_partial_value_and_image_route():
    story,candidates,decision,receipt,g=_inputs()
    empty={'original_source_sha256':receipt['original_source_sha256'],
        'articles':[]}
    result=prepare_architectural_pool(story,candidates,empty,g_funnel=g,
        g_source_sha256=receipt['original_source_sha256'])
    assert result['skip_T'] is True
    assert result['reason']=='no_acquired_architectural_text'
    assert result['g_handoff']['original_active_ids']==['osm:way:7','osm:way:8']
    assert result['next_step']=='existing_images_or_expanded_publisher_search'



def test_same_existing_compact_T_provider_call_accepts_G_and_emits_native_priority():
    story,candidates,decision,receipt,g=_inputs()
    # Existing Codex #246 compact model normally receives 1-2 verified
    # bodies, not the 8-article T research supplier.
    receipt['articles']=[receipt['articles'][0],receipt['articles'][-1]]
    prepared=prepare_architectural_comparison(story,candidates,receipt,
        require_grounded_refs=True,
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'])
    assert prepared['candidate_ids']==['osm:way:7','osm:way:8']
    assert 't_funnel' in prepared['schema']['required']
    assert prepared['g_funnel_prepared']['original_reserve_ids']==[
        'osm:way:9','osm:way:10']
    assert 'G_to_T_active_shortlist_not_ground_truth' in prepared['prompt']
    answer=copy.deepcopy(decision)
    answer.update(decision='uncertain',candidate_id='',
        article_bindings=[],correspondences=[],material_alternatives=[],
        material_alternatives_resolved=False,physical_link_evidence=[],
        t_funnel=_t_result())
    closed=combine_architectural_decision({},answer,prepared['schema'],
        literal_evidence_inventory=prepared['literal_evidence_inventory'],
        g_funnel_prepared=prepared['g_funnel_prepared'],
        source_sha256=receipt['original_source_sha256'],
        source_articles=receipt['articles'])
    assert closed['accepted_architectural_text']['decision']=='uncertain'
    assert 't_funnel' not in closed['accepted_architectural_text']
    assert closed['research_priority']['candidate_ids']==['osm:way:7']
    assert closed['research_priority']['next_question'].startswith(
        'Which photographed body')
    assert closed['T_shortlist_and_REF_plan']['T_proof_accepted'] is False
    assert closed['T_shortlist_and_REF_plan']['reserve_physical_candidate_ids']==[
        'osm:way:9','osm:way:10','osm:way:8']
    assert closed['research_priority']['contradictions'][0]['scope']=='facade'


def test_legacy_compact_G_no_reduction_continues_T_instead_of_blocking():
    story,candidates,decision,receipt,g=_inputs()
    receipt['articles']=[receipt['articles'][0],receipt['articles'][-1]]
    g.update(status='no_useful_reduction',active=[],reserve=[
        {'candidate_id':cid} for cid in (
            'osm:way:7','osm:way:8','osm:way:9','osm:way:10')])
    # The existing acquired article has no model-nominated body. Explicit
    # previous model lead provides a normal T research scope, not G's veto.
    receipt['articles'][1]['lookup_candidate_ids']=['osm:way:7']
    packet=prepare_architectural_comparison(story,candidates,receipt,
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'])
    assert packet.get('skip_T') is not True
    assert packet['candidate_ids']==['osm:way:7']
    assert packet['g_funnel_prepared'] is None
    assert 't_funnel' not in packet['schema']['properties']


def test_compact_G_independently_accepted_never_requires_text():
    story,candidates,decision,receipt,g=_inputs()
    g.update(status='accepted_identity_proposal',accepted=True,accepted_id='osm:way:7')
    packet=prepare_architectural_comparison(story,candidates,{
        'original_source_sha256':receipt['original_source_sha256'],'articles':[]},
        g_funnel=g,g_source_sha256=receipt['original_source_sha256'])
    assert packet['skip_T'] is True
    assert packet['g_funnel_prepared']['stage']=='already_accepted_G'
