"""Real SOURCE-neutral-map G data and provider-simple semantic contract.

These are host validation tests; they never assert that an automatic visual
model succeeds or supply a known-answer ID to one.
"""
from copy import deepcopy
import json

from google.genai import types

from street_story.identity_geometry_nomination import (
    visual_geometry_nomination_schema, check_visual_geometry_nomination,
)
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from test_original_osm_geometry_regression import story_for


def proposal(candidate_label, relationship, *, alternatives=()):
    return {'decision': 'nominated', 'candidate_label': candidate_label,
        'source_observations': ['Observed physical facade facing across a road'],
        'spatial_relations': [relationship],
        'alternative_labels': list(alternatives), 'uncertainties': [
            'Camera heading is not present in EXIF; no calibrated source crop']}


def test_g_provider_schema_is_small_valid_plain_json_without_pose_guess():
    schema=visual_geometry_nomination_schema()
    config=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema,max_output_tokens=2048)
    encoded=config.model_dump(mode='json',exclude_none=True)
    assert 'response_json_schema' in encoded
    assert len(json.dumps(schema).encode()) < 2500
    assert 'pose' not in json.dumps(schema)
    assert 'candidate_id' not in json.dumps(schema)
    assert 'heading_degrees' not in json.dumps(schema)


def test_original_132_conditional_witness_checks_real_road_and_both_directions():
    story=story_for(132)
    scene=render_scene(story,[])
    capsule=physical_decision_context(story,[],scene['manifest'])
    bodies={row[1]:row[0] for row in capsule['rows']}
    manifest=scene['manifest']
    obj={row[1]:row[0] for row in manifest['objects']['rows'] if len(row)>1}
    subject=bodies['osm:way:192217077']
    road=obj['osm:way:67826885']
    relation={'kind':'street_termination',
        'source_observation':'SOURCE shows an approach ending in a cross street before the frontal building.',
        'map_body_labels':[subject],
        'road_candidate_id':'osm:way:67826885','road_direction_index':1,
        'segment_ring_index':0,'first_segment_index':0,'second_segment_index':0}
    original=proposal(subject,relation,alternatives=[bodies['osm:way:192220354']])
    result=check_visual_geometry_nomination(original,manifest,capsule)
    assert result['candidate_id']=='osm:way:192217077'
    assert result['status']=='conditional_physical_nomination'
    assert result['reason_codes']==[]
    assert result['authorizes_identity'] is False
    assert result['alternative_candidate_ids']==['osm:way:192220354']
    reverse=deepcopy(original)
    reverse['spatial_relations'][0]['road_direction_index']=0
    out=check_visual_geometry_nomination(reverse,manifest,capsule)
    assert out['candidate_id']==result['candidate_id']
    assert 'road_first_hit_disagrees_with_nomination' in out['reason_codes']
    assert out['authorizes_identity'] is False


def test_original_106_frontage_can_preserve_visual_nomination_without_fake_camera_pose():
    story=story_for(106)
    scene=render_scene(story,[])
    capsule=physical_decision_context(story,[],scene['manifest'])
    bodies={row[1]:row[0] for row in capsule['rows']}
    subject=bodies['osm:way:150596899']
    neighbour=bodies['osm:way:150596903']
    relation={'kind':'frontage_sequence',
        'source_observation':'The photographed long front ends at a corner and a second body continues behind it.',
        'map_body_labels':[subject,neighbour],
        'road_candidate_id':'','road_direction_index':0,
        'segment_ring_index':0,'first_segment_index':0,'second_segment_index':1}
    output=check_visual_geometry_nomination(proposal(subject,relation),
        scene['manifest'],capsule)
    assert output['candidate_id']=='osm:way:150596899'
    assert output['reason_codes']==[]
    assert output['supported_spatial_kinds']==['frontage_sequence']
    assert output['authorizes_identity'] is False
    false_label=proposal(888888,relation)
    outcome=check_visual_geometry_nomination(false_label,scene['manifest'],capsule)
    assert outcome['status']=='invalid' and outcome['candidate_id'] is None
    assert outcome['authorizes_identity'] is False


def test_nomination_does_not_treat_schema_echo_or_unbound_image_as_evidence():
    story=story_for(132)
    scene=render_scene(story,[])
    capsule=physical_decision_context(story,[],scene['manifest'])
    result=check_visual_geometry_nomination({'type':'object','properties':{}},
        scene['manifest'],capsule)
    assert result['status']=='invalid'
    assert result['authorizes_identity'] is False
    valid={'decision':'uncertain','candidate_label':0,'source_observations':[],
        'spatial_relations':[],'alternative_labels':[],'uncertainties':[
            'No sufficient unique relationship']}
    assert check_visual_geometry_nomination(valid,scene['manifest'],capsule)['status']=='uncertain'
    assert check_visual_geometry_nomination(valid,scene['manifest'],capsule,
        source_map_bound=False)['reason_codes']==['unbound_source_map']
