import copy

import pytest

from street_story.identity_model_context import physical_decision_context, physical_overview_context
from street_story.identity_scene import render_scene
from street_story.identity_spatial_features import observed_boundary_view
from test_identity_spatial_features import building, point, story


def test_rectangle_projection_distinguishes_near_sides_from_self_occluded_far_sides():
    entry = building(813, 20, 10)
    original = copy.deepcopy(entry)
    view = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    assert view['anchor_relation'] == 'outside'
    assert [row[-1] for row in view['sides']] == [False, True, True, False]
    assert all(0 < row[4] < 90 for row in view['sides'])
    assert view['pose_scenario']['is_measured_camera_pose'] is False
    assert 'heading_degrees' not in view['pose_scenario']
    assert entry == original


@pytest.mark.parametrize('offset, relation', [((0, 0), 'outside'), ((25, 15), 'inside'),
                                           ((20, 15), 'on_boundary')])
def test_explicit_offset_is_a_scenario_not_a_measured_camera(offset, relation):
    view = observed_boundary_view(building(17, 20, 10), {'latitude': 55, 'longitude': 21},
        pose={'east_m': offset[0], 'north_m': offset[1]})
    assert view['anchor_relation'] == relation
    assert view['pose_scenario'] == {'east_m': offset[0], 'north_m': offset[1],
                                    'is_measured_camera_pose': False}


def test_courtyard_anchor_is_outside_building_material_and_closed_hole_is_preserved():
    entry = {'map_geometry': {'rings': [
        {'role': 'outer', 'closed': True, 'points': [point(-20, -20), point(20, -20),
            point(20, 20), point(-20, 20), point(-20, -20)]},
        {'role': 'inner', 'closed': True, 'points': [point(-5, -5), point(5, -5),
            point(5, 5), point(-5, 5), point(-5, -5)]}]}}
    view = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    assert view['anchor_relation'] == 'outside'
    assert [row[-1] for row in view['sides'][:4]] == [True] * 4
    assert [row[-1] for row in view['sides'][4:]] == [False] * 4


def test_open_or_invalid_geometry_does_not_invent_closure_or_visibility():
    entry = {'map_geometry': {'rings': [{'role': 'outer', 'closed': False,
        'points': [point(10, 10), point(20, 10), point(20, 20)]}]}}
    view = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    assert view['anchor_relation'] == 'unknown'
    assert all(row[-1] is None for row in view['sides'])
    assert observed_boundary_view(entry, {'latitude': None, 'longitude': None}) is None
    assert observed_boundary_view(entry, {'latitude': 55, 'longitude': 21},
                                  pose={'east_m': float('nan')}) is None
    assert observed_boundary_view(entry, {'latitude': 55, 'longitude': 21}, pose=[]) is None


def test_invalid_other_ring_and_camera_on_vertex_leave_projection_unknown():
    entry = {'map_geometry': {'rings': [
        {'role': 'outer', 'closed': True, 'points': [point(0, 0), point(20, 0),
            point(20, 20), point(0, 20), point(0, 0)]},
        {'role': 'inner', 'closed': True, 'points': [{'lat': None, 'lon': None}]}]}}
    view = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    assert view['anchor_relation'] == 'unknown'
    assert all(row[-1] is None for row in view['sides'])
    assert view['sides'][0][2] is None and view['sides'][0][4] is None


def test_crossing_north_has_short_angular_span_and_reversed_vertices_preserve_rays():
    entry = building(99, -10, 20)
    first = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    entry['geometry'].reverse()
    second = observed_boundary_view(entry, {'latitude': 55, 'longitude': 21})
    assert first['sides'][0][4] < 60
    assert sorted((row[4], row[5]) for row in first['sides']) == sorted(
        (row[4], row[5]) for row in second['sides'])


@pytest.mark.parametrize('ids', [(813, 17), (17, 813)])
def test_only_requested_details_receive_bound_profiles_without_excluding_other_bodies(ids):
    source = story([building(ids[0], 20, 10), building(ids[1], -40, -20)],
                   _location_provenance={'kind': 'owner_approx_camera'})
    original = copy.deepcopy(source)
    requested = 'osm:way:813'
    scene = render_scene(source, [], detail_candidate_ids=[requested])
    context = physical_decision_context(source, [], scene['manifest'])
    rows = {row['candidate_id']: row for row in
            (dict(zip(context['columns'], values)) for values in context['rows'])}
    assert set(rows) == {'osm:way:813', 'osm:way:17'}
    assert rows[requested]['conditional_boundary_view'] is not None
    assert rows['osm:way:17']['conditional_boundary_view'] is None
    profile = rows[requested]['conditional_boundary_view']
    assert {tuple(row[:2]) for row in profile['sides']} == {
        tuple(row[:2]) for row in rows[requested]['observed_side_segments']}
    overview = physical_overview_context(context)
    assert 'conditional_boundary_view' not in overview['columns']
    assert len(overview['rows']) == 2
    assert source == original


def test_geocoded_search_anchor_does_not_become_a_camera_projection():
    source = story([building(813, 20, 10)], _location_provenance={'kind': 'owner_live_place_query'})
    scene = render_scene(source, [], detail_candidate_ids=['osm:way:813'])
    context = physical_decision_context(source, [], scene['manifest'])
    index = context['columns'].index('conditional_boundary_view')
    assert context['rows'][0][index] is None
