import hashlib
import json
import sqlite3

import pytest

from deploy import devcoveer_install as installer


@pytest.mark.parametrize('broken', [None, 'legacy_absent', 'model', 'endpoint', 'negative', 'transport', 'parts', 'proof', 'common_gate'])
def test_optional_opencode_vision_requires_retained_direct_pair_proof(tmp_path, monkeypatch, broken):
    proof = tmp_path / 'pair-controls.json'
    proof.write_text('{"positive":"match","negative":"mismatch","image_attachments":2}')
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    vision = {'model_id': 'mimo-v2.6-flash-free', 'provider_id': 'opencode', 'endpoint': 'http://127.0.0.1:4097',
              'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True,
              'common_acceptance_verified': True, 'image_transport': 'inline_data_uri_v1', 'image_attachments': 2,
              'qualification_receipt': str(proof), 'qualification_sha256': digest}
    changes = {'model': ('model_id', 'gpt-6-luna'), 'endpoint': ('endpoint', 'http://127.0.0.1:9999'),
               'negative': ('negative', 'match'), 'transport': ('image_transport', 'public_url'),
               'parts': ('image_attachments', 1), 'proof': ('qualification_receipt', str(tmp_path / 'unretained.json')),
               'common_gate': ('common_acceptance_verified', False)}
    if broken in changes:
        key, value = changes[broken]
        vision[key] = value
    caches = {'native-vision-verification-v1': {'model': 'gpt-6-luna', 'transport': 'native_codex_app_server',
              'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True},
              'common_acceptance_verified': True},
              'research-text-verification-v1': {'gigachat_model': 'GigaChat-2', 'semantic_contract_verified': True}}
    if broken != 'legacy_absent':
        caches['research-vision-verification-v1'] = vision
    qualification = tmp_path / 'qualification.json'
    qualification.write_text(json.dumps({'evidence': [{'path': str(proof), 'sha256': digest}], 'caches': caches}))
    qualification.chmod(0o600)
    source = tmp_path / 'release/source/backend/deploy'
    source.mkdir(parents=True)
    (source / 'research_guard.mjs').write_text('guard fixture')
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        db.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,value_json TEXT,expires_at REAL,created_at REAL)')
    monkeypatch.setattr(installer, 'RESEARCH_QUALIFICATION', qualification)
    monkeypatch.setattr(installer, 'RESEARCH_DIRECTORY', tmp_path / 'opencode')
    monkeypatch.setattr(installer, 'DATA_ROOT', tmp_path)
    calls = []
    def attest(*args, **kwargs):
        calls.append(args)
        return '{}'
    monkeypatch.setattr(installer, 'run', attest)
    if broken not in {None, 'legacy_absent'}:
        with pytest.raises(installer.DeployError, match='OpenCode vision qualification incomplete'):
            installer.install_research_runtime(tmp_path / 'release', tmp_path / 'existing-venv')
        assert calls == [] and not (tmp_path / 'opencode').exists()
    else:
        result = installer.install_research_runtime(tmp_path / 'release', tmp_path / 'existing-venv')
        assert result['new_inference'] is False and len(calls) == 1
        with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
            row = db.execute('SELECT value_json FROM cache WHERE key=?', ('research-vision-verification-v1',)).fetchone()
        if broken == 'legacy_absent':
            assert row is None and 'qualified_opencode_vision_model' not in result
        else:
            assert json.loads(row[0]) == vision
            assert result['qualified_opencode_vision_model'] == 'mimo-v2.6-flash-free'
