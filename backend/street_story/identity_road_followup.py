"""One addressed G-only review of an unproven visual building nomination.

The prior candidate originates ONLY from an actual closed SOURCE+neutral-MAP
visual model response, never the owner's target oracle. The follow-up sees
the same original SOURCE/MAP and actual bidirectional OSM ray observations.
It may reject the prior. It cannot create an ID, road, fabricated compass yaw
or an architectural-text proof. The host still applies the original strict
geometry certificate independently before accepting a physical identity.
"""
from __future__ import annotations


def street_termination_review_schema():
    text={'type':'string','maxLength':300}
    return {'type':'object','properties':{
        'verdict':{'type':'string','enum':['street_termination_supported',
            'not_supported','uncertain']},
        'prior_subject_label':{'type':'integer'},
        'road_candidate_id':{'type':'string'},
        'road_direction_index':{'type':'integer'},
        'opposite_first_hit_label':{'type':'integer'},
        'source_road_observations':{'type':'array','items':text,'maxItems':4},
        'visual_contradictions':{'type':'array','items':text,'maxItems':4},
        'uncertainties':{'type':'array','items':text,'maxItems':4}},
      'required':['verdict','prior_subject_label','road_candidate_id',
       'road_direction_index','opposite_first_hit_label','source_road_observations',
       'visual_contradictions','uncertainties']}


def combine_road_review(prior_visual, followup):
    """Return model-authored G observation or None; NO host identity decision.

    Never transplant geometry from the report-only target oracle or fill missing
    visual observations. Numeric angles are computed later by the OSM verifier.
    Both model responses remain separately retained with their own provider
    receipts and source hashes, including unsupported/uncertain followups.
    """
    from jsonschema import Draft202012Validator
    from .identity_geometry_nomination import visual_geometry_nomination_schema
    if (not isinstance(prior_visual,dict) or not isinstance(followup,dict)
            or not Draft202012Validator(visual_geometry_nomination_schema()).is_valid(prior_visual)
            or not Draft202012Validator(street_termination_review_schema()).is_valid(followup)
            or prior_visual['decision'] != 'nominated'
            or followup['verdict'] != 'street_termination_supported'
            or followup['prior_subject_label'] != prior_visual['candidate_label']
            or followup['road_direction_index'] not in (0,1)
            or not followup['road_candidate_id'].startswith('osm:way:')
            or followup['opposite_first_hit_label'] <= 0
            or followup['opposite_first_hit_label'] == prior_visual['candidate_label']
            or followup['visual_contradictions']
            or not any(isinstance(x,str) and x.strip()
                for x in followup['source_road_observations'])):
        return None
    source_statement=' '.join(text.strip() for text in followup['source_road_observations']
        if isinstance(text,str) and text.strip())[:290]
    value={'decision':'nominated',
        'candidate_label':prior_visual['candidate_label'],
        'source_horizontal_extent':prior_visual['source_horizontal_extent'],
        'source_observations':[
            *prior_visual['source_observations'][:2],
            *followup['source_road_observations'][:3]][:5],
        'spatial_relations':[{
            'kind':'street_termination',
            'source_observation':source_statement,
            'map_body_labels':[prior_visual['candidate_label']],
            'road_candidate_id':followup['road_candidate_id'],
            'road_direction_index':followup['road_direction_index'],
            'segment_ring_index':0,
            'first_segment_index':0,
            'second_segment_index':0}],
        'alternative_labels':[followup['opposite_first_hit_label']],
        'uncertainties':[
            *prior_visual['uncertainties'][:2],
            *followup['uncertainties'][:3]][:5]}
    if not Draft202012Validator(visual_geometry_nomination_schema()).is_valid(value):
        return None
    return value
