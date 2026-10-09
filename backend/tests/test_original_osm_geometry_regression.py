"""G-only regression against verbatim retained OSM geometries of owner originals.

These explicit IDs are posthoc test oracles; do not supply this fixture to an
inference call or choose runtime candidates by these IDs. No external REF,
Prussia article, OSM network requests, or model inference occurs here.
"""
import json
import math
from pathlib import Path

import pytest

from street_story.identity_map_context import geometry_camera_context
from street_story.identity_model_context import physical_decision_context
from street_story.identity_scene import render_scene
from street_story.identity_spatial_features import (
    _local, _point, _distance, geometry_feature, measure_spatial_relations,
)


CASES = {item['case']:item for item in json.loads(
    (Path(__file__).parent / 'fixtures/g_original_osm_geometry.json').read_text())['cases']}


def story_for(case_number):
    item = CASES[case_number]
    camera = item['camera']
    assert camera['status'] in {'original_exif', 'owner_approximate'}
    latitude, longitude = camera['latitude'], camera['longitude']
    observed = [{**entry, **(geometry_camera_context(entry, latitude, longitude)
        if entry.get('geometry') else {})} for entry in item['osm_observed']]
    story = {'latitude':latitude, 'longitude':longitude,
        '_identity_map_snapshot':{'observed_pool':observed},
        '_camera_position_verified':camera['status']=='original_exif',
        '_location_provenance': {'kind':'owner_approx_camera'}
            if camera['status']=='owner_approximate' else {},
        '_camera_hints': {'diagonal_fov_35mm_deg':item['source_fov_nominal_deg']}}
    return story


def test_original_102_opaque_corner_requires_real_join_not_any_two_long_facades():
    story = story_for(102)
    context = physical_decision_context(story, [], render_scene(story,[])['manifest'])
    table = {r[1]:dict(zip(context['columns'],r)) for r in context['rows']}
    nominated = table['osm:way:133035113']
    assert nominated['boundary_distance_m'] == pytest.approx(21.1,abs=0.2)
    connected = nominated['observed_connected_side_pairs']
    assert any(pair[1:3] == [2,3] for pair in connected)
    assert not any(pair[1:3] == [5,2] for pair in connected)
    wrong_ends = [geometry_feature(story, [], {
        'candidate_id':'osm:way:133035113','kind':'segment',
        'ring_index':0,'segment_index':i}) for i in (5,2)]
    assert all(wrong_ends)
    assert not any(a==b for a in wrong_ends[0]['coordinates']
        for b in wrong_ends[1]['coordinates'])
    assert context['received_body_count']==4  # two independent blind model nominees now retained


def test_original_106_neighbour_setback_and_real_gap_without_invented_passage():
    story = story_for(106)
    names = ['osm:way:150596899','osm:way:150596903',
             'osm:way:102519047','osm:way:150596900']
    meta = {entry['candidate_id']:entry for entry in
        __import__('street_story.identity_scene', fromlist=['scene_entries']).scene_entries(story,[])}
    assert meta[names[0]]['map_object']['tags']['building:levels']=='3'
    assert meta[names[2]]['map_object']['tags']['building:levels']=='2'
    result=measure_spatial_relations(story,[],names[:2],
        pose={'east_m':0.,'north_m':0.,'heading_degrees':77.},front_segments=[])
    assert result is not None
    assert result['left_to_right_order']==names[:2]
    assert [row['relative_bearing_degrees'] for row in result['objects']] == pytest.approx(
        [-39.55,38.91],abs=1.5)
    gap=result['boundary_gaps'][0]
    assert gap['observed_boundary_gap_m']==pytest.approx(0.3,abs=0.2)
    assert gap['physical_passage_verified'] is False
    context=physical_decision_context(story,[],render_scene(story,[])['manifest'])
    d={r[1]:dict(zip(context['columns'],r)) for r in context['rows']}
    assert d[names[0]]['boundary_distance_m']==pytest.approx(10.6,abs=.3)
    assert len(d[names[0]]['observed_connected_side_pairs'])>=1
    assert context['received_body_count']==5  # includes the real distant negative from a closed G run


def test_original_132_actual_approach_axis_first_hit_is_directional_not_nearest():
    story=story_for(132)
    assert story['_camera_position_verified'] is False
    origin=(story['latitude'],story['longitude'])
    ref={'candidate_id':'osm:way:67826885','kind':'road_axis',
         'line_index':0,'segment_index':0}
    axis=geometry_feature(story,[],ref)
    assert axis is not None
    a,b=[_local(_point(p),origin) for p in axis['coordinates']]
    bearing=math.degrees(math.atan2(b[0]-a[0],b[1]-a[1]))%360
    assert _distance((0.,0.),a,b)==pytest.approx(4.67,abs=0.1)
    cases=[]
    for heading in (bearing,(bearing+180)%360):
        measured=measure_spatial_relations(story,[],
            ['osm:way:192217077'],pose={'east_m':0.,'north_m':0.,
            'heading_degrees':heading},street_axis=ref)
        assert measured is not None
        cases.append((measured['street_axis_ray_intersections'],
            measured['objects'][0]['relative_bearing_degrees']))
    # Both possible road directions were actually computed. Only the
    # image-using LLM can decide which direction the SOURCE faces.
    assert {hits[0]['candidate_id'] for hits,_ in cases}=={
        'osm:way:192217077','osm:way:192220354'}
    toward=next((hits,angle) for hits,angle in cases
        if hits[0]['candidate_id']=='osm:way:192217077')
    assert abs(toward[1])<90
    assert toward[0][0]['ray_distance_m']==pytest.approx(70.16,abs=2)
    ctx=physical_decision_context(story,[],render_scene(story,[])['manifest'])
    assert ctx['received_body_count']==2  # school + western competitor; street isn't a building


def test_original_111_telephoto_target_is_not_removed_by_distance_or_fov():
    story=story_for(111)
    assert story['_camera_hints']['diagonal_fov_35mm_deg']==10.3
    view=render_scene(story,[])
    assert view and view['manifest']['camera']['position_status']=='original_exif'
    context=physical_decision_context(story,[],view['manifest'])
    rows={r[1]:dict(zip(context['columns'],r)) for r in context['rows']}
    target=rows['osm:way:95290265']
    assert target['boundary_distance_m']==pytest.approx(188.8,abs=0.4)
    assert context['received_body_count']==1
    assert isinstance(target['omitted_side_count'],int)
    expanded=render_scene(story,[],detail_candidate_ids=['osm:way:95290265'])
    assert expanded
    after=physical_decision_context(story,[],expanded['manifest'])
    assert after['received_body_count']==1
    assert len(after['rows'][0][after['columns'].index('observed_side_segments')])>=4
    assert (target['outline_span_over_exif_diagonal'] is None or
        isinstance(target['outline_span_over_exif_diagonal'],float))


@pytest.mark.parametrize('case_number', [126,130])
def test_originals_without_gps_preserve_owner_approximation(case_number):
    story=story_for(case_number)
    assert not story['_camera_position_verified']
    scene=render_scene(story,[])
    assert scene
    camera=scene['manifest']['camera']
    assert camera['position_status']=='owner_approximate'
    assert camera['heading_status']=='missing'
    assert camera['accuracy_m'] is None


def test_original_132_source_free_bidirectional_road_cues_expose_both_physical_futures():
    story=story_for(132)
    scene=render_scene(story,[])
    context=physical_decision_context(story,[],scene['manifest'])
    cues=context['bidirectional_road_axis_cues']
    assert cues['camera_basis']=='owner_approximate'
    assert cues['observed_road_count']==1  # This bounded raw-OSM fixture retains one road axis.
    nearest=next((row for row in cues['rows'] if row[0]=='osm:way:67826885'),None)
    assert nearest is not None
    assert nearest[4]==pytest.approx(4.67,abs=.1)
    assert len(nearest[6])==2
    assert nearest[7]  # literal OSM highway type, not inferred street class
    directional=[row[1] for row in nearest[6]]
    firsts={direction[0][1] for direction in directional if direction}
    # Neither the correct-facing road nor its opposite is chosen by the host;
    # both geometries are available to the SOURCE-using LLM.
    assert 'osm:way:192217077' in firsts
    assert 'osm:way:192220354' in {
        hit[1] for direction in directional for hit in direction}
    assert context['received_body_count']==2


def test_search_context_is_not_a_fake_camera_heading_or_road_hit_oracle():
    story=story_for(132)
    story['_location_provenance']={}
    scene=render_scene(story,[])
    assert scene['manifest']['camera']['position_status']=='search_context'
    cues=physical_decision_context(story,[],scene['manifest'])['bidirectional_road_axis_cues']
    assert cues['rows']==[]
    assert cues['camera_basis']=='search_context'
