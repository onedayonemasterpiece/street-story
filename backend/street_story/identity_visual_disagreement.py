"""Source-grounded review of TWO independently model-nominated physical OSM bodies.

Only a closed image-using provider response may nominate each candidate. The
retained SOURCE SHA and neutral MAP provenance must agree between both
attempts. An optional image-using referee compares original SOURCE against
a neutral map with detail of BOTH candidates. No owner acceptance truth,
architecture text, target ranking or numeric yaw is inserted.

This module is strictly non-authorizing: host proof for physical identity
remains freeze_geometry_proof (or independently validated G certificate).
"""
from __future__ import annotations

from jsonschema import Draft202012Validator


def visual_disagreement_schema():
    text={'type':'string','maxLength':300}
    return {'type':'object','properties':{
        'decision':{'type':'string','enum':['distinguished','uncertain']},
        'selected_body_label':{'type':'integer'},
        'source_horizontal_extent':{'type':'string',
            'enum':['broad','medium','narrow','unknown']},
        'source_observations':{'type':'array','items':text,'maxItems':5},
        'visible_corner':{'type':'boolean'},
        'corner_ring_index':{'type':'integer'},
        'first_corner_segment_index':{'type':'integer'},
        'second_corner_segment_index':{'type':'integer'},
        'map_match_explanation':text,
        'other_body_spatial_contradictions':{'type':'array','items':text,'maxItems':4},
        'camera_crop_uncertainties':{'type':'array','items':text,'maxItems':4}},
        'required':['decision','selected_body_label','source_horizontal_extent',
            'source_observations','visible_corner','corner_ring_index',
            'first_corner_segment_index','second_corner_segment_index',
            'map_match_explanation','other_body_spatial_contradictions',
            'camera_crop_uncertainties']}


def check_visual_disagreement(answer, candidate_ids, physical_context, map_manifest,
                              *, bound_source_map=True):
    invalid={'status':'invalid','candidate_id':None,'authorizes_identity':False,
        'reason_codes':['unverified_independent_visual_disagreement']}
    if (not bound_source_map or not isinstance(answer,dict)
        or not Draft202012Validator(visual_disagreement_schema()).is_valid(answer)
        or not isinstance(candidate_ids,(list,tuple)) or len(set(candidate_ids))!=2):
        return invalid
    physical={r[1]:dict(zip(physical_context.get('columns') or [],r))
        for r in physical_context.get('rows') or []}
    if any(cid not in physical for cid in candidate_ids):
        return invalid
    candidate_by_label={physical[cid]['label']:cid for cid in candidate_ids}
    objects=map_manifest.get('objects') or {}
    received_labels={dict(zip(objects.get('columns') or [],row)).get('label'):
        dict(zip(objects.get('columns') or [],row)).get('candidate_id')
        for row in objects.get('rows') or []}
    if len(candidate_by_label)!=2 or any(received_labels.get(label)!=cid
        for label,cid in candidate_by_label.items()):
        return invalid
    if answer['decision']=='uncertain':
        return {'status':'uncertain','candidate_id':None,
            'reason_codes':['visual_referee_not_distinctive'],
            'authorizes_identity':False}
    cid=candidate_by_label.get(answer['selected_body_label'])
    if cid is None:
        return invalid
    row=physical[cid]
    reasons=[]
    if not any(isinstance(s,str) and s.strip() for s in answer['source_observations']):
        reasons.append('no_source_geometry_observation')
    if not answer['map_match_explanation'].strip():
        reasons.append('no_map_spatial_correspondence')
    if not answer['other_body_spatial_contradictions']:
        reasons.append('other_independent_nominee_not_distinguished')
    if answer['visible_corner']:
        chosen=(answer['corner_ring_index'],answer['first_corner_segment_index'],
            answer['second_corner_segment_index'])
        if not any(tuple(pair[:3])==chosen
                   for pair in row.get('observed_connected_side_pairs') or []):
            reasons.append('corner_pair_not_observed_in_received_osm')
    ratio=row.get('outline_span_over_exif_diagonal')
    if (answer['source_horizontal_extent']=='broad' and
         (physical_context.get('source_angular_reference') or {}).get(
             'camera_position_status')=='original_exif'
         and isinstance(ratio,(int,float)) and not isinstance(ratio,bool)
         and ratio<.25):
        reasons.append('angular_size_vs_source_broad_extent_conflict')
    return {'status':'conditional_physical_nomination',
        'candidate_id':cid,'candidate_label':answer['selected_body_label'],
        'rejected_competing_nomination_id':next(x for x in candidate_ids if x!=cid),
        'reason_codes':reasons,'observed_corner':answer['visible_corner'],
        'source_horizontal_extent':answer['source_horizontal_extent'],
        'nominal_outline_span_over_35mm_exif_diagonal':ratio,
        'authorizes_identity':False,
        'scope':'Two independent image+MAP provider nominations compared by another '
                'visual model with original SOURCE; map corner and nominal scale '
                'are checked, not a calibrated image-pixel or yaw proof.'}
