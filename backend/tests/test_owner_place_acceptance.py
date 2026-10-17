import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import owner_place_acceptance as harness


def qualified_capsule(tmp_path, monkeypatch, *, receipt_directory=None):
    monkeypatch.setattr(harness, 'managed', lambda path: path.resolve())
    directory = tmp_path/'capsule'
    directory.mkdir()
    profile = directory/'opencode.json'
    profile.write_text('{}')
    model = 'mimo-v2.6-flash-free'
    control = {'passed': True, 'phase': 'completed', 'checks': {'measured': True},
        'receipt': {'phase': 'completed', 'provider_id': 'opencode', 'model_id': model,
                    'isolation': {'directory': str(receipt_directory or directory)}}}
    report = tmp_path/'controls.json'
    report.write_text(json.dumps({'directory': str(directory), 'model_routes': {model: {
        'qualified': True, 'provider_id': 'opencode', 'model_id': model,
        'controls': {'negative_foreign_subject': control,
                     'positive_planned_modality_known_reuse': control}}}}))
    route = {'provider_id': 'opencode', 'model_id': model, 'directory': str(directory),
        'qualification_receipt': str(report), 'qualification_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
        'profile_sha256': hashlib.sha256(profile.read_bytes()).hexdigest(),
        **{flag: True for flag in ('semantic_contract_verified', 'source_subject_negative_verified',
                                  'planned_modality_verified', 'known_claim_reuse_verified')}}
    proof = tmp_path/'qualification.json'
    proof.write_text(json.dumps({'caches': {'research-text-verification-v1': {'extractors': [route]}},
        'evidence': [{'path': str(report), 'sha256': route['qualification_sha256']}]}))
    installer = SimpleNamespace(RESEARCH_DIRECTORY=tmp_path/'default-capsule', RESEARCH_QUALIFICATION=proof,
        require_mode=lambda path, mode: None, validate_fact_semantic_pool=lambda *args: None)
    return directory, proof, report, installer


def test_explicit_capsule_overrides_loaded_environment_and_freezes_profile_without_semantic_pool(tmp_path, monkeypatch):
    directory, proof, _, installer = qualified_capsule(tmp_path, monkeypatch)
    monkeypatch.setenv('STREET_STORY_RESEARCH_DIRECTORY', str(tmp_path/'old-env-capsule'))
    qualification, caches, provenance = harness.load_research_qualification(installer,
        research_directory=directory, qualification_path=proof)
    assert installer.RESEARCH_DIRECTORY == directory
    assert harness.os.environ['STREET_STORY_RESEARCH_DIRECTORY'] == str(directory)
    assert 'fact-semantic-verification-v1' not in caches
    assert provenance['research_directory'] == str(directory)
    assert provenance['qualification_path'] == str(proof)
    assert provenance['research_profile_sha256'] == hashlib.sha256(b'{}').hexdigest()
    assert provenance['qualification_file_sha256'] == hashlib.sha256(proof.read_bytes()).hexdigest()
    assert caches == qualification['caches']


def test_default_capsule_preserves_loaded_environment_and_original_qualification(tmp_path, monkeypatch):
    directory, proof, _, installer = qualified_capsule(tmp_path, monkeypatch)
    monkeypatch.setenv('STREET_STORY_RESEARCH_DIRECTORY', str(directory))
    _, _, provenance = harness.load_research_qualification(installer)
    assert provenance['research_directory'] == str(directory)
    assert provenance['qualification_path'] == str(proof)
    assert provenance['directory_override'] is False
    assert provenance['qualification_override'] is False


def test_override_rejects_relabelled_old_receipt_even_when_evidence_digest_is_valid(tmp_path, monkeypatch):
    directory, proof, _, installer = qualified_capsule(tmp_path, monkeypatch,
        receipt_directory=tmp_path/'old-qualified-capsule')
    with pytest.raises(ValueError, match='different capsule'):
        harness.load_research_qualification(installer, research_directory=directory, qualification_path=proof)


def test_override_rejects_changed_profile_or_changed_evidence(tmp_path, monkeypatch):
    directory, proof, report, installer = qualified_capsule(tmp_path, monkeypatch)
    (directory/'opencode.json').write_text('{"changed":true}')
    with pytest.raises(ValueError, match='provenance mismatch'):
        harness.load_research_qualification(installer, research_directory=directory, qualification_path=proof)
    report.write_text('{}')
    with pytest.raises(ValueError, match='provenance mismatch'):
        harness.load_research_qualification(installer, research_directory=directory, qualification_path=proof)


def test_useful_partial_does_not_stop_active_research_at_three_or_more():
    case = {'own_read_supported_fact_count': 4, 'own_read_source_count': 2,
        'jobs': [{'id': 'research', 'kind': 'research', 'state': 'running'}]}
    visible = {'facts': [{'id': str(i)} for i in range(4)],
        'research_outcome': {'outcome': 'useful_partial', 'coverage_complete': False}}
    assert harness.observe_research(case, visible, 81, window_ended=False) is None
    assert case['status'] == 'OBSERVING'
    assert case['active_research_job_ids'] == ['research']
    assert case['research_complete'] is False


def test_one_proved_fact_is_available_without_count_based_acceptance_gates():
    case = {'own_read_supported_fact_count': 1, 'own_read_source_count': 1,
        'confirmed_identity_preserved': True,
        'gates': {'eligible_minimum': False, 'canonical_poi_readback': False,
            'correct_physical_object': True},
        'jobs': [{'id': 'research', 'kind': 'research', 'state': 'running'}]}
    assert harness.observe_research(case, {'research_outcome': None}, 35, window_ended=False) is None
    assert case['useful_product_available'] is True
    assert case['gates'] == {'correct_physical_object': True}
    assert case['legacy_count_observations']['eligible_minimum'] is False


def test_five_minute_material_snapshot_is_immutable_when_late_results_arrive():
    case = {'own_read_supported_fact_count': 1, 'own_read_source_count': 1,
        'jobs': [{'id': 'research', 'kind': 'research', 'state': 'retry'}]}
    visible = {'facts': [{'id': 'first'}], 'research_outcome': None}
    assert harness.observe_research(case, visible, 301, window_ended=False) is None
    early = case['five_minute_observation']
    case['own_read_supported_fact_count'] = 5
    visible = {'facts': [{'id': str(i)} for i in range(5)], 'research_outcome': None}
    assert harness.observe_research(case, visible, 720, window_ended=True) == 'observation_window'
    assert case['five_minute_observation'] == early
    assert early['own_read_supported_fact_count'] == 1
    assert case['useful_fact_count'] == 5
    assert case['status'] == 'OBSERVATION_WINDOW_ENDED'
    assert case['research_complete'] is False


def test_idle_done_job_with_unknown_original_is_an_incomplete_attempt():
    case = {'own_read_supported_fact_count': 0, 'own_read_source_count': 1,
        'jobs': [{'id': 'research', 'kind': 'research', 'state': 'done'}]}
    visible = {'research_outcome': {'outcome': 'resource_blocked',
        'reason': 'research_agent_original_pending', 'coverage_complete': False}}
    assert harness.observe_research(case, visible, 90, window_ended=False) == 'research_workers_idle'
    assert case['status'] == 'RESEARCH_ATTEMPT_ENDED'
    assert case['research_complete'] is False
    assert case['product_outcome'] == visible['research_outcome']
