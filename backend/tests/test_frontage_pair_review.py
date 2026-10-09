"""No answer-key labels sent to a G binary SOURCE/MAP model followup."""
import copy
import json

from google.genai import types

from street_story.identity_frontage_context import adjacent_model_nomination_pair
from street_story.identity_frontage_review import (
    frontage_pair_review_schema, inspect_frontage_pair_review,
)
from street_story.identity_scene import render_scene
from street_story.identity_model_context import physical_decision_context
from test_original_osm_geometry_regression import story_for


def fixture():
    story=story_for(106)
    scene=render_scene(story,[])
    packet=physical_decision_context(story,[],scene['manifest'])
    pair=adjacent_model_nomination_pair(story,[],
        'osm:way:150596903',['osm:way:66345201','osm:way:150596899'])
    # The test fixture includes only the physically relevant raw OSM ways.
    # It does not need the non-adjacent model alternative to test the pair.
    if pair is None:
        pair=adjacent_model_nomination_pair(story,[],
            'osm:way:150596903',['osm:way:150596899'])
    assert pair is not None
    lookup={r[1]:r[0] for r in packet['rows']}
    return scene,packet,pair,lookup


def decision(main,next_):
    return {'decision':'distinguished',
       'main_photo_building_label':main,
       'receding_or_companion_label':next_,
       'visual_relation':'frontage_then_setback',
       'source_main_observations':['Distinct broad visible main frontage along street'],
       'source_companion_observations':['Neighbour facade visibly recedes behind end corner'],
       'map_relation_explanation':'Observed two separate close OSM contours',
       'contradictions':[],'uncertainties':['EXIF yaw missing']}


def test_review_schema_is_small_and_has_no_target_or_invented_camera_pose():
    schema=frontage_pair_review_schema()
    conf=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema)
    assert 'response_json_schema' in conf.model_dump(mode='json',exclude_none=True)
    assert len(json.dumps(schema).encode())<2000
    assert 'candidate_id' not in schema['properties']
    assert 'heading_degrees' not in json.dumps(schema)


def test_conditional_pair_can_distinguish_main_from_receding_with_real_osm():
    scene,packet,pair,labels=fixture()
    # The earlier model nominated .903 and named .899 as an alternative.
    # A hypothetical visual follow-up picks .899 as the main volume.
    src=decision(labels['osm:way:150596899'],labels['osm:way:150596903'])
    report=inspect_frontage_pair_review(src,pair,packet,scene['manifest'])
    assert report['status']=='conditional_physical_nomination'
    assert report['candidate_id']=='osm:way:150596899'
    assert report['nomination_changed'] is True
    assert report['observed_osm_boundary_gap_m']<1
    assert report['authorizes_identity'] is False
    reverse=decision(labels['osm:way:150596903'],labels['osm:way:150596899'])
    assert inspect_frontage_pair_review(reverse,pair,packet,scene['manifest'])['nomination_changed'] is False


def test_uncertain_or_unproven_semantics_cannot_assign_a_physical_identity():
    scene,packet,pair,labels=fixture()
    main=labels['osm:way:150596899']
    neighbor=labels['osm:way:150596903']
    good=decision(main,neighbor)
    for operation in ['generic','no_volume','contradiction','unreceived','duplicate']:
        raw=copy.deepcopy(good)
        if operation=='generic':raw['visual_relation']='two_aligned_facades'
        elif operation=='no_volume':raw['source_companion_observations']=[]
        elif operation=='contradiction':raw['contradictions']=['Cannot assign physical body']
        elif operation=='unreceived':raw['main_photo_building_label']=999999
        elif operation=='duplicate':raw['receding_or_companion_label']=main
        result=inspect_frontage_pair_review(raw,pair,packet,scene['manifest'])
        assert result['authorizes_identity'] is False
        assert result['candidate_id'] is None
        assert result['reason_codes']
    undecided=copy.deepcopy(good)
    undecided['decision']='uncertain'
    assert inspect_frontage_pair_review(undecided,pair,packet,scene['manifest'])['status']=='uncertain'
    forged=copy.deepcopy(pair)
    forged['observed_osm_boundary_gap_m']=100
    assert inspect_frontage_pair_review(good,forged,packet,scene['manifest'])['candidate_id'] is None
