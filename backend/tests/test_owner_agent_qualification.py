"""Controlled receipts exercise the explicit qualification trust boundary, no providers."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from owner_place_acceptance import load_agent_qualification
from street_story.fact_agents import QUALIFICATION_KEY
from street_story.fact_agent_providers import INSTRUCTIONS, RESULT_SCHEMA


@pytest.fixture(autouse=True)
def controlled_managed_root(tmp_path, monkeypatch):
    """Use this test's managed tree on CI; production guard stays untouched."""
    import owner_place_acceptance
    (tmp_path / '.artifact.json').write_text('{"class":"temporary","purpose":"controlled receipt fixture"}')
    def managed_test(path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(tmp_path.resolve()):
            raise ValueError('Acceptance artifacts must be under managed test root')
        if not any((parent / '.artifact.json').is_file() for parent in (resolved, *resolved.parents)):
            raise ValueError('Acceptance artifacts require a managed task ancestor')
        return resolved
    monkeypatch.setattr(owner_place_acceptance, 'managed', managed_test)


def fixture(tmp_path):
    routes, proofs = [], []
    for provider in ('google', 'gigachat'):
        route = SimpleNamespace(route_id=provider, provider_id=provider, model_id='test-model',
            endpoint='https://test.example', directory=None, profile_fingerprint='profile')
        routes.append(route)
        receipt = {'phase': 'completed', 'provider_id': provider, 'model_id': 'test-model',
            'binding': {'attempt_id': provider, 'story_id': 'test-story', 'purpose': 'facts'},
            'provider_send_state': 'response_closed', 'frozen_schema': RESULT_SCHEMA,
            'frozen_prompt': INSTRUCTIONS + '\nINPUT={}', 'model_attempts': [{'usage': {'input': 1}}],
            'tool_receipts': [{'name': 'websearch', 'arguments': {'query': 'Building'},
                'completed_at': 1, 'result_sha256': 'search'},
                {'name': 'webfetch', 'arguments': {'url': 'https://test.example/source'},
                 'completed_at': 2, 'result_sha256': 'fetch'}],
            'sources': [{'requested_url': 'https://test.example/source',
                'final_url': 'https://test.example/source', 'source_version_id': 'version',
                'content_sha256': 'content'}],
            'result': {'facts': [{'text': 'Факт', 'subject_scope': 'selected_building',
                'url': 'https://test.example/source', 'quote': 'Short quote', 'source_date': None}]}}
        path = tmp_path / (provider + '.json')
        path.write_text(json.dumps(receipt))
        proofs.append({'route_id': provider, 'provider_id': provider, 'model_id': 'test-model',
            'endpoint': 'https://test.example', 'directory': None, 'profile_sha256': 'profile',
            'contract': QUALIFICATION_KEY, 'tool_access_verified': True, 'tool_safety_verified': True,
            'qualification_receipts': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]})
    path = tmp_path / 'proof.json'
    path.write_text(json.dumps({'contract': QUALIFICATION_KEY, 'routes': proofs}))
    return path, routes


def test_actual_role_receipts_require_current_profile_for_each_independent_service(tmp_path):
    path, routes = fixture(tmp_path)
    proof, _ = load_agent_qualification(path, routes=routes)
    assert len(proof['routes']) == 2
    routes[0].profile_fingerprint = 'changed'
    with pytest.raises(ValueError, match='profile changed'):
        load_agent_qualification(path, routes=routes)


def test_one_qualified_provider_keeps_actual_integrity_checks_and_reports_reduced_availability(tmp_path):
    path, routes = fixture(tmp_path)
    proof = json.loads(path.read_text())
    proof['routes'] = proof['routes'][:1]
    path.write_text(json.dumps(proof))
    actual, metadata = load_agent_qualification(path, routes=routes)
    assert actual == proof
    assert metadata['qualified_provider_count'] == 1
    assert metadata['qualified_provider_availability'] == 'reduced'
    routes[0].profile_fingerprint = 'changed'
    with pytest.raises(ValueError, match='profile changed'):
        load_agent_qualification(path, routes=routes)


@pytest.mark.parametrize('fault', ['tampered_receipt', 'legacy_role', 'unread_candidate', 'unsafe_tool'])
def test_receipt_flags_cannot_replace_actual_read_only_role_integrity(tmp_path, fault):
    path, routes = fixture(tmp_path)
    proof = json.loads(path.read_text())
    evidence = proof['routes'][0]['qualification_receipts'][0]
    receipt_path = tmp_path / 'google.json'
    receipt = json.loads(receipt_path.read_text())
    if fault == 'tampered_receipt':
        receipt['result']['facts'][0]['text'] = 'Changed'
    elif fault == 'legacy_role':
        receipt['frozen_schema'] = {'type': 'object'}
    elif fault == 'unread_candidate':
        receipt['result']['facts'][0]['url'] = 'https://unread.example'
    elif fault == 'unsafe_tool':
        receipt['tool_receipts'].append({'name': 'bash', 'arguments': {}})
    receipt_path.write_text(json.dumps(receipt))
    if fault != 'tampered_receipt':
        evidence['sha256'] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(proof))
    with pytest.raises(ValueError):
        load_agent_qualification(path, routes=routes)


def test_receipt_path_cannot_escape_controlled_managed_root(tmp_path):
    path, routes = fixture(tmp_path)
    proof = json.loads(path.read_text())
    proof['routes'][0]['qualification_receipts'][0]['path'] = str(tmp_path.parent / 'foreign.json')
    path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match='managed test root'):
        load_agent_qualification(path, routes=routes)


@pytest.mark.parametrize('fault', [None, 'foreign_thread', 'no_search', 'no_read', 'disabled_hosted', 'unsafe_tool'])
def test_native_hosted_discovery_requires_executed_owned_search_and_frozen_reader(tmp_path, fault):
    from street_story.native_fact_contract import RESULT_SCHEMA as NATIVE_SCHEMA, FINAL_INSTRUCTIONS
    from street_story.native_fact_research import CONTRACT, INSTRUCTIONS as NATIVE_INSTRUCTIONS
    path, routes = fixture(tmp_path)
    proof = json.loads(path.read_text())
    proof['routes'] = proof['routes'][:1]
    route = proof['routes'][0]
    route.update(provider_id='codex_native', model_id='gpt-6-luna')
    routes[0].provider_id, routes[0].model_id = 'codex_native', 'gpt-6-luna'
    evidence = route['qualification_receipts'][0]
    receipt_path = Path(evidence['path'])
    receipt = json.loads(receipt_path.read_text())
    claim = {'text': 'Факт', 'claim_key': 'controlled', 'subject_scope': 'selected_building', 'source_date': None}
    assessment = {**claim, 'verdict': 'supported', 'confirmed_fragment': 'Short quote', 'chunk_ids': ['chunk'],
        'reason': 'Controlled semantic assessment', 'existing_fact_id': '', 'conflicts_with_existing': [],
        'checks': {'source_physical_subject': 'Building', 'object_relation': 'selected_building',
            'relation_basis': {'fragment': 'Building', 'chunk_ids': ['chunk']},
            'material_kind': 'history', 'temporal_basis': 'historical', 'temporal_preserved': True}}
    receipt.update(provider_id='codex_native', model_id='gpt-6-luna', native_contract=CONTRACT,
        native_terminal_status='completed', thread_id='owned', turn_id='turn', native_threads={'owned': {}},
        native_tool_calls={'call': {'phase': 'completed'}}, model_attempts=[{'thread_id': 'owned'}],
        frozen_schema=NATIVE_SCHEMA, frozen_prompt=NATIVE_INSTRUCTIONS+'\n'+FINAL_INSTRUCTIONS+'\nINPUT={}',
        applied_controls={'profile_overrides': {'web_search': 'live'}},
        native_discovery=[{'type': 'webSearch', 'id': 'search', 'thread_id': 'owned',
            'evidence_role': 'discovery_only_read_before_support',
            'action': {'type': 'search', 'queries': ['Building history']}, 'results': []}],
        raw_result={'facts': [{**claim, 'sources': [{'url': 'https://test.example/source',
            'source_version_id': 'version', 'content_sha256': 'content', 'assessment': assessment}]}],
            'completion': {'research_sufficient': False, 'reason': 'More history remains',
                'remaining_gaps': ['History'], 'stop_basis': 'unresolved_gap', 'next_action': None}})
    receipt['tool_receipts'] = receipt['tool_receipts'][1:]
    if fault == 'foreign_thread':
        receipt['native_discovery'][0]['thread_id'] = 'foreign'
    elif fault == 'no_search':
        receipt['native_discovery'] = []
    elif fault == 'no_read':
        receipt['tool_receipts'] = []
    elif fault == 'disabled_hosted':
        receipt['applied_controls']['profile_overrides']['web_search'] = 'disabled'
    elif fault == 'unsafe_tool':
        receipt['tool_receipts'].append({'name': 'bash', 'arguments': {}})
    receipt_path.write_text(json.dumps(receipt))
    evidence['sha256'] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(proof))
    if fault:
        with pytest.raises(ValueError):
            load_agent_qualification(path, routes=routes[:1])
    else:
        actual, metadata = load_agent_qualification(path, routes=routes[:1])
        assert actual == proof and metadata['qualified_provider_count'] == 1
