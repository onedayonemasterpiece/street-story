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
