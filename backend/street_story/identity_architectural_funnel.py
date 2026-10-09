"""G -> SOURCE+TEXT T -> targeted existing-image / REF research handoff.

This is one optional extension of the existing T visual model's response,
NOT a new model, controller, confidence score, deterministic building classifier,
geometric veto or independent POI acceptance path.

A genuinely CLOSED G funnel provides active physical body IDs and a reversible
reserve. The T model inspects original SOURCE pixels and fetched architectural
article bodies, then explicitly narrows those active bodies and explains which
exact view could separate those remaining. Host validates only immutable IDs,
receipt provenance, response schema and lossless accounting. The model, not
Python, decides which facade descriptions match, contradict or remain unseen.
"""
from __future__ import annotations

import copy
import hashlib
import json

from jsonschema import Draft202012Validator

_CONTRACT = 'street-story-T-G-shortlist-and-REF-handoff-v1'
_SHA = set('0123456789abcdef')


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA


def _physical_ids(rows):
    result = []
    for item in rows:
        if (not isinstance(item, dict)
                or not isinstance(item.get('candidate_id'), str)
                or not item['candidate_id'].startswith(('osm:way:', 'osm:relation:'))):
            raise ValueError('unobserved_g_physical_candidate')
        cid = item['candidate_id']
        if cid in result:
            raise ValueError('duplicate_g_physical_candidate')
        result.append(cid)
    return result


def prepare_t_g_funnel(g_result, observed_candidates, source_articles, *,
        source_sha256, g_source_sha256):
    """Prepare a compact G-backed T context without promoting G as truth.

    The original G output is immutable and already SOURCE+OSM hash-verified by
    project_g_funnel. Caller also supplies the SOURCE SHA from G's original
    request receipt so no story/camera scene can be accidentally crossed.
    Original article bodies and model image stay on their existing T route.
    """
    if (not isinstance(g_result, dict)
            or g_result.get('source_and_map_bound') is not True
            or not _sha(g_result.get('model_answer_sha256'))
            or not _sha(source_sha256)
            or g_source_sha256 != source_sha256):
        raise ValueError('G_and_T_original_SOURCE_receipts_do_not_match')
    status = g_result.get('status')
    if status not in {'active_shortlist','no_useful_reduction','accepted_identity_proposal'}:
        raise ValueError('G_funnel_has_no_valid_shortlist_status')
    if not isinstance(observed_candidates, list):
        raise ValueError('actual_received_OSM_candidates_required')
    catalog = {item.get('candidate_id'):item for item in observed_candidates
        if isinstance(item, dict) and isinstance(item.get('candidate_id'), str)}
    active = _physical_ids(g_result.get('active') or [])
    reserve = _physical_ids(g_result.get('reserve') or [])
    if (set(active) & set(reserve)
            or len(active)+len(reserve) != g_result.get('initial_body_count')
            or any(cid not in catalog for cid in [*active,*reserve])
            or (status == 'active_shortlist' and not active)
            or (status == 'no_useful_reduction' and active)
            or not isinstance(g_result.get('initial_body_count'), int)):
        raise ValueError('G_shortlist_does_not_cover_actual_received_map')
    # G has already accepted a physical candidate with its own proof; T and
    # external REF are never mandatory barriers for that independent result.
    if status == 'accepted_identity_proposal' and g_result.get('accepted') is True:
        return {'contract':_CONTRACT,'stage':'already_accepted_G',
            'g_accepted_physical_id':g_result.get('accepted_id'),
            'source_sha256':source_sha256,'g_model_answer_sha256':g_result['model_answer_sha256']}
    if status == 'no_useful_reduction':
        # No invented candidate shortlist. The caller may widen SOURCE+T
        # using its original received candidates as a separate model choice.
        return {'contract':_CONTRACT,'stage':'G_no_useful_reduction',
            'active_candidate_ids':[], 'reserve_candidate_ids':reserve,
            'source_sha256':source_sha256,'g_model_answer_sha256':g_result['model_answer_sha256'],
            'expandable':True}
    if not isinstance(source_articles,list):
        raise ValueError('actual_acquired_source_articles_required')
    aid = []
    images = []
    for article in source_articles:
        if (not isinstance(article,dict)
                or article.get('input_kind')!='acquired_article_text'
                or article.get('raw_body_sha256_verified') is not True
                or not isinstance(article.get('article_id'),str)
                or not _sha(article.get('source_sha256'))
                or not isinstance(article.get('url'),str)):
            raise ValueError('unverified_T_source_article_in_funnel')
        if article['article_id'] in aid:
            raise ValueError('duplicate_T_article')
        aid.append(article['article_id'])
        # Links were captured from *this article's actually fetched HTML*
        # (or an actually acquired encyclopedia page). Never fabricate a view.
        for value in article.get('source_image_links') or []:
            if isinstance(value,str) and value.startswith('https://'):
                images.append({'article_id':article['article_id'],
                    'source_sha256':article['source_sha256'],
                    'image_url':value,
                    'image_fetched_and_compared':False})
    model_active = []
    for item in g_result['active']:
        row = catalog[item['candidate_id']]
        model_active.append({'candidate_id':item['candidate_id'],
            'G_source_match_hypothesis_not_fact':item.get('source_match') or '',
            'G_unresolved_difference':item.get('unresolved_difference') or '',
            'observed_osm_source_link_hints_not_image_proof':copy.deepcopy(
                item.get('observed_source_links') or {}),
            'literal_observed_address':copy.deepcopy(
                item.get('literal_address') or {}),
            'actual_observed_map_identity':row['candidate_id']})
    context = {'contract':_CONTRACT,'stage':'T_while_G_shortlist_unconfirmed',
        'original_source_sha256':source_sha256,
        'G_model_answer_sha256':g_result['model_answer_sha256'],
        'G_source_and_map_checked':True,
        'G_active_physical_body_hypotheses_not_ground_truth':model_active,
        'G_model_source_pattern_not_ground_truth':g_result.get('model_scene_pattern'),
        'G_next_distinguishing_question':g_result.get('t_distinguishing_question'),
        'G_model_uncertainties':g_result.get('model_uncertainties') or [],
        'G_initial_received_physical_body_count':g_result['initial_body_count'],
        'G_active_count':len(active),
        'G_reserve_count':len(reserve),
        'G_reserve_remains_accessible':True,
        'received_article_ids':aid,
        'already_acquired_source_image_links':images,
        'original_active_ids':active}
    # Model need not read 300 raw reserve details again: it sees a complete
    # active group; reserved IDs stay in host evidence for later expansion.
    # No threshold on camera distance, dimensions or architectural name.
    return {'contract':_CONTRACT,'stage':'T_while_G_shortlist_unconfirmed',
        'source_sha256':source_sha256,'g_model_answer_sha256':g_result['model_answer_sha256'],
        'original_active_ids':active,'original_reserve_ids':reserve,
        'original_active_count':len(active),'original_map_count':g_result['initial_body_count'],
        'source_article_ids':aid,'existing_image_links':images,
        'model_input':context,
        'model_context_sha256':hashlib.sha256(json.dumps(
            context,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}


def t_g_funnel_schema(prepared):
    """Shape for *the same* T visual+text model call; no follow-up reviewer."""
    if (not isinstance(prepared,dict) or prepared.get('contract')!=_CONTRACT
            or prepared.get('stage')!='T_while_G_shortlist_unconfirmed'):
        raise ValueError('unprepared_G_T_funnel')
    ids=prepared['original_active_ids']
    article_ids=prepared['source_article_ids']
    txt={'type':'string','maxLength':440}
    cid={'type':'string','enum':ids}
    def array_of(kind):
        return {'type':'array','maxItems':len(ids),'items':kind}
    return {'type':'object','properties':{
        'effect':{'type':'string','enum':['confirmed','narrowed','no_useful_text']},
        'retained_active_candidate_ids':{
            **array_of(cid),'uniqueItems':True},
        'explicitly_contradicted':array_of({'type':'object','properties':{
            'candidate_id':cid,'source_vs_article_reason':txt,
            'contradiction_conditions':txt,
            'article_ids':{'type':'array','maxItems':len(article_ids),
                'uniqueItems':True,'items':{'type':'string','enum':article_ids}}},
            'required':['candidate_id','source_vs_article_reason',
                'contradiction_conditions','article_ids'],'additionalProperties':False}),
        'supporting_observations':array_of({'type':'object','properties':{
            'candidate_id':cid,'source_visible_features':txt,
            'matching_or_missing_article_details':txt,
            'supporting_article_ids':{'type':'array','maxItems':len(article_ids),
                'uniqueItems':True,'items':{'type':'string','enum':article_ids}},
            'remaining_uncertainty':txt},
            'required':['candidate_id','source_visible_features',
                'matching_or_missing_article_details','supporting_article_ids',
                'remaining_uncertainty'],'additionalProperties':False}),
        'new_map_evidence_needed':{'type':'boolean'},
        'reserve_expansion_reason':txt,
        'next_distinguishing_question':txt,
        'targeted_images':{'type':'array','maxItems':5,'items':{
            'type':'object','properties':{
                'target_candidate_ids':{'type':'array','minItems':1,
                    'maxItems':len(ids),'uniqueItems':True,'items':cid},
                'needed_view_or_feature':txt,
                'how_this_image_would_distinguish_bodies':txt,
                'reuse_actual_article_ids':{'type':'array','maxItems':len(article_ids),
                    'uniqueItems':True,'items':{'type':'string','enum':article_ids}}},
            'required':['target_candidate_ids','needed_view_or_feature',
                'how_this_image_would_distinguish_bodies','reuse_actual_article_ids'],
            'additionalProperties':False}}},
        'required':['effect','retained_active_candidate_ids','explicitly_contradicted',
            'supporting_observations','new_map_evidence_needed','reserve_expansion_reason',
            'next_distinguishing_question','targeted_images'],
        'additionalProperties':False}


def close_t_g_funnel(prepared, model_result, *, source_sha256, t_accepted=False,
        accepted_candidate_id=None):
    """Validate actual model choices, account for every previously active ID.

    An unchosen physical body is DEFERRED, not contradicted or blacklisted.
    No successful T outcome follows from merely one remaining body. No
    model-authored "confirmed" becomes accepted without the standard SOURCE+
    text host proof, independently available from the same model response.
    """
    if (not isinstance(prepared,dict)
            or prepared.get('stage')!='T_while_G_shortlist_unconfirmed'
            or prepared.get('source_sha256')!=source_sha256
            or not Draft202012Validator(t_g_funnel_schema(prepared)).is_valid(model_result)):
        raise ValueError('T_model_shortlist_schema_or_source_invalid')
    before=prepared['original_active_ids']
    before_set=set(before)
    retained=model_result['retained_active_candidate_ids']
    explicit=model_result['explicitly_contradicted']
    contradicted=[row['candidate_id'] for row in explicit]
    if (len(contradicted)!=len(set(contradicted)) or
            set(retained)&set(contradicted) or
            any(not row['source_vs_article_reason'].strip() or
                    not row['contradiction_conditions'].strip() for row in explicit) or
            any(item['candidate_id'] not in retained for item in
                model_result['supporting_observations'])):
        raise ValueError('T_model_shortlist_contains_conflicting_refs')
    model_effect=model_result['effect']
    if model_effect=='narrowed' and not (0<len(retained)<len(before)):
        raise ValueError('T_model_claimed_shortlist_without_reduction')
    if model_effect=='no_useful_text' and (
            retained and set(retained)!=before_set or explicit):
        raise ValueError('T_model_no_help_response_must_not_reject_any_body')
    if model_effect=='confirmed' and (
            not t_accepted or accepted_candidate_id not in before_set
            or accepted_candidate_id not in retained):
        # The caller's existing freeze/host checks, not the model claim,
        # determine independent T acceptance.
        final='unconfirmed_model_claim'
    else:
        final=('accepted_T_identity' if model_effect=='confirmed' else
            'active_shortlist' if model_effect=='narrowed' else 'no_useful_text')
    effective = retained if model_effect=='narrowed' else (
        [accepted_candidate_id] if final=='accepted_T_identity' else before)
    deferred=[cid for cid in before if cid not in effective and cid not in contradicted]
    reserved=list(dict.fromkeys([*prepared['original_reserve_ids'],
        *contradicted,*deferred]))
    if final=='unconfirmed_model_claim':
        # The original semantic claim did NOT survive the actual host SOURCE
        # proof. Preserve every G body for expansion, and do not turn the
        # rejected model's confident text into a permanent contradiction.
        effective=before
        reserved=prepared['original_reserve_ids']
        deferred=[]
    is_reduced=(len(effective)<len(before) and final=='active_shortlist')
    pending=[{'candidate_id':cid,'review_state':'model_explicit_contradiction',
        'reason':next(row['source_vs_article_reason'] for row in explicit
            if row['candidate_id']==cid),
        'conditions':next(row['contradiction_conditions'] for row in explicit
            if row['candidate_id']==cid)}
        for cid in contradicted]
    pending += [{'candidate_id':cid,'review_state':'not_selected_not_refuted'}
        for cid in deferred]
    rejected_unverified=(copy.deepcopy(explicit)
        if final=='unconfirmed_model_claim' else [])
    if final=='unconfirmed_model_claim':
        pending=[]
    expand=(model_result['new_map_evidence_needed']
        or final=='unconfirmed_model_claim')
    explanation=model_result['reserve_expansion_reason']
    if final=='unconfirmed_model_claim':
        explanation=('T model claimed an individual body but its actual '
            'publisher/OSM/structural proof was not admitted; original G '
            'reserve must remain eligible for independent evidence.')
    return {'contract':_CONTRACT,'status':final,'model_effect':model_effect,
        'source_sha256':source_sha256,'G_model_answer_sha256':prepared['g_model_answer_sha256'],
        'T_model_result_sha256':hashlib.sha256(json.dumps(
            model_result,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'G_initial_physical_count':prepared['original_map_count'],
        'before_T_active_count':len(before),'after_T_active_count':len(effective),
        'after_T_reserved_count':len(reserved),
        'active_physical_candidate_ids':list(effective),
        'reserve_physical_candidate_ids':reserved,
        't_explicit_contradictions':pending,
        'unaccepted_model_contradiction_claims':rejected_unverified,
        'T_reduced_active_count':is_reduced,
        'reconsider_reserve_on_new_evidence':True,
        'G_group_not_ground_truth':True,
        'T_proof_accepted':final=='accepted_T_identity',
        'accepted_physical_id':accepted_candidate_id if final=='accepted_T_identity' else None,
        'source_support':copy.deepcopy(model_result['supporting_observations']),
        'next_distinguishing_question':model_result['next_distinguishing_question'],
        'request_reserve_expansion':expand,
        'reserve_expansion_reason':explanation,
        'downstream_REF':{
            'needed_for_T_acceptance':False,
            'image_research_goals':copy.deepcopy(model_result['targeted_images']),
            'already_acquired_source_image_links':copy.deepcopy(prepared['existing_image_links']),
            'target_candidate_ids':list(effective),
            'next_distinguishing_question':model_result['next_distinguishing_question'],
            'independently_available_G_or_REF_can_accept':True},
        'identity_authorized_by_shortlist_count_alone':False}
