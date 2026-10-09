"""G model interprets SOURCE; OSM code only binds referenced geometry.

Regression against ORIGINAL owner OSM fixtures, with response fixtures authored
for contract mechanics only. No owner expected identity is ever sent to an
inference model, and a synthetic acceptance is not a live recognition result.
"""
import json

import pytest

from street_story.identity_model_context import physical_decision_context
from street_story.identity_scene import render_scene
from street_story.identity_spatial_choice import (
    check_spatial_choice, visual_spatial_choice_schema)
from street_story.identity_spatial_options import (
    for_vision, spatial_option_catalog)
from test_original_osm_geometry_regression import story_for

SHA = 'a'*64


def scene_and_packet(case):
    story=story_for(case)
    manifest=render_scene(story,[])['manifest']
    physical=physical_decision_context(story,[],manifest)
    packet=spatial_option_catalog(story,[],manifest,physical)
    return story,manifest,physical,packet


def check(response,packet,*,map_hash=None):
    return check_spatial_choice(response,packet,
        source_sha256=SHA,model_source_sha256='b'*64,
        actual_source_sha256=SHA,
        actual_map_sha256=map_hash or packet['map_sha256'])


def opinion(label,decision='accept',selected=None,alternatives=None,observations=None):
    return {'decision':decision,'candidate_label':label,
        'source_pattern':'single_frontage','crop_scope':'whole',
        'source_observations':(['Visible building front matches the mapped facade']
             if observations is None else observations),
        'selected_option_ids':selected or [],
        'contrasted_alternatives':alternatives or [],
        'request_detail_labels':[],
        'uncertainties':[]}


@pytest.mark.parametrize('case', [102,106,111,132])
def test_real_maps_first_pass_compact_without_semantic_k_shortlist(case):
    story,manifest,physical,packet=scene_and_packet(case)
    assert packet['presentation_stage']=='source_map_overview'
    assert packet['expanded_labels']==[]
    assert packet['physical_body_count']==physical['received_body_count']
    assert len(packet['all_received_physical_bodies'])==physical['received_body_count']
    # Neutral MAP also labels roads and other non-building objects.
    assert len(packet['private_label_to_osm_id'])>=physical['received_body_count']
    assert len(json.dumps(for_vision(packet),ensure_ascii=False).encode())<14000
    assert 'private_label_to_osm_id' not in for_vision(packet)
    assert packet['options'] or packet['physical_body_count']>0


def test_single_visible_facade_accepts_without_pretend_yaw_or_three_alternatives():
    story,manifest,physical,packet=scene_and_packet(106)
    cid=next(row[1] for row in physical['rows']
         if row[1]=='osm:way:150596899')
    label=next(int(k) for k,v in packet['private_label_to_osm_id'].items() if v==cid)
    first=check(opinion(label),packet)
    assert first['status']=='needs_detail'
    assert first['candidate_id']==cid
    assert first['requested_detail_labels']==[label]
    assert not first['accepted']
    # No arbitrary top-N heuristic; expand exactly the LLM-selected received
    # physical body, never any test expected identity in real inference.
    detail=spatial_option_catalog(story,[],manifest,physical,
          focus_candidate_ids=[first['candidate_id']])
    frontage=next(key for key,val in detail['options'].items()
          if val['kind']=='single_frontage' and val['body_label']==label)
    response=opinion(label,selected=[frontage])
    response['source_observations']=['An observed single street facade matches this mapped wall.']
    response.pop('contrasted_alternatives')
    response.pop('request_detail_labels')
    response.pop('crop_scope')
    assert not {'heading_degrees','pose','pitch','year'} & set(json.dumps(response).split())
    assert set(visual_spatial_choice_schema()['required'])=={
        'decision','candidate_label','source_observations'}
    validated=check(response,detail)
    assert validated['status']=='accepted_geometry_v3'
    assert validated['accepted'] is True
    assert validated['proof']['candidate_id']==cid
    assert validated['proof']['chosen_measured_options'][frontage]['kind']=='single_frontage'


def test_partial_upper_only_and_missing_alternatives_are_model_semantics_not_hard_veto():
    story,manifest,physical,packet=scene_and_packet(111)
    cid='osm:way:95290265'
    label=next(int(k) for k,v in packet['private_label_to_osm_id'].items() if v==cid)
    detail=spatial_option_catalog(story,[],manifest,physical,
        focus_candidate_ids=[cid])
    measured=next(k for k,v in detail['options'].items()
       if v['body_label']==label and v['kind']=='plan_shape')
    response=opinion(label,selected=[measured])
    response['source_pattern']='partial_complex'
    response['crop_scope']='partial'
    response['source_observations']=['Only an upper building volume is visible.']
    outcome=check(response,detail)
    assert outcome['accepted'] is True
    assert outcome['geometric_warnings']==[]
    assert outcome['proof']['model_crop_scope']=='partial'


def test_modelled_uncertainty_stays_useful_with_no_fake_acceptance():
    story,manifest,physical,packet=scene_and_packet(132)
    decision=opinion(0,decision='unknown',observations=[])
    decision['source_pattern']='unknown'
    out=check(decision,packet)
    assert out['status']=='unknown'
    assert out['candidate_id'] is None
    assert out['accepted'] is False
    label=packet['all_received_physical_bodies'][0][0]
    undecided=check(opinion(label,'candidate',observations=[
        'Only a plausible outline is visible; cannot distinguish the physical scope.']),packet)
    assert undecided['status']=='candidate_unconfirmed'
    assert undecided['candidate_id'] is not None
    assert undecided['requested_detail_labels']==[label]
    assert not undecided['accepted']


def test_impossible_map_reference_and_mismatched_image_remain_hard_invalid():
    story,manifest,physical,packet=scene_and_packet(102)
    label=packet['all_received_physical_bodies'][0][0]
    detail=spatial_option_catalog(story,[],manifest,physical,
        focus_candidate_ids=[packet['private_label_to_osm_id'][str(label)]])
    bad=check(opinion(label,selected=['C999999.0.9.10']),detail)
    assert not bad['accepted']
    assert bad['status']=='candidate_unconfirmed'
    assert 'unreceived_osm_option_reference' in bad['reason_codes']
    assert check(opinion(label),packet,map_hash='0'*64)['status']=='invalid'
    forged=check(opinion(123456789),packet)
    assert forged['status']=='invalid'
