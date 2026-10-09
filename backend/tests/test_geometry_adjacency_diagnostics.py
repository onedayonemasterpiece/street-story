"""G-only: real segment joins, strict proof and honest conditional nominations."""
import copy

from street_story.identity_corner_context import (
    observed_connected_pairs, preserve_one_connected_pair,
)
from street_story.identity_geometry_diagnostics import diagnose_geometry_nomination
from street_story.identity_model_context import physical_decision_context
from street_story.identity_proof import freeze_geometry_proof
from street_story.identity_scene import render_scene
from test_geometry_identity_plan import geometry_decision, geometry_setup
from test_spatial_correspondence_contract import receipt_for


def side(i, a, b, length):
    return [0, i, length, list(a), list(b)]


def test_connected_pairs_are_real_segments_and_never_fake_nonadjacent_corners():
    segments = [
        side(5, (0, 3), (0, 0), 3), side(2, (3, 3), (0, 3), 3),
        side(3, (0, 3), (0, 5), 2), side(4, (0, 5), (0, 3), 2)]
    pairs, missing = observed_connected_pairs(segments)
    assert missing == 0
    assert [0, 5, 2] not in [entry[:3] for entry in pairs]
    assert [0, 2, 3] in [entry[:3] for entry in pairs]
    assert [0, 3, 4] in [entry[:3] for entry in pairs]
    assert all(-180 <= p[3] <= 180 for p in pairs)


def test_missing_connected_excerpt_recovers_one_real_pair_not_a_fake_footprint():
    # Four longest sides can be mutually disconnected and omit a short return.
    full = [
        side(0, (0, 0), (100, 0), 100), side(1, (100, 0), (100, 1), 1),
        side(2, (100, 1), (10, 1), 90), side(3, (10, 1), (10, 2), 1),
        side(4, (10, 2), (90, 2), 80), side(5, (90, 2), (90, 3), 1),
        side(6, (90, 3), (20, 3), 70)]
    shown = [full[i] for i in (0, 2, 4, 6)]
    assert not observed_connected_pairs(shown)[0]
    improved = preserve_one_connected_pair(full, shown)
    assert len(improved) == 4 and len({(s[0], s[1]) for s in improved}) == 4
    assert observed_connected_pairs(improved)[0]
    assert all(s in full for s in improved)
    assert full == [side(0, (0, 0), (100, 0), 100),
        side(1, (100, 0), (100, 1), 1), side(2, (100, 1), (10, 1), 90),
        side(3, (10, 1), (10, 2), 1), side(4, (10, 2), (90, 2), 80),
        side(5, (90, 2), (90, 3), 1), side(6, (90, 3), (20, 3), 70)]


def test_model_context_keeps_all_bodies_and_adjacency_has_observed_indices(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    original = copy.deepcopy(story)
    scene = render_scene(story, active)
    context = physical_decision_context(story, active, scene['manifest'])
    rows = [dict(zip(context['columns'], row)) for row in context['rows']]
    assert len(rows) == context['received_body_count'] == 2
    assert context['connected_pair_columns'] == [
        'ring_index', 'first_segment_index', 'second_segment_index', 'observed_turn_degrees']
    for row in rows:
        shown = row['observed_side_segments']
        pair_ids = {(p[0],p[1],p[2]) for p in row['observed_connected_side_pairs']}
        assert pair_ids.issubset({tuple(p[:3]) for p in observed_connected_pairs(shown)[0]})
        assert row['omitted_side_count'] >= 0
    assert story == original


def test_model_nominated_but_unproven_corner_survives_as_conditional(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    receipt = receipt_for(story, active)
    good = geometry_decision()
    assert freeze_geometry_proof(story, good, receipt, active)
    accepted = diagnose_geometry_nomination(story, good, receipt, active)
    assert accepted['status'] == 'accepted_geometry'
    assert accepted['candidate_id'] == good['candidate_id']
    assert accepted['authorizes_identity'] is True

    bad = copy.deepcopy(good)
    bad['spatial_correspondence']['front_segments'][0]['second']['segment_index'] = 2
    assert freeze_geometry_proof(story, bad, receipt, active) is None
    evidence = diagnose_geometry_nomination(story, bad, receipt, active)
    assert evidence['status'] == 'conditional_physical_nomination'
    assert evidence['candidate_id'] == good['candidate_id']
    assert evidence['authorizes_identity'] is False
    assert 'corner_segments_do_not_share_vertex' in evidence['reason_codes']


def test_false_yaw_repeated_pose_and_unreceived_labels_never_become_identity(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    receipt = receipt_for(story, active)
    good = geometry_decision()
    duplicate = copy.deepcopy(good)
    duplicate['spatial_correspondence']['uncertainty_scenarios'][0]['pose'] = (
        duplicate['spatial_correspondence']['pose'])
    assert not freeze_geometry_proof(story, duplicate, receipt, active)
    diag = diagnose_geometry_nomination(story, duplicate, receipt, active)
    assert 'repeated_nominal_pose' in diag['reason_codes']
    assert diag['authorizes_identity'] is False

    behind = copy.deepcopy(good)
    behind['spatial_correspondence']['pose']['heading_degrees'] = 240
    assert not freeze_geometry_proof(story, behind, receipt, active)
    diag = diagnose_geometry_nomination(story, behind, receipt, active)
    assert 'nominated_body_outside_forward_view' in diag['reason_codes']

    forged = copy.deepcopy(good)
    forged['candidate_label'] = 2
    evidence = diagnose_geometry_nomination(story, forged, receipt, active)
    assert evidence['status'] == 'no_grounded_nomination'
    assert evidence['candidate_id'] is None

    unbound = dict(receipt, map_image_sha256='0'*64)
    result = diagnose_geometry_nomination(story, good, unbound, active)
    assert result['status'] == 'no_grounded_nomination'


def test_explicit_detail_retains_original_ids_and_side_coverage(tmp_path):
    _svc, story, active = geometry_setup(tmp_path)
    original = render_scene(story, active)
    expanded = render_scene(story, active, detail_candidate_ids=['osm:way:2'])
    baseline = physical_decision_context(story, active, original['manifest'])
    detailed = physical_decision_context(story, active, expanded['manifest'])
    assert {r[1] for r in baseline['rows']} == {r[1] for r in detailed['rows']}
    assert any(p[2] for row in detailed['rows'] for p in row[-2])  # actual connected pairs
    assert original['manifest']['objects'] == expanded['manifest']['objects']


def test_rounded_equal_points_do_not_create_nonadjacent_osm_corners():
    # Two very close raw coordinates may round to the same displayed .1m.
    # Their arbitrary equal rounded positions do NOT prove OSM shared vertices.
    a = side(1, (0, 0), (5, 0), 5)
    b = side(8, (5, 0), (5, 4), 4)
    assert observed_connected_pairs([a, b])[0] == []


def test_last_to_first_wrap_requires_original_closed_ring():
    sides = [
        side(0, (0, 0), (6, 0), 6),
        side(1, (6, 0), (6, 4), 4),
        side(2, (6, 4), (0, 4), 6),
        side(3, (0, 4), (0, 0), 4),
    ]
    assert [0, 3, 0] not in [p[:3] for p in observed_connected_pairs(sides)[0]]
    confirmed = observed_connected_pairs(sides, closed_rings={0: 3})[0]
    assert [0, 3, 0] in [p[:3] for p in confirmed]
    assert [0, 1, 2] in [p[:3] for p in confirmed]
