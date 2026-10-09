"""Model-led comparison of two *previously nominated* adjacent OSM buildings.

A broad SOURCE photo may show several joined or closely abutting physical OSM
footprints. A previous image+MAP model nominates one physical body and another
as a competitor. Only when a genuine observed OSM gap connects those bodies
may a focused view ask the image-using LLM to assign MAIN street frontage vs
RECEDING companion, or uncertain. This is not a ground-truth shortlist or an
address/rank correction. Host mechanics verify received labels, pair provenance,
explicit SOURCE observations, and OSM map relations; it can return a CONDITIONAL
candidate to G/T but never certify an identity without the strict proof.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator


def frontage_pair_review_schema():
    text={'type':'string','maxLength':300}
    return {'type':'object','properties':{
        'decision':{'type':'string','enum':['distinguished','uncertain']},
        'main_photo_building_label':{'type':'integer'},
        'receding_or_companion_label':{'type':'integer'},
        'visual_relation':{'type':'string','enum':[
            'frontage_then_setback','visible_corner_and_receding_facade',
            'two_aligned_facades','not_distinguishable']},
        'source_main_observations':{'type':'array','items':text,'maxItems':4},
        'source_companion_observations':{'type':'array','items':text,'maxItems':4},
        'map_relation_explanation':text,
        'contradictions':{'type':'array','items':text,'maxItems':4},
        'uncertainties':{'type':'array','items':text,'maxItems':4}},
      'required':['decision','main_photo_building_label',
        'receding_or_companion_label','visual_relation',
        'source_main_observations','source_companion_observations',
        'map_relation_explanation','contradictions','uncertainties']}


def inspect_frontage_pair_review(model_response, pair, physical_context, map_manifest):
    """Return non-authorizing, provenance-bound result; never rank by distance.

    The supplied 'pair' must come from a prior model nomination + exactly one
    actual near OSM-boundary alternative. It is NOT a positive SOURCE label or
    ground truth; a different pair derived from owner expected IDs is forbidden
    at the caller. The original full pool and raw model response remain stored.
    """
    invalid={'status':'invalid','candidate_id':None,'authorizes_identity':False,
             'reason_codes':['model_pair_review_unverifiable']}
    if (not isinstance(pair,dict) or pair.get('authorizes_identity') is not False
            or not isinstance(model_response,dict)
            or not Draft202012Validator(frontage_pair_review_schema()).is_valid(model_response)):
        return invalid
    rows=[dict(zip(physical_context.get('columns') or [],r))
          for r in physical_context.get('rows') or []]
    id_to_row={r.get('candidate_id'):r for r in rows if isinstance(r.get('candidate_id'),str)}
    from_id=pair.get('physical_subject_candidate_id')
    alt_id=pair.get('adjacent_alternative_candidate_id')
    if (not isinstance(from_id,str) or not isinstance(alt_id,str)
            or from_id==alt_id or from_id not in id_to_row or alt_id not in id_to_row
            or pair.get('physical_passage_verified') is not False):
        return invalid
    ids={id_to_row[from_id].get('label'):from_id,id_to_row[alt_id].get('label'):alt_id}
    if len(ids)!=2 or any(type(label) is not int for label in ids):
        return invalid
    raw_manifest=(map_manifest.get('objects') or {})
    columns=raw_manifest.get('columns') or []
    labels={dict(zip(columns,row)).get('label'):dict(zip(columns,row)).get('candidate_id')
         for row in raw_manifest.get('rows') or []}
    if any(labels.get(label)!=cid for label,cid in ids.items()):
        return invalid

    main_label=model_response.get('main_photo_building_label')
    next_label=model_response.get('receding_or_companion_label')
    if model_response['decision']=='uncertain':
        return {'status':'uncertain','candidate_id':None,
            'original_model_candidate_id':from_id,
            'reviewed_model_alternative_id':alt_id,'reason_codes':[
                'source_cannot_distinguish_adjoining_physical_bodies'],
            'authorizes_identity':False}
    reasons=[]
    if (main_label not in ids or next_label not in ids or main_label==next_label):
        reasons.append('chosen_body_not_in_model_nomination_pair')
    if (model_response['visual_relation'] not in {
            'frontage_then_setback','visible_corner_and_receding_facade'}):
        reasons.append('main_and_receding_volume_not_visually_distinguished')
    if (not any(x.strip() for x in model_response['source_main_observations'])
            or not any(x.strip() for x in model_response['source_companion_observations'])):
        reasons.append('missing_photo_volume_observation')
    if not model_response['map_relation_explanation'].strip():
        reasons.append('missing_mapped_relation')
    if model_response['contradictions']:
        reasons.append('model_reports_unresolved_contradiction')
    # Equality of two candidate outlines, a true passage, overlap or street
    # frontage cannot be inferred just because the OSM boundary gap is small.
    gap=pair.get('observed_osm_boundary_gap_m')
    if not isinstance(gap,(int,float)) or isinstance(gap,bool) or not 0<=gap<=3:
        reasons.append('close_osm_gap_not_verified')
    return {'status':'conditional_physical_nomination',
       'candidate_id':ids.get(main_label) if not reasons else None,
       'candidate_label':main_label if not reasons else None,
       'companion_candidate_id':ids.get(next_label) if not reasons else None,
       'original_model_candidate_id':from_id,
       'nomination_changed':ids.get(main_label)!=from_id if not reasons else None,
       'observed_osm_boundary_gap_m':gap,
       'source_observations_count':len(model_response['source_main_observations']) +
           len(model_response['source_companion_observations']),
       'relation':model_response['visual_relation'],'reason_codes':reasons,
       'authorizes_identity':False,
       'scope':'Physical scope of a model-nominated close pair; SOURCE semantics '
             'supplied by an image-using model, map membership and 2D gap '
             'by independent OSM. This is not an accepted geometry proof.'}
