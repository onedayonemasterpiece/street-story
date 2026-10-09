"""Choose an observed close NEIGHBOR for a bounded G physical-scope review.

The image-using model already nominated a subject and alternatives. The host
only measures literal OSM outline gaps among those same model-chosen objects
and can select ONE closely adjacent pair to ask the model to distinguish in
SOURCE. It NEVER promotes that neighbor as the correct physical building;
far/cropped/telephoto candidates remain available in the full map and should
not be screened out based on this pair review.
"""
from __future__ import annotations


def adjacent_model_nomination_pair(story, candidates, current_id, alternatives,
                                    *, max_observed_gap_m=3.0):
    from .identity_spatial_features import measure_spatial_relations
    if (not isinstance(current_id,str) or not current_id.startswith('osm:')
            or not isinstance(alternatives,(list,tuple))):
        return None
    options=[]
    for alternative in dict.fromkeys(alternatives):
        if (not isinstance(alternative,str) or alternative==current_id
                or not alternative.startswith('osm:')):
            continue
        measured=measure_spatial_relations(story,candidates,[current_id,alternative],
            pose=None,front_segments=[],street_axis=None)
        if not measured:
            continue
        gaps=measured.get('boundary_gaps') or []
        if not gaps or not isinstance(gaps[0].get('observed_boundary_gap_m'),(int,float)):
            continue
        gap=gaps[0]['observed_boundary_gap_m']
        if not 0 <= gap <= max_observed_gap_m:
            continue
        options.append((gap,alternative))
    options.sort(key=lambda row:(row[0],row[1]))
    if not options:
        return None
    # Two independent alternatives equally close can represent a complex;
    # do not silently choose one nearest neighbour when this is unresolved.
    if len(options)>1 and abs(options[1][0]-options[0][0])<=0.25:
        return None
    return {'physical_subject_candidate_id':current_id,
        'adjacent_alternative_candidate_id':options[0][1],
        'observed_osm_boundary_gap_m':options[0][0],
        'other_close_alternative_count':len(options)-1,
        'selection_basis':'Only smallest actual mapped boundary gap among '
            'independently model-nominated alternatives, not a target score '
            'or global physical identity decision',
        'authorizes_identity':False,
        'physical_passage_verified':False}
