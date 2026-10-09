"""The existing joint operation may finish identity without external REF."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.identity_proof import geometry_result_valid
from street_story.identity_source_selection import model_identity_context, compact_planner_packet, expand_planner_packet
from street_story.providers import PermanentProviderError, RetryableProviderError
from street_story.service import ConflictError
from test_identity_scene import building, point
from test_visual_search_continuation import prepared


def geometry_setup(tmp_path):
    service, _adapter, story, _sessions = prepared(tmp_path)
    first, other = building(2, 20), building(3, 50)
    road = {'type': 'way', 'id': 9, 'tags': {'highway': 'residential'},
        'geometry': [point(0, 0), point(90, 0)]}
    osm = {'observed_pool': [first, other, road], 'nearby': [other]}
    observed = service._candidate_catalog(osm, [], observed_pool=True)
    snapshot = {**service._identity_snapshot(story['id'])[0],
        '_identity_map_snapshot': osm, '_identity_observed_candidates': observed,
        '_camera_position_verified': True}
    active = [item for item in observed if item['candidate_id'] == 'osm:way:3']
    return service, snapshot, active


def geometry_decision():
    return {'decision': 'accepted_geometry', 'candidate_id': 'osm:way:2',
        'candidate_label': 1,
        # SOURCE camera is south-west of the rectangle; the REAL visible
        # corner uses south and west OSM sides (0,3), not the rear east side.
        'spatial_correspondence': {'pattern_kind': 'corner',
            'source_pattern': 'Two adjacent sides of the main volume form a visible return; the next body lies farther right.',
            'pitch_basis': 'Low upward view; vertical pitch is unknown and is separate from horizontal yaw.',
            'coverage_basis': 'Both received bodies and the street approach were examined; the next body has a different return position.',
            'candidate_ids': ['osm:way:2'],
            'pose': {'east_m': 0, 'north_m': 0, 'heading_degrees': 55},
            'front_segments': [{'first': {'candidate_id': 'osm:way:2', 'kind': 'segment', 'ring_index': 0, 'segment_index': 0},
                'second': {'candidate_id': 'osm:way:2', 'kind': 'segment', 'ring_index': 0, 'segment_index': 3}}],
            'street_axis': None,
            'uncertainty_scenarios': [{'pose': {'east_m': -2, 'north_m': -2, 'heading_degrees': 60},
                'assumption': 'Explicit two-metre position and five-degree yaw perturbation, not EXIF accuracy.',
                'source_pattern_preserved': True}]},
        'scope': 'Main physical footprint; neighboring body remains scene context.',
        'decisive_relations': [{
            'source_observation': 'Main facade faces the approach and the next volume is behind its right return.',
            'map_features': [{'candidate_id': 'osm:way:2', 'kind': 'contour'},
                {'candidate_id': 'osm:way:9', 'kind': 'road_axis'}],
            'correspondence': 'The visible approach and facade orientation agree for this footprint.'}],
        'rejected_alternatives': [{'candidate_id': 'osm:way:3',
            'reason': 'Its facade would lie beside the approach rather than close the visible scene.'}],
        'assumptions': ['SOURCE shows the principal facade rather than a reflected scene.'],
        'bounded_coverage': {'scope': 'All received local contours relevant to the visible approach.',
            'limitations': ['Unknown remote geometry beyond the map does not distinguish these local alternatives.'],
            'material_alternatives_resolved': True},
        'camera_pose': {'position_basis': 'Observed camera position; any shift is a stated scenario.',
            'yaw_basis': 'Looking along the visible street approach toward the main facade.',
            'sensitivity': 'Small plausible shifts preserve the approach order; unknown accuracy remains unknown.'}}


def payload(decision):
    return {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '',
        'commons_query': '', 'article_queries': [], 'first_wave_hypotheses': [],
        'accepted_geometry': decision}


class Executor:
    async def execute(self, role, call):
        return await call('offline-fixture', 5)


def test_large_osm_dictionary_stays_in_host_validation_without_repeated_provider_enums():
    from jsonschema import Draft202012Validator
    from street_story.identity_source_selection import identity_transport_schema
    ids = [f'osm:way:{n}' for n in range(1000)]
    canonical = {'type': 'object', 'properties': {
        'observed_candidate_ids': {'type': 'array', 'items': {'type': 'string', 'enum': ids}},
        'spatial_hypotheses': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'candidate_id': {'type': 'string', 'enum': ids}}}},
        'first_wave_hypotheses': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'subject_id': {'type': 'string', 'enum': [*ids, '']}}}}}}
    original = copy.deepcopy(canonical)
    transmitted = identity_transport_schema(canonical)
    assert canonical == original
    assert len(json.dumps(transmitted)) < 1000
    assert len(json.dumps(canonical)) > 40000
    unobserved = {'observed_candidate_ids': ['osm:way:unreceived']}
    assert Draft202012Validator(transmitted).is_valid(unobserved)
    assert not Draft202012Validator(canonical).is_valid(unobserved)
    assert Draft202012Validator(canonical).is_valid({'observed_candidate_ids': [ids[-1]]})


@pytest.mark.asyncio
async def test_joint_repairs_one_unreceived_alternative_without_another_judge_or_reference(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    bad = geometry_decision()
    bad['rejected_alternatives'][0]['candidate_id'] = 'osm:way:999'
    calls = []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(payload(bad)))
        assert 'unreceived_alternative_ids' in contents[-1] and 'osm:way:999' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def forbidden(*args, **kwargs):
        pytest.fail('A repaired valid joint decision needs no further planner or REF')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(calls) == 2 and story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'


@pytest.mark.asyncio
async def test_nominated_architectural_lookup_closes_identity_without_ref_or_extra_text_planner(tmp_path, monkeypatch):
    from street_story import identity_architectural_context
    from test_architectural_text_identity import text_inputs
    service, story, active = geometry_setup(tmp_path)
    _input, _catalog, text_decision, receipt = text_inputs(candidate_id='osm:way:2')
    text_decision['material_alternatives'] = [{'candidate_id': 'osm:way:3',
        'reason': 'SOURCE and the text place the return behind the three-axis bay; this neighbor puts it beside the bay.'}]
    articles = receipt['articles']
    uncertain = geometry_decision()
    uncertain['decision'] = 'uncertain'
    initial = payload(uncertain)
    initial.update(regional_lookup={'route': 'address', 'candidate_ids': ['osm:way:2'],
        'reason': 'The isolated facade needs a distinguishing bay description.'})
    requests, calls = [], []
    async def acquire(svc, snapshot, candidates, request):
        requests.append(request)
        return articles, {'status': 'completed', 'results': []}
    monkeypatch.setattr(identity_architectural_context, 'acquire_regional_text', acquire)
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert articles[0]['text'] in contents[-1]
        return SimpleNamespace(text=json.dumps(text_decision))
    async def forbidden(*args, **kwargs):
        pytest.fail('Architectural text identity needs no external REF or third planner')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(requests) == 1 and len(calls) == 2
    accepted = story['_identity_geometry_result']
    assert accepted['proof_kind'] == 'architectural_text' and accepted['candidate_id'] == 'osm:way:2'
    assert accepted['visual_reference_verified'] is False
    assert history['planned_queries'] == []
    assert history['search_plan']['payload']['architectural_text_proof']['validated'] is True


@pytest.mark.asyncio
async def test_invalid_geometry_preserves_explicit_ready_wiki_choice_without_another_planner(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    story['_identity_wikipedia_metadata'] = [{'pageid': 99, 'title': 'Observed subject',
        'thumbnail_url': 'https://example.org/ready.jpg'}]
    decision = geometry_decision()
    decision['candidate_label'] = 2  # Bound to the other received building.
    plan = payload(decision)
    plan['selected_wikipedia_page_ids'] = ['99']
    calls = []
    async def generate(*args, **kwargs):
        calls.append('joint')
        return SimpleNamespace(text=json.dumps(plan))
    async def forbidden(*args, **kwargs):
        pytest.fail('Valid explicit Wiki choice must survive rejected geometry without paid replanning')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['joint', 'joint']  # One bounded exact-pointer repair.
    saved = history['search_plan']['payload']
    assert saved['selected_wikipedia_page_ids'] == ['99']
    assert 'accepted_geometry' not in saved and 'geometry_proof' not in saved
    assert saved['rejected_geometry']['decision'] == decision
    assert '_identity_geometry_result' not in story


@pytest.mark.asyncio
@pytest.mark.parametrize('label', [None, 2, 999])
async def test_joint_geometry_rejects_missing_or_wrong_map_label_binding(tmp_path, label):
    service, story, active = geometry_setup(tmp_path)
    decision = geometry_decision()
    if label is None:
        decision.pop('candidate_label')
    else:
        decision['candidate_label'] = label
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps(payload(decision)))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = None
    with pytest.raises(PermanentProviderError, match='identity_geometry_proof_invalid'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert '_identity_geometry_result' not in story


@pytest.mark.asyncio
async def test_one_joint_call_accepts_full_pool_geometry_and_reuses_without_any_reference_search(tmp_path, monkeypatch):
    service, story, active = geometry_setup(tmp_path)
    story['_identity_search_plan_route'] = 'qualified_text_fallback'
    calls = []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        assert len(contents) == 3 and contents[0].inline_data and contents[1].inline_data
        assert config.response_json_schema is None
        assert config.response_mime_type == 'application/json'
        assert 'accepted_geometry' in config.system_instruction
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def forbidden(*args, **kwargs):
        pytest.fail('Accepted joint geometry must end identity before external reference work')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    monkeypatch.setattr(identity_discovery, 'web_image_sources', forbidden)
    monkeypatch.setattr(identity_discovery, 'retrieve', forbidden)
    service._identify_photo = forbidden
    service._candidate_reference_images = forbidden
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    raw = story['_identity_geometry_result']
    assert raw['candidate_id'] == 'osm:way:2' and raw['confidence'] is None
    assert raw['status'] == 'match' and raw['proof_kind'] == 'geometry'
    assert raw['visual_reference_verified'] is False and raw['_references_sent'] == []
    receipt = raw['geometry_proof']['source_map_receipt']
    assert receipt['original_source_sha256'] == hashlib.sha256(service._source_photo_bytes(story['id'])).hexdigest()
    assert receipt['model_source_sha256'] == hashlib.sha256(calls[0][0].inline_data.data).hexdigest()
    assert geometry_result_valid(raw, active, story)
    assert history['planned_queries'] == []
    assert history['search_plan']['payload']['geometry_proof'] == raw['geometry_proof']
    assert any(item['candidate_id'] == 'osm:way:2' for item in active)
    recovered = await identity_discovery.recover(service, story, '', active, set())
    assert recovered[0] == raw and len(calls) == 1
    assert service._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'
    # The lifecycle, rather than the planner, owns the fenced product commit.


@pytest.mark.asyncio
async def test_equal_pose_or_incomplete_coverage_remains_uncertain_without_fake_proof(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    decision = geometry_decision()
    decision.update(decision='uncertain', candidate_id='', decisive_relations=[], rejected_alternatives=[])
    decision['bounded_coverage']['material_alternatives_resolved'] = False
    decision['bounded_coverage']['limitations'] = ['Two poses still explain the repeated facade equally well.']
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps(payload(decision)))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert '_identity_geometry_result' not in story
    assert 'geometry_proof' not in history['search_plan']['payload']
    assert service._identity_snapshot(story['id'])[1]['visual_identity']['status'] == 'uncertain'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['geometry', 'coverage'])
async def test_schema_valid_unproved_geometry_gets_one_evidence_repair_before_identity_acceptance(tmp_path, failure):
    from test_structured_identity_first_wave import choice
    service, story, active = geometry_setup(tmp_path)
    decision = geometry_decision()
    decision['bounded_coverage']['material_alternatives_resolved'] = False
    initial = payload(decision)
    code = 'identity_geometry_proof_invalid'
    if failure == 'coverage':
        for candidate in story['_identity_observed_candidates']:
            if candidate['candidate_id'] in {'osm:way:2', 'osm:way:3'}:
                candidate['map_address'] = {'street': 'Fixture street', 'house_number': candidate['candidate_id'][-1]}
        decision.update(decision='uncertain', candidate_id='', decisive_relations=[], rejected_alternatives=[])
        initial['first_wave_hypotheses'] = [choice('address', 'osm:way:2')]
        code = 'identity_first_wave_coverage_incomplete'
    calls = []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert len(calls) == 2
        assert code in contents[-1]
        assert 'previous_claim_is_not_confirmation' in contents[-1]
        assert contents[0].inline_data.data == calls[0][0].inline_data.data
        assert contents[1].inline_data.data == calls[0][1].inline_data.data
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = None
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert len(calls) == 2
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'


@pytest.mark.asyncio
async def test_closed_invalid_geometry_uses_one_bounded_repair_without_third_inference(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    decision = geometry_decision()
    decision['bounded_coverage']['material_alternatives_resolved'] = False
    calls = []
    async def generate(*args, **kwargs):
        calls.append('google')
        return SimpleNamespace(text=json.dumps(payload(decision)))
    async def fallback(story, prompt, schema):
        pytest.fail('Two invalid spatial responses cannot authorize a third semantic attempt')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=fallback)
    with pytest.raises(PermanentProviderError, match='identity_geometry_proof_invalid'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['google', 'google'] and '_identity_geometry_result' not in story


@pytest.mark.asyncio
async def test_text_fallback_cannot_claim_geometry_without_actual_joint_images(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    async def generate(*args, **kwargs):
        error = RetryableProviderError('fixture_google_unavailable')
        error.receipt = {'provider_send_state': 'not_sent'}
        raise error
    async def fallback(*args, **kwargs):
        return {'result': payload(geometry_decision())}
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=fallback)
    with pytest.raises(PermanentProviderError, match='identity_search_plan_malformed'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert '_identity_geometry_result' not in story
    assert not service._identity_snapshot(story['id'])[1].get('identity_article_discovery', {}).get('search_plan')


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', [
    {'candidate_id': 'osm:way:999', 'kind': 'contour'},
    {'candidate_id': 'osm:way:2', 'kind': 'attribute', 'key': 'height'},
    {'candidate_id': 'osm:way:2', 'kind': 'segment', 'ring_index': 0, 'segment_index': 999}])
async def test_geometry_cannot_use_unreceived_object_or_invent_missing_height_or_segment(tmp_path, reference):
    service, story, active = geometry_setup(tmp_path)
    decision = geometry_decision()
    decision['decisive_relations'][0]['map_features'] = [reference]
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps(payload(decision)))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = None
    with pytest.raises(PermanentProviderError, match='identity_geometry_proof_invalid'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert '_identity_geometry_result' not in story


def test_large_joint_scene_leaf_keeps_all_ids_for_later_lossless_compaction():
    observed = [{'candidate_id': f'osm:way:{i}', 'name': f'Actual observed physical name{i}',
        'map_object': {'tags': {'building': 'yes', 'name': f'Actual observed physical name{i}'}},
        'map_address': {'city': 'Observed city', 'street': 'Actual observed road', 'house_number': f'{i}A'}}
        for i in range(1000)]
    packet = model_identity_context({'_identity_observed_candidates': observed}, scene_available=True)
    rows = packet['observed_physical_candidates']['rows']
    assert len(rows) == 1000 and {row[0] for row in rows} == {item['candidate_id'] for item in observed}


def test_integer_label_table_joins_restore_exact_ids_and_original_nonstring_values():
    packet = {'map_scene': {'objects': {'columns': ['label', 'candidate_id'],
        'rows': [[i, f'osm:way:{i}'] for i in range(1, 501)]}},
        'subjects': {'columns': ['subject_id', 'group_key', 'query'],
            'rows': [[f'osm:way:{i}', f'osm:way:{i}', 'Exact literal road 12A'] for i in range(1, 501)]
                + [[42, {'literal_candidate_value': 3}, '@42'], [True, None, '$42']]}}
    original = copy.deepcopy(packet)
    compact = compact_planner_packet(packet)
    assert expand_planner_packet(compact) == original and packet == original
    subjects = compact['subjects']
    assert subjects['candidate_label_columns'] == [0, 1]
    assert len(json.dumps(compact)) < len(json.dumps(original)) * .7


@pytest.mark.asyncio
async def test_uncertain_joint_geometry_can_choose_one_bound_distinguishing_address_step(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    for item in story['_identity_observed_candidates']:
        if item['candidate_id'] in {'osm:way:2', 'osm:way:3'}:
            item['map_address'] = {'city': 'Observed city', 'street': 'Observed street',
                'house_number': '12A' if item['candidate_id'] == 'osm:way:2' else '14'}
    decision = geometry_decision()
    decision['decision'] = 'uncertain'
    decision['bounded_coverage']['material_alternatives_resolved'] = False
    decision['next_action'] = {'kind': 'address_text',
        'reason': 'The one literal address source could distinguish the repeated facade.',
        'target_candidate_ids': ['osm:way:2']}
    plan = payload(decision)
    plan['visual_query'] = 'Generic appearance search must not become a compulsory extra step'
    plan['first_wave_hypotheses'] = [{'kind': 'address', 'subject_id': 'osm:way:2', 'query': '',
        'reason': 'Read the actual mapped address source for the chosen remaining question.'}]
    async def generate(*args, **kwargs):
        return SimpleNamespace(text=json.dumps(plan))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    assert history['planned_queries'] == ['Observed city Observed street 12A']
    assert history['search_plan']['payload']['accepted_geometry']['next_action'] == decision['next_action']
    assert '_identity_geometry_result' not in story and 'geometry_proof' not in history['search_plan']['payload']


@pytest.mark.asyncio
async def test_geometry_response_for_replaced_photo_cannot_persist_or_pass_current_scope(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    async def generate(*args, **kwargs):
        with service.store.tx() as db:
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('f' * 64, story['id']))
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    with pytest.raises(ConflictError):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    # The durable operation fence now rejects stale SOURCE before a geometry
    # result can be constructed, rather than waiting for plan persistence.
    assert '_identity_geometry_result' not in story
    marker = service._identity_snapshot(story['id'])[1]['identity_joint_initial']
    assert marker['scope']['photo_sha256'] == story['photo_sha256'] and marker['phase'] == 'send_intent'
    assert not service._identity_snapshot(story['id'])[1].get('identity_article_discovery', {}).get('search_plan')
