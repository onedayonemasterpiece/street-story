"""Reproducible original photo102 OSM with two *prior provider* nominations.

The physical candidates are from actual closed MiMo and Gemini SOURCE+MAP
sessions. No inference or owner oracle is used in this unit fixture.
"""
import copy
import json

from google.genai import types
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from street_story.identity_visual_disagreement import (
    visual_disagreement_schema, check_visual_disagreement)
from test_original_osm_geometry_regression import story_for


def actual_case():
    story=story_for(102)
    scene=render_scene(story,[])
    physical=physical_decision_context(story,[],scene['manifest'])
    labels={r[1]:r[0] for r in physical['rows']}
    assert {'osm:way:133035111','osm:way:133035113'}.issubset(labels)
    return story,scene,physical,labels


def proposed(label,*, visible_corner=True, extent='broad'):
    return {'decision':'distinguished','selected_body_label':label,
        'source_horizontal_extent':extent,
        'source_observations':[
            'A tall facade with a distinct visible return wall',
            'A full main frontage and adjacent side are in frame'],
        'visible_corner':visible_corner,'corner_ring_index':0,
        'first_corner_segment_index':4,
        'second_corner_segment_index':5,
        'map_match_explanation':'One observed near corner matches the received neutral OSM building outline',
        'other_body_spatial_contradictions':[
            'Alternative OSM body has a substantially smaller nominal angular footprint'],
        'camera_crop_uncertainties':['EXIF has no measured heading or actual crop calibration']}


def test_tiny_schema_is_separate_from_strict_host_physical_proof():
    schema=visual_disagreement_schema()
    config=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema)
    assert 'response_json_schema' in config.model_dump(mode='json',exclude_none=True)
    assert len(json.dumps(schema).encode()) < 2400
    assert 'heading_degrees' not in json.dumps(schema)
    assert 'candidate_id' not in schema['properties']


def test_original_102_two_model_nominees_are_valid_map_bodies_but_not_proofs():
    _,scene,physical,labels=actual_case()
    provided=['osm:way:133035111','osm:way:133035113']
    response=proposed(labels['osm:way:133035113'])
    result=check_visual_disagreement(response,provided,physical,scene['manifest'])
    assert result['candidate_id']=='osm:way:133035113'
    assert result['reason_codes']==[]
    assert result['authorizes_identity'] is False
    assert result['observed_corner'] is True
    other=proposed(labels['osm:way:133035111'],visible_corner=False)
    failure=check_visual_disagreement(other,provided,physical,scene['manifest'])
    assert failure['candidate_id']=='osm:way:133035111'
    assert 'angular_size_vs_source_broad_extent_conflict' in failure['reason_codes']
    assert failure['authorizes_identity'] is False


def test_fake_osm_corner_and_unobserved_label_fail_closed():
    _,scene,physical,labels=actual_case()
    ids=['osm:way:133035111','osm:way:133035113']
    correct=proposed(labels['osm:way:133035113'])
    fake=copy.deepcopy(correct)
    fake['first_corner_segment_index']=5
    fake['second_corner_segment_index']=2
    result=check_visual_disagreement(fake,ids,physical,scene['manifest'])
    assert 'corner_pair_not_observed_in_received_osm' in result['reason_codes']
    assert result['authorizes_identity'] is False
    fabricated=copy.deepcopy(correct)
    fabricated['selected_body_label']=999999
    invalid=check_visual_disagreement(fabricated,ids,physical,scene['manifest'])
    assert invalid['status']=='invalid'
    assert invalid['candidate_id'] is None
    unbound=check_visual_disagreement(correct,ids,physical,scene['manifest'],
      bound_source_map=False)
    assert unbound['status']=='invalid'
    undecided=copy.deepcopy(correct)
    undecided['decision']='uncertain'
    assert check_visual_disagreement(undecided,ids,physical,scene['manifest'])['status']=='uncertain'


def test_actual_corner_wrap_may_be_reported_in_reverse_visual_order():
    _story,scene,physical,labels=actual_case()
    ids=['osm:way:133035111','osm:way:133035113']
    data=proposed(labels['osm:way:133035113'])
    data['first_corner_segment_index']=0
    data['second_corner_segment_index']=5  # real ring closes 5 -> 0, not 0 -> 5
    data['map_match_explanation']='The PHOTO visible return and the OSM closed polygon '
    data['map_match_explanation']+='are geometrically compatible. '*15
    result=check_visual_disagreement(data,ids,physical,scene['manifest'])
    assert result['candidate_id']=='osm:way:133035113'
    assert result['reason_codes']==[]
    assert result['observed_corner'] is True
    assert result['authorizes_identity'] is False
