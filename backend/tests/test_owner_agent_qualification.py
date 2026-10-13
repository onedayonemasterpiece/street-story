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


def test_actual_role_receipts_require_current_profile_and_both_independent_services(tmp_path):
    path, routes = fixture(tmp_path)
    proof, _ = load_agent_qualification(path, routes=routes)
    assert len(proof['routes']) == 2
    routes[0].profile_fingerprint = 'changed'
    with pytest.raises(ValueError, match='profile changed'):
        load_agent_qualification(path, routes=routes)


@pytest.mark.parametrize('fault', ['tampered_receipt', 'legacy_role', 'unread_candidate', 'unsafe_tool', 'one_provider'])
def test_receipt_flags_cannot_replace_actual_read_only_role_integrity(tmp_path, fault):
    path, routes = fixture(tmp_path)
    proof = json.loads(path.read_text())
    evidence = proof['routes'][0]['qualification_receipts'][0]
    receipt_path = tmp_path / 'google.json'
    receipt = json.loads(receipt_path.read_text())
    if fault == 'one_provider':
        proof['routes'] = proof['routes'][:1]
    elif fault == 'tampered_receipt':
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
