"""No nominee/target leaks into the second SOURCE-road-direction model contract."""
import copy
import json

from google.genai import types
from street_story.identity_road_direction import (
    independent_road_direction_schema, combine_independent_road_direction)
from street_story.identity_road_witness import freeze_model_street_termination
from test_road_followup_geometry import full_case, proposals


def test_direction_schema_is_small_no_prior_candidate_or_poses():
    schema=independent_road_direction_schema()
    config=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema)
    data=config.model_dump(exclude_none=True,mode='json')
    assert 'response_json_schema' in data
    encoded=json.dumps(schema)
    assert len(encoded.encode())<1300
    assert 'candidate_label' not in encoded and 'candidate_id' in encoded
    assert 'heading_degrees' not in encoded and 'building' not in encoded


def test_two_independent_model_observations_can_validate_actual_132_road_proof():
    story,ctx,receipt,_body,road=full_case()
    prior, _unused_candidate_seeded_followup=proposals()
    direction={'route_kind':'street_termination_visible',
      'road_candidate_id':road[0],'road_direction_index':1,
      'transverse_cross_street_visible':True,
      'source_road_observations':[
        'The SOURCE street leads to a transverse carriageway before the broad frontal building.',
        'A visible intersection separates the cobbled approach from the facade-facing courtyard.'],
      'visible_contradictions':[],'uncertainties':[
         'The exact road bearing is not in the JPEG EXIF.']}
    assert 'prior_subject_label' not in direction
    value=combine_independent_road_direction(prior,direction,ctx,receipt['manifest'])
    assert value and value['nomination']['candidate_id']=='osm:way:192217077'
    assert value['authorizes_identity'] is False
    proof=freeze_model_street_termination(story,value['combined'],receipt,ctx,
        story['_identity_observed_candidates'])
    assert proof and proof['proof']['proof_sha256']
    assert proof['no_measured_yaw'] is True


def test_independent_wrong_direction_or_courtyard_only_does_not_accept():
    story,ctx,receipt,_body,road=full_case()
    prior,_=proposals()
    correct={'route_kind':'street_termination_visible','road_candidate_id':road[0],
       'road_direction_index':1,'transverse_cross_street_visible':True,
       'source_road_observations':['Transverse street crosses the approach.',
            'Road-ending facade directly faces the camera beyond this crossing.'],
       'visible_contradictions':[],'uncertainties':[]}
    for invalid_type in ('opposite','not_visible','missing_cross_street',
                          'made_up_road','visual_contradiction'):
        bad=copy.deepcopy(correct)
        if invalid_type=='opposite':bad['road_direction_index']=0
        elif invalid_type=='not_visible':bad['route_kind']='uncertain'
        elif invalid_type=='missing_cross_street':bad['transverse_cross_street_visible']=False
        elif invalid_type=='made_up_road':bad['road_candidate_id']='osm:way:999999'
        elif invalid_type=='visual_contradiction':bad['visible_contradictions']=['No transverse road visible']
        value=combine_independent_road_direction(prior,bad,ctx,receipt['manifest'])
        assert value is None
