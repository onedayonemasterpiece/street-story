"""Semantic SOURCE choice among frozen OSM options; no model-authored math.

The vision model may select *already calculated* real-plan alternatives.
Validation resolves exact observed options and their shared SOURCE/MAP
binding. This independent G-v3 certificate is NOT silently substituted into
the old v2 identity_proof policy: Codex integrates it with canonical POI rules
only after corpus accuracy and false-positive acceptance are measured.
"""
from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator

POLICY='street_story.g_option_evidence.v3'


def visual_spatial_choice_schema():
    string={'type':'string','maxLength':360}
    return {'type':'object','properties':{
        'decision':{'type':'string','enum':['accept','candidate','needs_detail','unknown']},
        'candidate_label':{'type':'integer'},
        # PHOTO vocabulary is model-owned: a new facade/bridge/roof pattern
        # must not become UNKNOWN because its name was absent from a Python
        # enum. Only stable action decisions and literal OSM references have
        # deterministic validation.
        'source_pattern':{'type':'string','maxLength':140},
        'crop_scope':{'type':'string','maxLength':120},
        'source_observations':{'type':'array','items':string,'maxItems':5},
        'selected_option_ids':{'type':'array','items':{'type':'string','maxLength':32},
            'maxItems':5},
        'contrasted_alternatives':{'type':'array','maxItems':5,'items':{
            'type':'object','properties':{
                'label':{'type':'integer'},
                'source_vs_map_difference':string,
                'observed_option_ids':{'type':'array',
                    'items':{'type':'string','maxLength':32},'maxItems':3}},
            'required':['label','source_vs_map_difference','observed_option_ids']}},
        'request_detail_labels':{'type':'array','items':{'type':'integer'},'maxItems':4},
        'uncertainties':{'type':'array','items':string,'maxItems':5}},
        'required':['decision','candidate_label','source_observations']}


def check_spatial_choice(response, packet, *, source_sha256, model_source_sha256,
                         actual_source_sha256, actual_map_sha256):
    """Validate MEASURED references, not semantic strength by hand-coded rules.

    PHOTO interpretation, distinctive visible features and decision confidence
    belong to the visual model. The host checks only source/map hashes, real
    OSM identity/option pointers and genuine contradictions in its own data.
    A first-pass physical nomination is useful even before map_detail, so it
    requests expansion instead of failing the whole case. No one is forced to
    invent yaw, two visible corners, three observations or a contrast table.

    Accepted G-v3 is an independent candidate certificate for evaluation.
    Codex explicitly approves integration into canonical POI/fact gates after
    measured accuracy; this helper never updates POI or story identity itself.
    """
    invalid = {'status':'invalid', 'candidate_id':None, 'accepted':False,
        'proof':None, 'reason_codes':['invalid_spatial_option_response']}
    if not isinstance(response,dict) or not isinstance(packet,dict):
        return invalid
    if not Draft202012Validator(visual_spatial_choice_schema()).is_valid(response):
        return invalid
    if (packet.get('version')!='street_story.g_spatial_options.v3'
            or packet.get('map_sha256')!=actual_map_sha256
            or not isinstance(source_sha256,str) or len(source_sha256)!=64
            or source_sha256!=actual_source_sha256
            or not isinstance(model_source_sha256,str) or len(model_source_sha256)!=64):
        return {**invalid,'reason_codes':['source_map_not_original_bound']}

    labels = {int(label):cid for label,cid in
              (packet.get('private_label_to_osm_id') or {}).items()}
    decision = response['decision']
    label = response['candidate_label']
    option_ids = response.get('selected_option_ids') or []
    claims = response.get('source_observations') or []
    detail_labels = response.get('request_detail_labels') or []
    alternatives = response.get('contrasted_alternatives') or []
    pattern = response.get('source_pattern') or 'unknown'

    if decision=='unknown' and label==0:
        return {'status':'unknown','candidate_id':None,'accepted':False,
            'proof':None,'reason_codes':['model_declared_unknown'],
            'requested_detail_labels':[x for x in detail_labels if x in labels]}
    if label==0 and decision=='needs_detail':
        return {'status':'needs_detail','candidate_id':None,'accepted':False,
            'proof':None,'reason_codes':[],
            'requested_detail_labels':[x for x in detail_labels if x in labels]}
    cid = labels.get(label)
    if not isinstance(cid,str) or not cid.startswith(('osm:way:','osm:relation:')):
        return {**invalid,'reason_codes':['selected_label_is_not_received_building']}
    if decision=='unknown':
        return {'status':'unknown','candidate_id':cid,'accepted':False,
            'proof':None,'reason_codes':['model_declared_unknown_with_candidate']}

    options = packet.get('options') or {}
    errors, warnings, selected, compared = [], [], {}, []
    for oid in option_ids:
        opt = options.get(oid)
        if not isinstance(opt,dict):
            errors.append('unreceived_osm_option_reference')
            continue
        kind = opt.get('kind')
        bound = (opt.get('body_label')==label
            or kind=='physical_pair' and label in (opt.get('body_labels') or [])
            or kind=='road_axis_direction' and opt.get('first_plan_hit_body_label')==label)
        if not bound:
            errors.append('osM_option_does_not_belong_to_model_selected_body')
            continue
        selected[oid] = opt
        if kind=='observed_corner' and 'nominal_interior' in (
                opt.get('camera_side_advisory') or []):
            warnings.append('camera_anchor_uncertain_for_selected_corner')
    for alternative in alternatives:
        other = alternative['label']
        if other==label or other not in labels:
            errors.append('alternative_label_not_received')
            continue
        mapped=[]
        for oid in alternative.get('observed_option_ids') or []:
            opt = options.get(oid)
            if opt is None or not (
                    opt.get('body_label')==other
                    or opt.get('kind')=='physical_pair'
                       and other in (opt.get('body_labels') or [])):
                errors.append('alternative_option_not_in_received_map')
            else:
                mapped.append(oid)
        compared.append({'label':other,'candidate_id':labels[other],
            'option_ids':mapped,
            'model_visual_difference':alternative['source_vs_map_difference']})
    requested = [label_value for label_value in detail_labels if label_value in labels]
    if len(requested)!=len(detail_labels):
        warnings.append('unreceived_detail_request_skipped')

    # Semantic comparison is LLM-first. We do not decide that every PHOTO
    # requires a road, explicit second facade, precise pose or two distinct
    # observations. An independently selected single front wall can suffice
    # if the SOURCE-using model states that it is a distinctive match.
    if decision=='accept' and not claims:
        errors.append('no_image_observation_provided')
    if decision=='accept' and not selected:
        # On the initial overview, the correct measured options were not even
        # shown to the model. Request one candidate-led expansion instead of
        # rejecting its otherwise useful first-pass physical nomination.
        warnings.append('candidate_needs_map_detail_for_measured_support')
    if decision=='accept' and requested:
        warnings.append('model_requests_more_detail_before_final_identity')
    accepted = (decision=='accept' and not errors and bool(selected)
        and any(isinstance(obs,str) and obs.strip() for obs in claims)
        and not requested)

    # The model nominates the focus group, not a distance/score heuristic.
    # Never force a follow-up where the model actually returned UNKNOWN.
    expanded = {int(x) for x in packet.get('expanded_labels') or []}
    suggested = list(dict.fromkeys([
        *requested,
        *([label] if label not in expanded and not accepted else []),
        *(x['label'] for x in compared if x['label'] not in expanded)][:4]))
    if decision=='needs_detail' or (decision=='accept' and not accepted and suggested):
        status='needs_detail'
    elif accepted:
        status='accepted_geometry_v3'
    else:
        status='candidate_unconfirmed'
    result={'status':status,'candidate_id':cid,'candidate_label':label,
        'accepted':accepted,'proof':None,
        'reason_codes':list(dict.fromkeys(errors)),
        'geometric_warnings':list(dict.fromkeys(warnings)),
        'model_pattern':pattern,'selected_measured_options':list(selected),
        'alternatives':compared,'requested_detail_labels':suggested,
        'initial_semantic_decision':decision,
        'source_observations':claims}
    if accepted:
        evidence={'policy':POLICY,'validated':True,'candidate_id':cid,
            'source_sha256':source_sha256,'model_source_sha256':model_source_sha256,
            'map_image_sha256':actual_map_sha256,'model_pattern':pattern,
            'model_source_observations':claims,
            'model_crop_scope':response.get('crop_scope','unknown'),
            'chosen_measured_options':selected,'compared_alternatives':compared,
            'source_map_options_digest':hashlib.sha256(json.dumps(
               packet,sort_keys=True,ensure_ascii=False,
               separators=(',',':')).encode()).hexdigest(),
            'model_answer_digest':hashlib.sha256(json.dumps(
               response,sort_keys=True,ensure_ascii=False,
               separators=(',',':')).encode()).hexdigest(),
            'scope':'SOURCE-based model identity supported by actual mapped OSM '
              'option references, WITHOUT model-fitted camera pose. This is '
              'independent G-v3 evidence, not automatic canonical POI/facts approval.'}
        evidence['proof_sha256']=hashlib.sha256(json.dumps(
            evidence,sort_keys=True,ensure_ascii=False,
            separators=(',',':')).encode()).hexdigest()
        result['proof']=evidence
    return result
