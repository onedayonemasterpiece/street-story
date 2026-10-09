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
        'source_pattern':{'type':'string','enum':[
            'single_frontage','corner','frontage_sequence','setback',
            'street_termination','partial_complex','unknown']},
        'crop_scope':{'type':'string','enum':['whole','partial','unknown']},
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
        'required':['decision','candidate_label','source_pattern','crop_scope',
            'source_observations','selected_option_ids','contrasted_alternatives',
            'request_detail_labels','uncertainties']}


def check_spatial_choice(response, packet, *, source_sha256, model_source_sha256,
                         actual_source_sha256, actual_map_sha256):
    """Safe host check; accepted output has v3 spatial proof, not old POI proof.

    The semantic SOURCE judgment comes solely from the model. Deterministic
    checks enforce exact body/option membership and measured spatial claims.
    A candidate with no specific or incompatible evidence is retained as a
    conditional lead. No option/camera yaw gets changed after model response.
    """
    bad={'status':'invalid','candidate_id':None,'accepted':False,'proof':None,
         'reason_codes':['invalid_spatial_option_response']}
    if not isinstance(response,dict) or not isinstance(packet,dict):
        return bad
    if not Draft202012Validator(visual_spatial_choice_schema()).is_valid(response):
        return bad
    if (packet.get('version')!='street_story.g_spatial_options.v3' or
            packet.get('map_sha256')!=actual_map_sha256 or
            source_sha256!=actual_source_sha256 or
            not isinstance(source_sha256,str) or len(source_sha256)!=64 or
            not isinstance(model_source_sha256,str) or len(model_source_sha256)!=64):
        return {**bad,'reason_codes':['source_map_not_original_bound']}
    labels={int(label):cid for label,cid in
            (packet.get('private_label_to_osm_id') or {}).items()}
    candidate=response['candidate_label']
    cid=labels.get(candidate)
    if response['decision']=='unknown' and candidate==0:
        return {'status':'unknown','candidate_id':None,'accepted':False,
            'proof':None,'reason_codes':['model_declared_unknown']}
    if cid is None or not cid.startswith(('osm:way:','osm:relation:')):
        return {**bad,'reason_codes':['model_body_not_received']}
    all_options=packet.get('options') or {}
    reasons=[]
    valid=[]
    for oid in response['selected_option_ids']:
        option=all_options.get(oid)
        if not isinstance(option,dict):
            reasons.append('model_option_id_not_in_frozen_osm')
            continue
        kind=option.get('kind')
        owned=(option.get('body_label')==candidate
               or kind=='physical_pair' and candidate in option.get('body_labels',[])
               or kind=='road_axis_direction' and option.get('first_plan_hit_body_label')==candidate)
        if not owned:
            reasons.append('option_not_bound_to_chosen_physical_body')
            continue
        valid.append((oid,option))
        if kind=='road_axis_direction' and response['source_pattern']!='street_termination':
            reasons.append('street_ray_without_street_source_pattern')
        if kind=='observed_corner':
            side=option.get('camera_side_advisory') or []
            if 'nominal_interior' in side:
                reasons.append('nominal_camera_rear_wall_uncertainty')
    refs=[]
    for alt in response['contrasted_alternatives']:
        label=alt['label']
        if label==candidate or label not in labels or not alt['source_vs_map_difference'].strip():
            reasons.append('material_alternative_not_grounded')
            continue
        observed=[]
        for oid in alt['observed_option_ids']:
            other=all_options.get(oid)
            if not isinstance(other,dict):
                reasons.append('contrast_option_not_in_frozen_osm')
            elif not (other.get('body_label')==label
                  or other.get('kind')=='physical_pair'
                      and label in other.get('body_labels',[])):
                reasons.append('contrast_option_does_not_reference_alternative')
            else:
                observed.append(oid)
        refs.append({'label':label,'candidate_id':labels[label],'option_ids':observed,
            'model_visual_difference':alt['source_vs_map_difference']})
    if response['request_detail_labels']:
        if any(label not in labels for label in response['request_detail_labels']):
            reasons.append('requested_unreceived_detail_label')
        if response['decision']=='accept':
            reasons.append('acceptance_with_unresolved_map_detail')
    if response['decision']=='accept':
        if not response['source_observations'] or any(not s.strip()
              for s in response['source_observations']):
            reasons.append('no_source_spatial_observation')
        if not valid:
            reasons.append('no_measured_osm_option_selected')
        kinds={obj['kind'] for _oid,obj in valid}
        strong={'observed_corner','road_axis_direction','physical_pair','single_frontage'}
        if not kinds.intersection(strong):
            reasons.append('plan_shape_alone_not_physical_identity')
        if response['source_pattern']=='corner' and 'observed_corner' not in kinds:
            reasons.append('corner_not_selected_from_true_osm')
        if response['source_pattern']=='street_termination' and 'road_axis_direction' not in kinds:
            reasons.append('street_without_selected_measured_road')
        if response['source_pattern'] in {'setback','frontage_sequence'} and 'physical_pair' not in kinds:
            reasons.append('multi_body_relation_without_observed_pair')
        if response['source_pattern']=='single_frontage' and not (
                'single_frontage' in kinds and len(refs)>0):
            reasons.append('single_facade_without_distinguishing_competitor')
        if response['source_pattern']=='partial_complex':
            reasons.append('partial_complex_scope_unresolved')
        if response['source_pattern']=='unknown':
            reasons.append('source_spatial_pattern_unknown')
        if not refs:
            reasons.append('no_material_alternative_contrast')
    accepted=response['decision']=='accept' and not reasons
    result={'status':('accepted_geometry_v3' if accepted else
                'candidate_unconfirmed' if response['decision']!='unknown' else 'unknown'),
        'candidate_id':cid,'candidate_label':candidate,
        'accepted':accepted,'proof':None,'reason_codes':list(dict.fromkeys(reasons)),
        'model_pattern':response['source_pattern'],
        'selected_measured_options':[oid for oid,_v in valid],
        'alternatives':refs,'requested_detail_labels':response['request_detail_labels']}
    if accepted:
        evidence={
            'policy':POLICY,'validated':True,'candidate_id':cid,
            'source_sha256':source_sha256,'model_source_sha256':model_source_sha256,
            'map_image_sha256':actual_map_sha256,
            'source_pattern':response['source_pattern'],
            'model_source_observations':response['source_observations'],
            'model_crop_scope':response['crop_scope'],
            'chosen_measured_options':{oid:value for oid,value in valid},
            'compared_alternatives':refs,
            'source_map_options_digest':hashlib.sha256(json.dumps(
                packet,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
            'model_answer_digest':hashlib.sha256(json.dumps(
                response,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
            'scope':'Model SOURCE judgment matched to literal precomputed OSM options. '
               'This G-v3 evidence is NOT a v2 canonical POI/facts approval.'}
        evidence['proof_sha256']=hashlib.sha256(json.dumps(
            evidence,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        result['proof']=evidence
    return result
