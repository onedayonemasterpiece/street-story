"""Independent SOURCE+MAP packet must stay comprehensive and reasonably small."""
import copy
import json

from street_story.identity_source_map_prompt import compact_source_map_visual_prompt


def test_spatial_packet_is_faithful_without_regional_or_wikipedia_corpus():
    scene = {'objects': {'columns': ['label', 'candidate_id'], 'rows': [
        [1, 'osm:way:102'], [2, 'osm:way:103'], [3, 'osm:way:104']]},
        'physical_bodies': {'columns': ['label', 'candidate_id', 'observed_side_segments'],
            'rows': [[1, 'osm:way:102', [[0, 3], [0, 4]]],
                     [2, 'osm:way:103', [[0, 1]]],
                     [3, 'osm:way:104', [[0, 2], [0, 8]]]]},
        'coverage': {'completeness': 'unknown'}, 'source_angular_reference': {'diagonal_fov_35mm_deg': 84.1}}
    packet = {'encoding': 'lossless-literals-and-map-labels-v1',
        'literals': ['osm:way:102', 'literal entrance 22А', 'unknown yaw'],
        'map_scene': scene, 'camera_hints': {'direction_status': 'missing'},
        'location_search_context': {'observed_localities': ['Калининград']},
        'wikipedia_metadata': [{'pageid': i, 'extract': 'Irrelevant encyclopedia text'*100} for i in range(25)],
        'regional_catalogue': {'results': [{'metadata_excerpt': 'Archive text'*200} for _ in range(30)]},
        'regional_source_profile': {'observed_localities': ['Калининград']}}
    before = copy.deepcopy(packet)
    prompt = compact_source_map_visual_prompt(packet)
    supplied = json.loads(prompt.split('Observed spatial context (lossless map label/literal references):\n', 1)[1])
    assert packet == before
    assert supplied['map_scene'] == scene
    assert supplied['literals'] == packet['literals']
    assert supplied['camera_hints'] == packet['camera_hints']
    assert len(supplied['map_scene']['objects']['rows']) == 3
    assert 'wikipedia_metadata' not in supplied and 'regional_catalogue' not in supplied
    assert supplied['external_ref_in_this_visual_send'] is False
    assert len(prompt.encode()) < len(json.dumps(packet, ensure_ascii=False).encode()) / 3


def test_lean_route_fails_closed_without_received_map():
    import pytest
    with pytest.raises(ValueError, match='source_map_scene_required'):
        compact_source_map_visual_prompt({})


def test_singleton_structural_pair_normalizes_without_changing_evidence():
    from street_story.identity_source_map_prompt import normalize_source_map_visual_result
    source = {
        'accepted_geometry': {
            'decision': 'accepted_geometry', 'candidate_id': '@381',
            'spatial_correspondence': {
                'pattern_kind': 'corner',
                'front_segments': {
                    'first': {'candidate_id': '@381', 'kind': 'segment', 'ring_index': 0, 'segment_index': 1},
                    'second': {'candidate_id': '@381', 'kind': 'segment', 'ring_index': 0, 'segment_index': 2}},
                'pose': {'east_m': 0, 'north_m': 0, 'heading_degrees': 180}}},
        'other': [{'name': 'unrelated fact, never reformatted'}]}
    before = copy.deepcopy(source)
    normalized, changes = normalize_source_map_visual_result(source)
    assert source == before
    assert len(changes) == 1
    assert normalized['accepted_geometry']['candidate_id'] == '@381'
    assert normalized['accepted_geometry']['spatial_correspondence']['front_segments'] == [
        source['accepted_geometry']['spatial_correspondence']['front_segments']]
    assert normalized['other'] == source['other']
    assert normalize_source_map_visual_result(normalized) == (normalized, [])
    ambiguous = copy.deepcopy(source)
    ambiguous['accepted_geometry']['spatial_correspondence']['front_segments'] = {
        'first': {'candidate_id': '@381'}, 'reason': 'incomplete'}
    assert normalize_source_map_visual_result(ambiguous) == (ambiguous, [])
