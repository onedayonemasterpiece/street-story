"""Independent SOURCE→OSM road-direction observation; no prior physical target.

The model receives original SOURCE, neutral MAP and observed road axes, but
NOT an earlier nominated building ID, its label, expected target, address,
Wikipedia or Prussia description. It chooses an actual approach road and one
of its two displayed rays from image/scene correspondence. Host then compares
that ray's first observed physical building to the *separate* already-closed
visual nomination. Only matching evidence may proceed to the existing strict
G proof. No bearing or candidate is adjusted to force agreement.
"""
from __future__ import annotations


def independent_road_direction_schema():
    txt={'type':'string','maxLength':300}
    return {'type':'object','properties':{
        'route_kind':{'type':'string','enum':[
            'street_termination_visible','not_street_termination','uncertain']},
        'road_candidate_id':{'type':'string'},
        'road_direction_index':{'type':'integer'},
        'transverse_cross_street_visible':{'type':'boolean'},
        'source_road_observations':{'type':'array','items':txt,'maxItems':4},
        'visible_contradictions':{'type':'array','items':txt,'maxItems':4},
        'uncertainties':{'type':'array','items':txt,'maxItems':4}},
      'required':['route_kind','road_candidate_id','road_direction_index',
        'transverse_cross_street_visible','source_road_observations',
        'visible_contradictions','uncertainties']}


def combine_independent_road_direction(prior_visual, direction_response,
                                       physical_context, manifest):
    """LLM-submitted road+direction agrees with a different earlier LLM body?

    Produces a non-authorizing combined observation with explicit provenance
    of the host-derived opposite-ray competitor. No target IDs are supplied to
    the direction model. The expensive semantic work remains entirely with the
    two image-using model operations.
    """
    from jsonschema import Draft202012Validator
    from .identity_geometry_nomination import (
        visual_geometry_nomination_schema, check_visual_geometry_nomination)
    if (not isinstance(prior_visual,dict) or not isinstance(direction_response,dict)
            or not Draft202012Validator(visual_geometry_nomination_schema()).is_valid(prior_visual)
            or not Draft202012Validator(independent_road_direction_schema()).is_valid(direction_response)
            or prior_visual['decision']!='nominated'
            or direction_response['route_kind']!='street_termination_visible'
            or direction_response['transverse_cross_street_visible'] is not True
            or direction_response['visible_contradictions']
            or len(direction_response['source_road_observations'])<2
            or any(not isinstance(s,str) or not s.strip()
                for s in direction_response['source_road_observations'])):
        return None
    roads=(physical_context.get('bidirectional_road_axis_cues') or {}).get('rows') or []
    road=next((x for x in roads if x[0]==direction_response['road_candidate_id']),None)
    idx=direction_response['road_direction_index']
    if road is None or idx not in (0,1):
        return None
    toward=road[6][idx][1]
    contrary=road[6][1-idx][1]
    if not toward or not contrary:
        return None
    body_lookup={row[1]:row[0] for row in physical_context.get('rows') or []}
    subject=body_lookup.get(toward[0][1])
    alternative=body_lookup.get(contrary[0][1])
    if (type(subject) is not int or type(alternative) is not int
            or subject == alternative or subject != prior_visual['candidate_label']):
        return None
    statement=' '.join(x.strip() for x in direction_response['source_road_observations'])[:290]
    combined={'decision':'nominated','candidate_label':subject,
        'source_horizontal_extent':prior_visual['source_horizontal_extent'],
        'source_observations':[
            *prior_visual['source_observations'][:2],
            *direction_response['source_road_observations'][:3]][:5],
        'spatial_relations':[{
            'kind':'street_termination','source_observation':statement,
            'map_body_labels':[subject],
            'road_candidate_id':road[0],
            'road_direction_index':idx,
            'segment_ring_index':0,'first_segment_index':0,'second_segment_index':0}],
        # Exact OTHER-ray first hit is deterministically obtained from the
        # received map. It is never mistaken for a model-supplied rejection.
        'alternative_labels':[alternative],
        'uncertainties':[
            *prior_visual['uncertainties'][:2],
            *direction_response['uncertainties'][:3]][:5]}
    result=check_visual_geometry_nomination(combined,manifest,physical_context)
    if (result['status']!='conditional_physical_nomination'
            or result['reason_codes']):
        return None
    return {'combined':combined,'nomination':result,
        'first_model_chose_physical_body':True,
        'second_model_chose_road_and_direction_without_priors':True,
        'opposite_candidate_from_osm_not_model':True,
        'authorizes_identity':False}
