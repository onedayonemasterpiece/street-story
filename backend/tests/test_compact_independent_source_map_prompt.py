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
    assert 'external REF is attached' not in prompt
    assert len(prompt.encode()) < len(json.dumps(packet, ensure_ascii=False).encode()) / 3


def test_lean_route_fails_closed_without_received_map():
    import pytest
    with pytest.raises(ValueError, match='source_map_scene_required'):
        compact_source_map_visual_prompt({})
