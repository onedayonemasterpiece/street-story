"""Actual SOURCE EXIF camera to original OSM wall-plane verification.

Original photo102 closed model correctly nominated one body but claimed
joined OSM side 0+5 as visible corner; the raw camera position is on the
inward side of edge 5. A real geometric check must retain the candidate but
not certify the visually impossible corner.
"""
import copy
from street_story.identity_camera_visibility import (
    nominal_exterior_sides, observed_corner_halfplane)
from street_story.identity_visual_disagreement import check_visual_disagreement
from street_story.identity_scene import render_scene,scene_entries
from street_story.identity_model_context import physical_decision_context
from test_visual_disagreement import actual_case,proposed


def test_original_102_joined_back_corner_is_not_sufficient_visible_proof():
    story,_scene,_context,labels=actual_case()
    ids=['osm:way:133035111','osm:way:133035113']
    scene=render_scene(story,[],detail_candidate_ids=ids)
    manifest=scene['manifest']
    wrong_pair=observed_corner_halfplane(story,manifest,
        'osm:way:133035113',0,0,5)
    assert wrong_pair['status']=='corner_not_nominally_visible'
    assert wrong_pair['signed_exterior_camera_distances_m'][0]>15
    assert wrong_pair['signed_exterior_camera_distances_m'][1]<-15
    assert wrong_pair['authorizes_identity'] is False
    # Genuine side adjacency is still true, but it points to one nominally
    # back-facing wall. No model yaw is involved.
    context=physical_decision_context(story,[],manifest)
    trial=proposed(labels['osm:way:133035113'])
    trial['first_corner_segment_index']=0
    trial['second_corner_segment_index']=5
    result=check_visual_disagreement(trial,ids,context,manifest,story=story)
    assert result['candidate_id']=='osm:way:133035113'
    assert result['status']=='conditional_physical_nomination'
    assert 'claimed_corner_faces_away_from_nominal_camera' in result['reason_codes']
    assert result['nominal_outer_corner_camera_relation']['camera_position_basis']=='original_exif'
    assert result['authorizes_identity'] is False


def test_original_102_different_real_corner_is_exterior_but_not_identity_proof():
    story,_scene,_ctx,labels=actual_case()
    ids=['osm:way:133035111','osm:way:133035113']
    scene=render_scene(story,[],detail_candidate_ids=ids)
    witness=observed_corner_halfplane(story,scene['manifest'],
        'osm:way:133035113',0,2,3)
    assert witness['status']=='both_walls_nominally_exterior'
    assert all(v>2 for v in witness['signed_exterior_camera_distances_m'])
    assert witness['authorizes_identity'] is False
    ctx=physical_decision_context(story,[],scene['manifest'])
    trial=proposed(labels['osm:way:133035113'])
    trial['first_corner_segment_index']=2
    trial['second_corner_segment_index']=3
    answer=check_visual_disagreement(trial,ids,ctx,scene['manifest'],story=story)
    assert answer['reason_codes']==[]
    assert answer['authorizes_identity'] is False


def test_search_context_must_not_gain_fabricated_camera_wall_visibility():
    story,_scene,_ctx,_labels=actual_case()
    story=copy.deepcopy(story)
    story['_camera_position_verified']=False
    story['_location_provenance']={}
    scene=render_scene(story,[])
    assert scene['manifest']['camera']['position_status']=='search_context'
    entries=scene_entries(story,[])
    entry=next(e for e in entries if e['candidate_id']=='osm:way:133035113')
    data=nominal_exterior_sides(story,scene['manifest'],entry['map_geometry'])
    assert data['outward_segments']==[]
    assert data['inward_segments']==[]
    status=observed_corner_halfplane(story,scene['manifest'],
        'osm:way:133035113',0,2,3)
    assert status['status']=='camera_position_unavailable'


def test_outward_side_context_does_not_remove_bodies_or_infer_gps_precision():
    story,_scene,_ctx,_labels=actual_case()
    scene=render_scene(story,[])
    before=physical_decision_context(story,[],scene['manifest'])
    cols=before['columns']
    assert 'nominal_camera_exterior_side_indices' in cols
    assert 'nominal_camera_inward_side_indices' in cols
    assert before['received_body_count']==4  # frozen test subset, all remain
    assert before['camera_side_halfplane_policy']['epsilon_m_is_not_measured_gps_accuracy']==2.
    rows=[dict(zip(cols,row)) for row in before['rows']]
    target=next(r for r in rows if r['candidate_id']=='osm:way:133035113')
    assert [0,5] in target['nominal_camera_inward_side_indices']
    assert [0,2] in target['nominal_camera_exterior_side_indices']
    assert len(rows)==before['received_body_count']
