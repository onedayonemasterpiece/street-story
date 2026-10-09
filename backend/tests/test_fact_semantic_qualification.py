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


def timed_proof(tmp_path, module, *, elapsed=91741):
    caches, evidence, text = proof(tmp_path, module)
    route = caches['fact-semantic-verification-v1']['routes'][0]
    path = Path(evidence[0]['path'])
    report = json.loads(path.read_text())
    report['elapsed_ms'] = 999999  # Packaging time is not the client measurement.
    report['receipt'].update(provider_id=route['provider_id'], role='facts', elapsed_ms=elapsed,
        isolation={'directory': route['directory']}, result={'packet_ref': 'qualification-control',
            'decisions': [{'fact': 0, 'verdict': 'supported'}], 'relations_complete': True})
    route['qualification_review_timing'] = {'elapsed_ms': 1}  # Never trust a supplied timing.
    return caches, evidence, text, route, path, report


def persist_timed_proof(evidence, route, path, report):
    path.write_text(json.dumps(report))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence[0]['sha256'] = digest
    route['qualification_sha256'] = digest


def test_verified_review_receipt_exports_exact_scalar_and_route_binding(tmp_path):
    module = installer()
    caches, evidence, text, route, path, report = timed_proof(tmp_path, module)
    persist_timed_proof(evidence, route, path, report)
    module.validate_fact_semantic_pool(caches, evidence, text)
    hint = route['qualification_review_timing']
    assert hint == {'operation': 'semantic_fact_review', 'phase': 'completed', 'elapsed_ms': 91741,
        'source_sha256': evidence[0]['sha256'], **{k: route[k] for k in
            ('provider_id', 'model_id', 'endpoint', 'directory')}}
    assert 'qualification-control' not in json.dumps(hint)  # No result or fact transfer.
    path.write_text(path.read_text() + ' ')
    with pytest.raises(module.DeployError, match='evidence changed'):
        module.validate_fact_semantic_pool(caches, evidence, text)


@pytest.mark.parametrize('elapsed', [None, True, '12000', 0, -1, float('nan'), float('inf')])
def test_unmeasured_or_invalid_duration_cannot_export_review_hint(tmp_path, elapsed):
    module = installer()
    caches, evidence, text, route, path, report = timed_proof(tmp_path, module, elapsed=elapsed)
    persist_timed_proof(evidence, route, path, report)
    module.validate_fact_semantic_pool(caches, evidence, text)
    assert 'qualification_review_timing' not in route


@pytest.mark.parametrize('defect', ['provider', 'directory', 'search_role', 'extraction_result'])
def test_other_operation_or_configuration_cannot_seed_review_latency(tmp_path, defect):
    module = installer()
    caches, evidence, text, route, path, report = timed_proof(tmp_path, module)
    receipt = report['receipt']
    if defect == 'provider':
        receipt['provider_id'] = 'another-provider'
    elif defect == 'directory':
        receipt['isolation']['directory'] = '/another/product'
    elif defect == 'search_role':
        receipt['role'] = 'search'
    else:
        receipt['result'] = {'facts': []}
    persist_timed_proof(evidence, route, path, report)
    module.validate_fact_semantic_pool(caches, evidence, text)
    assert 'qualification_review_timing' not in route
