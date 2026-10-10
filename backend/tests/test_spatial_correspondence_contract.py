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


def test_explicit_far_detail_exposes_short_setback_sides_without_dropping_near_bodies():
    from test_identity_scene import building, point
    far = building(21, 190)
    far['geometry'] = [point(x, y) for x, y in (
        (190, 10), (202, 10), (202, 28), (196, 28), (196, 26), (190, 26), (190, 10))]
    story = {'latitude': 54.7, 'longitude': 20.5,
        '_identity_map_snapshot': {'observed_pool': [building(1, 20), far, building(99, 500)]}}
    original = copy.deepcopy(story)
    initial = render_scene(story, [])
    before = physical_decision_context(story, [], initial['manifest'])
    expanded = render_scene(story, [], detail_candidate_ids=['osm:way:21'])
    after = physical_decision_context(story, [], expanded['manifest'])
    assert {row[1] for row in before['rows']} == {row[1] for row in after['rows']}
    before_far = dict(zip(before['columns'], next(row for row in before['rows'] if row[1] == 'osm:way:21')))
    after_far = dict(zip(after['columns'], next(row for row in after['rows'] if row[1] == 'osm:way:21')))
    assert before_far['observed_side_segments'] == [] and before_far['omitted_side_count'] == 6
    assert after_far['omitted_side_count'] == 0
    assert len(after_far['observed_side_segments']) == 6
    assert any(side[2] == 2 for side in after_far['observed_side_segments'])
    assert initial['manifest']['objects'] == expanded['manifest']['objects']
    assert initial['manifest']['views'][0] == expanded['manifest']['views'][0]
    assert expanded['manifest']['views'][1]['target_candidate_ids'] == ['osm:way:21']
    assert story == original
    assert render_scene(story, [], detail_candidate_ids=['osm:way:unreceived']) is None


def test_first_overview_keeps_far_unnamed_pool_and_model_requested_details():
    from test_identity_scene import building
    story = {'latitude': 54.7, 'longitude': 20.5,
        '_identity_map_snapshot': {'observed_pool': [building(i, i*25) for i in range(1, 27)]}}
    frozen = copy.deepcopy(story)
    initial = render_scene(story, [])
    overview = physical_decision_context(story, [], initial['manifest'], overview_only=True)
    assert len(overview['rows']) == overview['received_body_count'] == 26
    assert 'observed_side_segments' not in overview['columns']
    assert 'plan_morphology' not in overview['columns']
    expanded = render_scene(story, [], detail_candidate_ids=['osm:way:26'])
    detail = physical_decision_context(story, [], expanded['manifest'])
    assert [row[1] for row in detail['rows']] == [row[1] for row in overview['rows']]
    far = dict(zip(detail['columns'], next(row for row in detail['rows'] if row[1] == 'osm:way:26')))
    assert far['omitted_side_count'] == 0 and len(far['observed_side_segments']) == 4
    assert expanded['manifest']['objects'] == initial['manifest']['objects']
    assert story == frozen


@pytest.mark.asyncio
async def test_closed_native_initial_uses_native_for_new_visual_detail_without_google(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    initial = geometry_decision()
    initial.update(decision='uncertain', next_action={'kind': 'map_detail',
        'reason': 'Inspect the short return against the other received body.',
        'target_candidate_ids': ['osm:way:2']})
    initial['bounded_coverage']['material_alternatives_resolved'] = False
    calls = []
    async def native(s, prompt, schema, images, host_context):
        calls.append((prompt, images))
        assert len(images) == 2 and images[0][0] == 'SOURCE' and images[1][0] == 'MAP'
        return {'result': payload(initial), 'receipt': {'turn_id': 'closed-initial'}, 'host_context': host_context}
    async def followup(s, prompt, schema, images, host_context):
        calls.append((prompt, images))
        assert 'Explicit requested MAP expansion' in prompt
        assert images[0][2] == calls[0][1][0][2] and images[1][2] != calls[0][1][1][2]
        return {'result': payload(geometry_decision()), 'receipt': {'turn_id': 'closed-detail'}, 'host_context': host_context}
    async def forbidden(*args, **kwargs):
        pytest.fail('Successful Native visual work must not require Google or a text-only planner')
    service.providers.gemini = SimpleNamespace(_generate=forbidden, executor=Executor(), research_routes=[])
    service.providers.research = SimpleNamespace(native_vision=SimpleNamespace(available=True),
        source_map_available=True, source_map_receipt=lambda s: None,
        source_map_followup_receipt=lambda s: None, plan_source_map=native,
        plan_source_map_followup=followup, plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert story['_identity_search_plan_payload']['geometry_proof']
    assert len(calls) == 2
    marker = service._identity_snapshot(story['id'])[1]['identity_joint_followup']
    assert marker['phase'] == 'response_closed' and marker['prepared_request']['model'] == 'gpt-6-luna'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['not_sent', 'unknown'])
async def test_native_followup_admission_uses_visual_reserve_only_when_definitely_unsent(tmp_path, phase):
    from street_story.providers import RetryableProviderError
    service, story, active = geometry_setup(tmp_path)
    initial = geometry_decision()
    initial.update(decision='uncertain', next_action={'kind': 'map_detail',
        'reason': 'Inspect the actual return, without accepting a hypothesis.',
        'target_candidate_ids': ['osm:way:2']})
    initial['bounded_coverage']['material_alternatives_resolved'] = False
    calls, saved = [], {}
    async def native(s, prompt, schema, images, host_context):
        calls.append('initial')
        return {'result': payload(initial), 'receipt': {'turn_id': 'initial-closed'}, 'host_context': host_context}
    async def followup(s, prompt, schema, images, host_context):
        calls.append('native-admission')
        saved.update(phase='created' if phase == 'not_sent' else 'submitted', provider_send_state=phase)
        raise RetryableProviderError('fixture_native_budget')
    async def google(key, timeout, contents, config, **kwargs):
        calls.append('google')
        assert phase == 'not_sent'
        assert len(contents) == 3 and 'Explicit requested MAP expansion' in contents[-1]
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())), response_id='reserve-closed')
    async def forbidden(*args, **kwargs):
        pytest.fail('No text planner or third semantic turn may replace the visual followup')
    executor = Executor()
    service.providers.gemini = SimpleNamespace(_generate=google, executor=executor,
        research_routes=[('fixture-visual', None, None, executor)])
    service.providers.research = SimpleNamespace(native_vision=SimpleNamespace(available=True),
        source_map_available=True, source_map_receipt=lambda s: None,
        source_map_followup_receipt=lambda s: saved or None, plan_source_map=native,
        plan_source_map_followup=followup, plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    marker = service._identity_snapshot(story['id'])[1]['identity_joint_followup']
    if phase == 'not_sent':
        assert calls == ['initial', 'native-admission', 'google']
        assert story['_identity_search_plan_payload']['geometry_proof']
        old = marker['route_operations']['gpt-6-luna']
        assert old['phase'] == 'not_sent' and marker['phase'] == 'response_closed'
        assert old['binding'] == marker['binding']
        assert old['prepared_request']['prompt'] == marker['prepared_request']['prompt']
        assert marker['prepared_request']['model'] == 'fixture-visual'
    else:
        assert calls == ['initial', 'native-admission']
        assert marker['phase'] == 'unknown' and 'route_operations' not in marker
        assert not story['_identity_search_plan_payload'].get('geometry_proof')


@pytest.mark.asyncio
async def test_map_detail_uses_existing_single_followup_and_freezes_the_new_map(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    initial = geometry_decision()
    initial.update(decision='uncertain', next_action={'kind': 'map_detail',
        'reason': 'Inspect the short return and its relation to the other received body.',
        'target_candidate_ids': ['osm:way:2']})
    initial['bounded_coverage']['material_alternatives_resolved'] = False
    calls = []

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(payload(initial)))
        assert len(calls) == 2
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data != calls[0][1].inline_data.data
        assert 'Explicit requested MAP expansion' in contents[-1]
        assert 'conditional_initial_decision' in contents[-1]
        assert 'osm:way:3' in contents[-1]
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))

    async def forbidden(*args, **kwargs):
        pytest.fail('No third planner or paid text fallback after a valid detail proof')

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    proof = story['_identity_search_plan_payload']['geometry_proof']
    assert len(calls) == 2
    assert proof['source_map_receipt']['map_image_sha256'] == hashlib.sha256(calls[1][1].inline_data.data).hexdigest()
    assert proof['source_map_receipt']['manifest']['detail_view']['name'] == 'nominated_detail'
    assert set(proof['source_map_receipt']['material_alternative_candidate_ids']) == {'osm:way:2', 'osm:way:3'}
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'


@pytest.mark.asyncio
async def test_unsent_detail_followup_reuses_original_map_and_preserves_unsent_text_receipt(tmp_path, monkeypatch):
    from test_closed_initial_plan_reuse import install, prepared_plan
    service, story, active, initial = prepared_plan(tmp_path)
    initial['accepted_geometry']['next_action'] = {'kind': 'map_detail',
        'reason': 'Inspect the return before accepting this physical subject.',
        'target_candidate_ids': ['osm:way:2']}
    original_map = render_scene(story, active)
    calls, acquisitions = install(service, initial, monkeypatch)
    await identity_discovery.suggest(service, story, '', active)
    plan = story['_identity_search_plan_payload']
    assert calls == ['initial', 'followup'] and acquisitions == [['99']]
    assert plan['source_map_receipt']['map_image_sha256'] == original_map['manifest']['image_sha256']
    assert plan['source_map_receipt']['manifest'].get('detail_view', {}).get('name') != 'nominated_detail'
    assert plan['source_text_receipt']['provider_send_state'] == 'not_sent'
    assert not plan.get('geometry_proof') and not plan.get('architectural_text_proof')


@pytest.mark.asyncio
async def test_selected_text_followup_replaces_one_schema_instead_of_appending_a_conflicting_contract(tmp_path, monkeypatch):
    from test_closed_initial_plan_reuse import install, prepared_plan
    service, story, active, initial = prepared_plan(tmp_path)
    install(service, initial, monkeypatch)
    original_generate = service.providers.gemini._generate
    configurations = []

    async def generate(key, timeout, contents, config, **kwargs):
        configurations.append(config.system_instruction)
        return await original_generate(key, timeout, contents, config, **kwargs)

    service.providers.gemini._generate = generate
    await identity_discovery.suggest(service, story, '', active)
    assert len(configurations) == 2
    assert 'accepted_architectural_text' not in configurations[0]
    assert 'accepted_architectural_text' in configurations[1]
    assert 'Follow-up contract:' not in configurations[1]
    # Compact T owns one decision schema; the original search plan survives
    # outside the model output rather than becoming a conflicting contract.
    assert 'first_wave_hypotheses' not in configurations[1]
    issued = json.loads(configurations[1].split('\n', 1)[1])
    assert 'material_alternatives' in issued['properties']
    assert 'first_wave_hypotheses' not in issued['properties']
    assert 'Return only the SOURCE/architectural-text decision object' in configurations[1]
