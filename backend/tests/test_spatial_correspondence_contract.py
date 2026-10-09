import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context, identity_discovery
from street_story.identity_geometry_contract import CONTRACT
from street_story.identity_model_context import physical_decision_context
from street_story.identity_proof import freeze_geometry_proof, geometry_result_valid
from street_story.identity_scene import render_scene
from test_geometry_identity_plan import Executor, geometry_decision, geometry_setup, payload


def receipt_for(story, active):
    scene = render_scene(story, active)
    return {'joint_image_input': True, 'source_photo_sha256': story['photo_sha256'],
        'original_source_sha256': story['photo_sha256'], 'model_source_sha256': story['photo_sha256'],
        'map_image_sha256': scene['manifest']['image_sha256'], 'manifest': scene['manifest'],
        'geometry_contract': CONTRACT, 'physical_body_candidate_ids': ['osm:way:2', 'osm:way:3']}


def test_current_geometry_requires_real_adjacent_sides_horizontal_pose_and_uncertainty(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    receipt = receipt_for(story, active)
    decision = geometry_decision()
    proof = freeze_geometry_proof(story, decision, receipt, active)
    assert proof
    assert abs(proof['spatial_correspondence']['measurements'][0]['front_segments'][0]['side_angle_difference_degrees']) == 90
    raw = {'status': 'match', 'candidate_id': 'osm:way:2', 'proof_kind': 'geometry', 'geometry_proof': proof}
    assert geometry_result_valid(raw, active, story)
    bad = copy.deepcopy(decision)
    bad.pop('spatial_correspondence')
    bad['camera_pose'] = {'position_basis': 'GPS', 'yaw_basis': 'снизу вверх', 'sensitivity': 'детализация'}
    assert freeze_geometry_proof(story, bad, receipt, active) is None
    for change in ('same_pose', 'non_adjacent', 'missed_prior', 'invented_side'):
        bad = copy.deepcopy(decision)
        current_receipt = copy.deepcopy(receipt)
        value = bad['spatial_correspondence']
        if change == 'same_pose':
            value['uncertainty_scenarios'][0]['pose'] = value['pose']
        elif change == 'non_adjacent':
            value['front_segments'][0]['second']['segment_index'] = 2
        elif change == 'invented_side':
            value['front_segments'][0]['second']['segment_index'] = 99
        else:
            bad['rejected_alternatives'] = []
            current_receipt['material_alternative_candidate_ids'] = ['osm:way:2', 'osm:way:3']
        assert freeze_geometry_proof(story, bad, current_receipt, active) is None
    bad = copy.deepcopy(raw)
    bad['geometry_proof']['spatial_correspondence']['measurements'][0]['objects'][0]['boundary_distance_m'] = 0
    assert not geometry_result_valid(bad, active, story)


def test_legacy_frozen_geometry_keeps_its_original_contract_without_relabelling(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    receipt = receipt_for(story, active)
    receipt.pop('geometry_contract')
    decision = geometry_decision()
    decision.pop('spatial_correspondence')
    proof = freeze_geometry_proof(story, decision, receipt, active)
    assert proof
    raw = {'status': 'match', 'candidate_id': 'osm:way:2', 'proof_kind': 'geometry', 'geometry_proof': proof}
    assert geometry_result_valid(raw, active, story)
    upgraded_receipt = {**receipt, 'geometry_contract': CONTRACT}
    assert freeze_geometry_proof(story, decision, upgraded_receipt, active) is None


def test_current_architectural_proof_uses_individual_structure_not_three_style_or_color_votes():
    from street_story.identity_proof import TEXT_CONTRACT, freeze_architectural_text_proof
    from test_architectural_text_identity import text_inputs
    story, candidates, decision, receipt = text_inputs()
    receipt['text_contract'] = TEXT_CONTRACT
    assert freeze_architectural_text_proof(story, decision, receipt, candidates)
    for feature in ('color_or_finish', 'generic_style', 'historical_fact'):
        bad = copy.deepcopy(decision)
        bad['correspondences'] *= 3
        for correspondence in bad['correspondences']:
            correspondence['feature_kind'] = feature
        assert freeze_architectural_text_proof(story, bad, receipt, candidates) is None


@pytest.mark.asyncio
async def test_ready_source_map_does_not_wait_for_cold_catalogue_and_owned_read_is_drained(tmp_path, monkeypatch):
    service, story, active = geometry_setup(tmp_path)
    started, drained = asyncio.Event(), asyncio.Event()

    async def cold(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def generate(key, timeout, contents, config, **kwargs):
        assert started.is_set() and not drained.is_set()
        assert len(contents) == 3
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))

    monkeypatch.setattr(identity_architectural_context, 'prepare_regional_catalogue', cold)
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    # Watchdog for the impossible catalogue barrier, not a CPU speed assertion.
    await asyncio.wait_for(identity_discovery.suggest(service, story, '', active), 60)
    assert drained.is_set() and '_identity_regional_catalogue_task' not in story
    assert story['_identity_search_plan_payload']['geometry_proof']


def test_physical_context_keeps_all_bodies_and_literal_entry_provenance_without_mutating_map(tmp_path):
    _service, story, active = geometry_setup(tmp_path)
    original = copy.deepcopy(story)
    scene = render_scene(story, active)
    context = physical_decision_context(story, active, scene['manifest'])
    assert {row[1] for row in context['rows']} == {'osm:way:2', 'osm:way:3'}
    assert context['received_body_count'] == 2
    assert 'osm:way:9' not in json.dumps(context['rows'])
    # This synthetic catalog has not acquired provider boundary/sector fields;
    # presentation must preserve unknowns rather than infer them from centers.
    assert all(row[3] is None and row[4] == [None, None, None] for row in context['rows'])
    assert story == original
    assert hashlib.sha256(scene['bytes']).hexdigest() == scene['manifest']['image_sha256']
