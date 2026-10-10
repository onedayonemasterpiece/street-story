import copy
import math

import pytest

from street_story.identity_scene import render_scene, scene_camera_context
from street_story.identity_spatial_features import geometry_feature, measure_spatial_relations


def point(east, north):
    scale=6_371_000*math.pi/180
    return {'lat':55+north/scale,'lon':21+east/(scale*math.cos(math.radians(55)))}


def building(oid, east, north, width=20, depth=10):
    return {'type':'way','id':oid,'tags':{'building':'yes'},'center':point(east+width/2,north+depth/2),
        'geometry':[point(east,north),point(east+width,north),point(east+width,north+depth),
            point(east,north+depth),point(east,north)]}


def story(entries, **extra):
    return {'latitude':55.,'longitude':21.,'_identity_map_snapshot':{'observed_pool':entries},**extra}


def test_owner_approx_camera_has_nominal_position_but_no_exif_accuracy_or_invented_heading():
    source=story([building(1,10,10)],_location_provenance={'kind':'owner_approx_camera'},
        _camera_position_verified=True,_camera_hints={'direction_degrees':40,'direction_ref':'T','direction_status':'true_north'})
    original=copy.deepcopy(source)
    scene=render_scene(source,[])
    camera=scene['manifest']['camera']
    assert camera['position_status']=='owner_approximate' and not camera['position_verified']
    assert (camera['latitude'],camera['longitude'])==(55.,21.)
    assert camera['position_source']=='owner_approx_camera' and camera['accuracy_m'] is None
    assert camera['accuracy_source']=='unknown' and 'heading_degrees' not in camera
    assert scene['manifest']['anchor']['label']=='CAMERA HINT (OWNER APPROX)'
    assert scene['manifest']['distance_origin']=='owner_approx_camera_hint'
    assert scene['manifest']['coverage']['completeness']=='unknown'
    assert source==original


@pytest.mark.parametrize('accuracy,expected',[(None,None),(12.5,12.5),(True,None),(float('nan'),None),(-2,None)])
def test_owner_accuracy_is_only_an_explicit_valid_owner_value(accuracy,expected):
    camera=scene_camera_context(story([],_location_provenance={'kind':'owner_approx_camera','accuracy_m':accuracy}))
    assert camera['accuracy_m']==expected
    assert camera['accuracy_source']==('owner_reported' if expected is not None else 'unknown')


def test_city_geocode_cannot_become_approximate_camera_and_original_exif_error_is_distinct():
    city=scene_camera_context(story([],_location_provenance={'kind':'owner_live_place_query','accuracy_m':12},
        _camera_position_verified=True))
    assert city['position_status']=='search_context' and city['latitude'] is None
    assert city['accuracy_m'] is None and not city['position_verified']
    exif=scene_camera_context(story([],_camera_position_verified=True,_camera_hints={'horizontal_error_m':3.2}))
    assert exif['position_status']=='original_exif' and exif['accuracy_source']=='original_exif'
    assert exif['accuracy_m']==3.2


def test_feature_aliases_resolve_actual_contour_segments_tags_and_entries_without_fabrication():
    item=building(1,10,10)
    item['tags']['building:levels']='3'
    entry={'type':'node','id':9,**point(10,15),'tags':{'entrance':'yes','addr:housenumber':'18A'}}
    source=story([item,entry])
    original=copy.deepcopy(source)
    contour=geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'footprint','ring_index':0})
    assert contour['closed'] and contour['coordinates']==item['geometry']
    assert contour['kind']=='footprint' and contour['provenance']=='osm.observed_geometry'
    segment=geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'segment','ring_index':0,'segment_index':2})
    assert segment['coordinates']==item['geometry'][2:4]
    assert geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'tag','tag_key':'building:levels'})['value']=='3'
    assert geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'tag','tag_key':'height'}) is None
    assert geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'tag','tag_key':'tunnel'}) is None
    assert geometry_feature(source,[],{'candidate_id':'osm:node:9','kind':'entry'})['coordinates']==[point(10,15)]
    assert geometry_feature(source,[],{'candidate_id':'osm:way:1','kind':'entry'}) is None
    assert source==original


@pytest.mark.parametrize('reference',[
    {'candidate_id':'osm:way:missing','kind':'footprint'},
    {'candidate_id':[],'kind':'footprint'},
    {'candidate_id':'osm:way:1','kind':'footprint','ring_index':True},
    {'candidate_id':'osm:way:1','kind':'segment','segment_index':4},
    {'candidate_id':'osm:way:1','kind':'segment','segment_index':-1},
    {'candidate_id':'osm:way:1','kind':'road_axis'},
    {'candidate_id':'osm:node:8','kind':'entry'},
])
def test_invalid_or_nonexistent_features_do_not_acquire_proof(reference):
    source=story([building(1,10,10),{'type':'node','id':8,**point(0,0),'tags':{'amenity':'cafe'}}])
    assert geometry_feature(source,[],reference) is None


def test_fragmented_outer_and_inner_feature_indices_retain_observed_members_and_no_new_closure():
    outside=building(1,10,10)['geometry']
    inside=building(2,13,12,width=3,depth=3)['geometry']
    item={'type':'relation','id':3,'tags':{'building':'yes'},'members':[
        {'type':'way','ref':1,'role':'outer','geometry':outside[:3]},
        {'type':'way','ref':2,'role':'outer','geometry':outside[2:]},
        {'type':'way','ref':4,'role':'inner','geometry':inside}]}
    source=story([item])
    assert geometry_feature(source,[],{'candidate_id':'osm:relation:3','kind':'footprint','ring_index':0})['coordinates']==outside
    hole=geometry_feature(source,[],{'candidate_id':'osm:relation:3','kind':'footprint','ring_index':1})
    assert hole['role']=='inner' and hole['coordinates']==inside
    incomplete={**item,'members':[item['members'][0]]}
    fragment=geometry_feature(story([incomplete]),[],{'candidate_id':'osm:relation:3','kind':'footprint'})
    assert not fragment['closed'] and fragment['coordinates']==outside[:3]


def test_nominated_gap_and_front_setback_are_vector_measurements_not_passage_truth():
    source=story([building(1,10,10),building(2,36.56,20)])
    refs={'first':{'candidate_id':'osm:way:1','kind':'segment','segment_index':0},
        'second':{'candidate_id':'osm:way:2','kind':'segment','segment_index':0}}
    result=measure_spatial_relations(source,[],['osm:way:1','osm:way:2'],front_segments=[refs])
    assert result['boundary_gaps'][0]['observed_boundary_gap_m']==pytest.approx(6.56,abs=.01)
    assert not result['boundary_gaps'][0]['physical_passage_verified']
    fronts=result['front_segments'][0]
    assert fronts['front_length_m']==20 and fronts['second_length_m']==20
    assert fronts['normal_bearing_degrees']==180 and fronts['signed_front_setback_m']==-10
    assert fronts['normal_basis']=='right_of_first_directed_observed_segment'
    assert fronts['first_angular_span_degrees']>fronts['second_angular_span_degrees']
    assert fronts['side_angle_difference_degrees']==0
    assert result['left_to_right_order'] is None  # Unknown yaw does not turn west/east into image left/right.


def test_camera_yaw_and_explicit_offset_change_neighbor_order_without_claiming_recovered_gps():
    source=story([building(1,-40,40),building(2,10,40),building(3,50,-20)],
        _location_provenance={'kind':'owner_approx_camera'})
    result=measure_spatial_relations(source,[],['osm:way:1','osm:way:2','osm:way:3'],
        pose={'east_m':0,'north_m':-10,'heading_degrees':40})
    assert result['left_to_right_order']==['osm:way:1','osm:way:2','osm:way:3']
    assert not result['pose_scenario']['is_measured_camera_pose']
    assert result['pose_scenario']['provenance']=='explicit_scenario'
    assert result['camera']['accuracy_m'] is None
    assert result['objects'][2]['relative_bearing_degrees']>40


def test_street_axis_ray_checks_full_received_physical_pool_beyond_nominated_near_building():
    road={'type':'way','id':99,'tags':{'highway':'residential'},'geometry':[point(0,0),point(40,0)]}
    source=story([building(1,10,20),building(2,70,-5,width=20,depth=10),road])
    result=measure_spatial_relations(source,[],['osm:way:1'],pose={'heading_degrees':90},
        street_axis={'candidate_id':'osm:way:99','kind':'road_axis','line_index':0,'segment_index':0})
    assert result['street_axis_ray_intersections']==[{'candidate_id':'osm:way:2','ray_distance_m':70.}]
    assert result['street_axis_scenario']['observed_segment_bearing_degrees']==90
    assert result['street_axis_scenario']['heading_difference_degrees']==0
    assert result['street_axis_scenario']['camera_to_observed_axis_segment_m']==0
    assert result['objects'][0]['candidate_id']=='osm:way:1'  # Ray measurement did not promote/select identity.


@pytest.mark.parametrize('ids,pose',[
    ([],None),(['osm:way:missing'],None),(['osm:way:1']*2,None),([[]],None),
    (['osm:way:1'],{'east_m':True}),(['osm:way:1'],{'heading_degrees':float('nan')}),
    (['osm:way:1'],['not a pose']),
])
def test_unavailable_or_invalid_relational_requests_return_no_invented_measurements(ids,pose):
    assert measure_spatial_relations(story([building(1,10,10)]),[],ids,pose=pose) is None
