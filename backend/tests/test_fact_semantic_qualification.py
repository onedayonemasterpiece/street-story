import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def installer():
    spec = importlib.util.spec_from_file_location('semantic_qualification_installer',
        Path(__file__).resolve().parents[1] / 'deploy' / 'devcoveer_install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def proof(tmp_path, module):
    flags = dict.fromkeys(('schema_verified', 'own_passages_verified', 'qualifier_negative_verified',
                         'nearby_duplicate_verified', 'nearby_conflict_verified'), True)
    report = {'phase': 'completed', 'qualified': True, 'provider_id': 'opencode',
        'model_id': 'mimo-v2.6-flash-free', 'endpoint': 'http://127.0.0.1:4097', **flags,
        'receipt': {'phase': 'completed', 'model_id': 'mimo-v2.6-flash-free'}}
    path = tmp_path / 'completed-probe.json'
    path.write_text(json.dumps(report))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    route = {**report, 'directory': str(module.RESEARCH_DIRECTORY),
             'qualification_receipt': str(path), 'qualification_sha256': digest}
    return ({'fact-semantic-verification-v1': {'routes': [route]}},
        [{'path': str(path), 'sha256': digest}],
        {'extractors': [{'provider_id': 'opencode', 'model_id': 'mimo-v2.6-flash-free'}]})


def test_optional_semantic_pool_requires_exact_completed_proof(tmp_path):
    module = installer()
    module.validate_fact_semantic_pool({}, [], {})
    caches, evidence, text = proof(tmp_path, module)
    module.validate_fact_semantic_pool(caches, evidence, text)
    caches['fact-semantic-verification-v1']['routes'][0]['qualifier_negative_verified'] = False
    with pytest.raises(module.DeployError):
        module.validate_fact_semantic_pool(caches, evidence, text)


def test_not_sent_probe_cannot_qualify_even_with_true_route_flags(tmp_path):
    module = installer()
    caches, evidence, text = proof(tmp_path, module)
    path = Path(evidence[0]['path'])
    report = json.loads(path.read_text())
    report.update(phase='failed', qualified=False, receipt={})
    path.write_text(json.dumps(report))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence[0]['sha256'] = digest
    caches['fact-semantic-verification-v1']['routes'][0]['qualification_sha256'] = digest
    with pytest.raises(module.DeployError):
        module.validate_fact_semantic_pool(caches, evidence, text)
