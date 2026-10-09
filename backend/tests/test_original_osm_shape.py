"""Photo111 tall/slender SOURCE versus observed OSM-way and large multipart relation.

IDs are posthoc test examples from the original frozen OSM (the two previous
CLOSED visual model nominations). They are never target hints in provider
prompts and cannot make the model choose a house.
"""
import json
import math
from pathlib import Path

import pytest
from google.genai import types

from street_story.identity_geometry_nomination import (
    check_visual_geometry_nomination, visual_geometry_nomination_schema)
from street_story.identity_model_context import physical_decision_context
from street_story.identity_scene import render_scene
from street_story.identity_shape_context import (
    SHAPE_COLUMNS, observed_plan_shape, compare_visual_shape_to_mapped_bodies)
from test_original_osm_geometry_regression import story_for


FIXTURE = json.loads((Path(__file__).parent /
    'fixtures/g_original_111_morphology.json').read_text())
OBJECTS = FIXTURE['received_objects']


def measured(item):
    return dict(zip(SHAPE_COLUMNS,observed_plan_shape(item)))


def original_111_context():
    # The original G corpus already has the real way, and this second object
    # is the exact unrelated relation from a CLOSED wrong blind model answer.
    story=story_for(111)
    relation=next(x for x in OBJECTS if x['type']=='relation')
    story['_identity_map_snapshot']['observed_pool'].append(relation)
    scene=render_scene(story,[])
    ctx=physical_decision_context(story,[],scene['manifest'])
    table={r[1]:dict(zip(ctx['columns'],r)) for r in ctx['rows']}
    return story,scene,ctx,table


def test_real_111_shape_exposes_size_and_complexity_without_imaginary_height():
    way=measured(next(x for x in OBJECTS if x['type']=='way'))
    relation=measured(next(x for x in OBJECTS if x['type']=='relation'))
    assert way['status']=='observed_closed_outer'
    assert way['long_axis_m']==pytest.approx(47.79,abs=.15)
    assert way['short_axis_m']==pytest.approx(28.60,abs=.15)
    assert way['footprint_area_m2']==pytest.approx(963.6,abs=3)
    assert way['footprint_elongation']==pytest.approx(1.67,abs=.03)
    assert way['explicit_height_m'] is None
    assert way['observed_building_levels'] is None
    assert relation['status']=='observed_closed_outer'
    assert relation['long_axis_m']==pytest.approx(112.06,abs=.20)
    assert relation['short_axis_m']==pytest.approx(100.87,abs=.20)
    assert relation['footprint_area_m2']==pytest.approx(5821.3,abs=5)
    assert relation['footprint_area_m2']/way['footprint_area_m2']>6
    assert relation['explicit_height_m']==pytest.approx(49.64,abs=.01)
    assert relation['observed_building_levels']==12.
    assert relation['height_over_long_axis']==pytest.approx(.44,abs=.01)
    assert relation['concave_corner_count']>way['concave_corner_count']
    # Tall source silhouette must not fabricate a numeric height for the WAY.
    assert way['height_over_long_axis'] is None


def test_original_111_multipolygon_gets_real_distance_angle_and_shape():
    story,scene,ctx,byid=original_111_context()
    assert scene['manifest']['camera']['position_status']=='original_exif'
    way=byid['osm:way:95290265']
    rival=byid['osm:relation:19306342']
    assert ctx['received_body_count']==2
    assert rival['boundary_distance_m']==pytest.approx(38.6,abs=.2)
    assert rival['bearing_start_end_span_degrees'][2]==pytest.approx(58.7,abs=.2)
    assert rival['outline_span_over_exif_diagonal']==pytest.approx(5.7,abs=.02)
    assert way['boundary_distance_m']==pytest.approx(188.8,abs=.2)
    assert way['bearing_start_end_span_degrees'][2]==pytest.approx(11.6,abs=.2)
    assert way['outline_span_over_exif_diagonal']==pytest.approx(1.126,abs=.03)
    assert ctx['plan_morphology_columns']==SHAPE_COLUMNS
    assert len(rival['plan_morphology'])==len(SHAPE_COLUMNS)
    assert 'NOT image silhouette' in ctx['plan_morphology_policy']
    # The wrong nearby apartment complex cannot silently replace the far way.
    assert {r[1] for r in ctx['rows']}=={
        'osm:way:95290265','osm:relation:19306342'}


def test_tall_narrow_source_vs_large_rival_is_a_review_signal_not_a_hard_cutoff():
    story,scene,ctx,byid=original_111_context()
    way=byid['osm:way:95290265']
    large=byid['osm:relation:19306342']
    shape=compare_visual_shape_to_mapped_bodies(
        'tall_narrow','upper_or_partial',large['plan_morphology'],[way],
        angular_ratio=large['outline_span_over_exif_diagonal'],
        camera_basis='original_exif')
    assert shape['status']=='measured_plan_advisory'
    assert shape['selected_shape']['explicit_height_m']==49.64
    assert 'selected_plan_much_larger_than_available_narrow_alternative' in shape['warnings']
    assert 'entire_building_source_vs_nominal_fov_extent_conflict' not in shape['warnings']
    assert shape['alternative_shape_comparisons'][0]['selected_area_over_alternative']>6
    assert shape['authorizes_identity'] is False
    full=compare_visual_shape_to_mapped_bodies('tall_narrow','full_building',
       large['plan_morphology'],[way],
       angular_ratio=large['outline_span_over_exif_diagonal'],
       camera_basis='original_exif')
    assert 'entire_building_source_vs_nominal_fov_extent_conflict' in full['warnings']
    assert full['authorizes_identity'] is False
    no_height=compare_visual_shape_to_mapped_bodies('tall_narrow',
        'upper_or_partial',way['plan_morphology'],[large])
    assert 'mapped_vertical_dimension_unknown' in no_height['warnings']
    assert 'selected_plan_much_larger_than_available_narrow_alternative' not in no_height['warnings']


def test_slim_vs_wide_shape_observation_is_optional_old_model_receipts_remain_valid():
    story,scene,ctx,byid=original_111_context()
    labels={r[1]:r[0] for r in ctx['rows']}
    candidate=labels['osm:relation:19306342']
    old={'decision':'nominated','candidate_label':candidate,
       'source_observations':['SOURCE shows a tall slender tower facade'],
       'source_horizontal_extent':'narrow',
       'spatial_relations':[],'alternative_labels':[labels['osm:way:95290265']],
       'uncertainties':['Upper part only visible; heading unknown']}
    assert not list(__import__('jsonschema').Draft202012Validator(
        visual_geometry_nomination_schema()).iter_errors(old))
    prior=check_visual_geometry_nomination(old,scene['manifest'],ctx)
    assert prior['candidate_id']=='osm:relation:19306342'
    assert prior['source_shape_evidence'] is None
    enriched={**old,'source_silhouette_form':'tall_narrow',
       'source_crop_scope':'upper_or_partial',
       'source_shape_observations':['Vertical turret looks narrower than the long apartment block in OSM']}
    updated=check_visual_geometry_nomination(enriched,scene['manifest'],ctx)
    assert updated['candidate_id']==prior['candidate_id']
    assert updated['authorizes_identity'] is False
    assert updated['source_shape_evidence']['alternative_shape_comparisons']
    assert 'selected_plan_much_larger_than_available_narrow_alternative' in (
        updated['source_shape_evidence']['warnings'])
    schema=visual_geometry_nomination_schema()
    provider=types.GenerateContentConfig(response_mime_type='application/json',
        response_json_schema=schema)
    assert provider.model_dump(mode='json',exclude_none=True)['response_json_schema']==schema


@pytest.mark.parametrize('tags,expected',[
    ({'height':'49.64','building:levels':'12'},(49.64,12.)),
    ({'height':'50 m','building:levels':'7.5'},(50.,7.5)),
    ({'height':'160ft','building:levels':'many'},(None,None)),
    ({'height':'unknown','building:levels':'4;5'},(None,None)),
    ({'height':'-1','building:levels':'0'},(None,None)),
])
def test_height_and_floor_tags_are_not_fabricated(tags,expected):
    base=next(x for x in OBJECTS if x['type']=='way')
    val=measured({**base,'tags':tags})
    assert (val['explicit_height_m'],val['observed_building_levels'])==expected


def test_unclosed_and_disjoint_multipolygon_never_invents_one_large_building():
    base=next(x for x in OBJECTS if x['type']=='way')
    without_closure={**base,'geometry':base['geometry'][:-1]}
    assert measured(without_closure)['status']=='incomplete_or_excessive_outer_contour'
    both={**base,'members':[{'type':'way','ref':1,'role':'outer',
        'geometry':base['geometry']}]}
    assert measured(both)['status']=='multipart_or_missing_outer_contour'


@pytest.mark.parametrize('degrees',[0,31,77,123])
def test_oriented_rectangle_ratio_is_nearly_rotation_invariant(degrees):
    angle=math.radians(degrees)
    c,s=math.cos(angle),math.sin(angle)
    corner=[(-25,-5),(25,-5),(25,5),(-25,5),(-25,-5)]
    lat0,lon0=54.7,20.5
    pts=[{'lat':lat0+(x*s+y*c)/111195,
        'lon':lon0+(x*c-y*s)/(111195*math.cos(math.radians(lat0)))}
        for x,y in corner]
    val=measured({'type':'way','id':123,'tags':{'building':'yes'},
        'geometry':pts})
    assert val['status']=='observed_closed_outer'
    assert val['long_axis_m']==pytest.approx(50,abs=.5)
    assert val['short_axis_m']==pytest.approx(10,abs=.5)
    assert val['footprint_elongation']==pytest.approx(5,abs=.18)


def test_original_102_tall_narrow_photo_shape_is_not_a_single_plan_aspect_threshold():
    # These were the TWO separately closed real SOURCE/MAP model nominees.
    # The correct body is from report-only owner acceptance, never from an
    # inference input. Both observed plans are elongated; a hard threshold
    # cannot safely discard one from physical identity.
    story=story_for(102)
    scene=render_scene(story,[])
    ctx=physical_decision_context(story,[],scene['manifest'])
    byid={r[1]:dict(zip(ctx['columns'],r)) for r in ctx['rows']}
    target=byid['osm:way:133035113']
    rival=byid['osm:way:133035111']
    a=dict(zip(SHAPE_COLUMNS,target['plan_morphology']))
    b=dict(zip(SHAPE_COLUMNS,rival['plan_morphology']))
    assert a['status']==b['status']=='observed_closed_outer'
    assert a['long_axis_m']==pytest.approx(33.37,abs=.2)
    assert a['short_axis_m']==pytest.approx(10.93,abs=.2)
    assert b['long_axis_m']==pytest.approx(31.55,abs=.2)
    assert b['short_axis_m']==pytest.approx(13.19,abs=.2)
    assert a['footprint_elongation']==pytest.approx(3.05,abs=.05)
    assert b['footprint_elongation']==pytest.approx(2.39,abs=.05)
    assert a['explicit_height_m'] is None and b['explicit_height_m'] is None
    assert a['footprint_area_m2']<b['footprint_area_m2']
    assert target['outline_span_over_exif_diagonal']==pytest.approx(.423,abs=.005)
    assert rival['outline_span_over_exif_diagonal']==pytest.approx(.138,abs=.005)
    assert ctx['received_body_count']==4


def test_original_102_joined_rear_wall_cannot_certify_a_tall_narrow_visual_candidate():
    story=story_for(102)
    scene=render_scene(story,[],detail_candidate_ids=['osm:way:133035113'])
    ctx=physical_decision_context(story,[],scene['manifest'])
    byid={r[1]:r[0] for r in ctx['rows']}
    label=byid['osm:way:133035113']
    evidence={'decision':'nominated','candidate_label':label,
      'source_observations':['SOURCE shows a tall narrow corner building with a return wall'],
      'source_horizontal_extent':'broad',
      'source_silhouette_form':'tall_narrow',
      'source_crop_scope':'upper_or_partial',
      'source_shape_observations':['Five or more visible facade tiers and a narrow frontage'],
      'spatial_relations':[{
        'kind':'corner',
        'source_observation':'SOURCE reveals two connected building walls at a corner',
        'map_body_labels':[label],
        'road_candidate_id':'','road_direction_index':0,
        'segment_ring_index':0,'first_segment_index':0,'second_segment_index':5}],
      'alternative_labels':[byid['osm:way:133035111']],
      'uncertainties':['Camera yaw missing; OSM outer ring cannot prove SOURCE visibility']}
    result=check_visual_geometry_nomination(evidence,scene['manifest'],ctx)
    assert result['candidate_id']=='osm:way:133035113'
    assert 'model_claimed_corner_includes_nominally_rear_wall' in result['reason_codes']
    assert result['source_shape_evidence']['source_profile']=='tall_narrow'
    assert result['source_shape_evidence']['authorizes_identity'] is False
    assert result['authorizes_identity'] is False
