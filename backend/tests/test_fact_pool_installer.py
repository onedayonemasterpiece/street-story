import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


def installer():
    path = Path(__file__).resolve().parents[1] / 'deploy/devcoveer_install.py'
    spec = importlib.util.spec_from_file_location('fact_pool_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entries(directory):
    return [{'provider_id': provider, 'model_id': model,
             **({'endpoint': 'http://127.0.0.1:4097', 'directory': str(directory)} if provider == 'opencode' else {}),
             'semantic_contract_verified': True, 'source_subject_negative_verified': True,
             'planned_modality_verified': True, 'known_claim_reuse_verified': True}
            for provider, model in [('gigachat', 'GigaChat-2'), ('opencode', 'mimo-v2.6-flash-free'),
                                    ('opencode', 'nemotron-3-ultra-free')]]


def fixture(tmp_path, monkeypatch, *, pool=True):
    module = installer()
    directory = tmp_path / 'opencode'
    proof = tmp_path / 'controls.json'
    proof.write_text('{"actual_controls":"retained"}')
    text = {'gigachat_model': 'GigaChat-2', 'semantic_contract_verified': True}
    if pool:
        text['extractors'] = entries(directory)
    caches = {'native-vision-verification-v1': {'model': 'gpt-6-luna', 'transport': 'native_codex_app_server',
              'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True},
              'common_acceptance_verified': True}, 'research-text-verification-v1': text}
    qualification = tmp_path / 'qualification.json'
    qualification.write_text(json.dumps({'evidence': [{'path': str(proof), 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}],
                                         'caches': caches}))
    qualification.chmod(0o600)
    release = tmp_path / 'release'
    source = release / 'source/backend/deploy'
    source.mkdir(parents=True)
    from street_story.shared_devcoveer_research import GUARD_SOURCE
    (source / 'research_guard.mjs').write_bytes(GUARD_SOURCE.read_bytes())
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        db.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,value_json TEXT,expires_at REAL,created_at REAL)')
    monkeypatch.setattr(module, 'RESEARCH_QUALIFICATION', qualification)
    monkeypatch.setattr(module, 'RESEARCH_DIRECTORY', directory)
    monkeypatch.setattr(module, 'DATA_ROOT', tmp_path)
    return module, release, directory, qualification, caches


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unknown_model', 'missing_flag', 'false_flag', 'endpoint', 'directory', 'not_list'])
def test_incomplete_pool_rejected_before_any_profile_or_cache_write(tmp_path, monkeypatch, defect):
    module, release, directory, qualification, caches = fixture(tmp_path, monkeypatch)
    values = caches['research-text-verification-v1']['extractors']
    if defect == 'missing':
        values.pop()
    elif defect == 'duplicate':
        values[2] = dict(values[1])
    elif defect == 'unknown_model':
        values[2]['model_id'] = 'unapproved-model'
    elif defect == 'missing_flag':
        values[2].pop('planned_modality_verified')
    elif defect == 'false_flag':
        values[2]['source_subject_negative_verified'] = False
    elif defect == 'endpoint':
        values[2]['endpoint'] = 'http://other-server:4097'
    elif defect == 'directory':
        values[2]['directory'] = '/other/capsule'
    else:
        caches['research-text-verification-v1']['extractors'] = {}
    content = json.loads(qualification.read_text())
    content['caches'] = caches
    qualification.write_text(json.dumps(content))
    calls = []
    monkeypatch.setattr(module, 'run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(module.DeployError, match='pool qualification incomplete'):
        module.install_research_runtime(release, tmp_path / 'venv')
    assert not calls and not directory.exists()
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0


@pytest.mark.parametrize('pool', [False, True])
def test_one_existing_profile_attests_pool_without_inference_or_new_server(tmp_path, monkeypatch, pool):
    module, release, directory, _, caches = fixture(tmp_path, monkeypatch, pool=pool)
    from street_story.shared_devcoveer_research import SharedDevCoveerResearch
    calls, commands, config_reads = [], [], []
    async def existing_config(client, transport, method, path, **kwargs):
        assert transport is None and (method, path) == ('GET', '/config')
        config_reads.append(client.directory)
        return {'mcp': {}}
    async def attest(client, transport, role):
        calls.append((client.directory, client.model_id, role))
        return {'guard_sha256': 'retained-guard', 'tool_boundary_enforced': True, 'search_call_limit': None}
    monkeypatch.setattr(SharedDevCoveerResearch, '_attest', attest)
    # This installer unit fixture has no platform service or credentials. Mock
    # its read-only configuration boundary as well as semantic attestation.
    monkeypatch.setattr(SharedDevCoveerResearch, '_request', existing_config)
    def run(argv, **kwargs):
        commands.append(argv)
        with monkeypatch.context() as temporary:
            temporary.setattr(sys, 'argv', ['profile', *argv[3:]])
            exec(argv[2], {'__name__': 'profile_test'})
        return '{}'
    monkeypatch.setattr(module, 'run', run)
    result = module.install_research_runtime(release, tmp_path / 'existing-venv')
    assert result['new_inference'] is False and len(commands) == 1
    assert config_reads == [str(directory)]
    expected = [(str(directory), 'mimo-v2.6-flash-free', 'search')]
    if pool:
        expected += [(str(directory), 'mimo-v2.6-flash-free', 'facts'),
                     (str(directory), 'nemotron-3-ultra-free', 'facts')]
        assert set(result['qualified_fact_extractors']) == {'GigaChat-2', 'mimo-v2.6-flash-free', 'nemotron-3-ultra-free'}
    assert calls == expected
    profile = json.loads((directory / 'opencode.json').read_text())
    models = profile['provider']['opencode']['models']
    assert set(models) == ({'mimo-v2.6-flash-free', 'nemotron-3-ultra-free'} if pool else {'mimo-v2.6-flash-free'})
    assert all(value['limit']['output'] == 8192 for value in models.values())
    assert len(profile['plugin']) == 1
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        retained = json.loads(db.execute('SELECT value_json FROM cache WHERE key=?', ('research-text-verification-v1',)).fetchone()[0])
    assert retained == caches['research-text-verification-v1']


def test_conflicting_existing_profile_requires_reconciliation_and_preserves_cache(tmp_path, monkeypatch):
    module, release, directory, _, _ = fixture(tmp_path, monkeypatch)
    directory.mkdir()
    profile = directory / 'opencode.json'
    original = '{"existing":"active-profile"}'
    profile.write_text(original)
    def run(argv, **kwargs):
        with monkeypatch.context() as temporary:
            temporary.setattr(sys, 'argv', ['profile', *argv[3:]])
            exec(argv[2], {'__name__': 'profile_test'})
    monkeypatch.setattr(module, 'run', run)
    with pytest.raises(RuntimeError, match='reconcile its active attempts'):
        module.install_research_runtime(release, tmp_path / 'venv')
    assert profile.read_text() == original
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0
