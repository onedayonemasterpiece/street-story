"""Accepted original confirmations survive REST and the acceptance measurement."""
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

from street_story.agent_evidence import CONTRACT, NETWORK_CONTRACT
from street_story.app import create_app
from street_story.config import reveal
from street_story.runtime import RuntimeStreetStoryService
from test_agent_evidence import AGENT, CANDIDATE, setup
from test_headless_facts import controlled_public_dns  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import owner_place_acceptance as harness


async def confirmed(tmp_path, contract):
    service, job, reader, _, calls = await setup(tmp_path)
    service.__class__ = RuntimeStreetStoryService
    original = service.providers.gemini.verify_agent_evidence

    async def verify(payload, *, checkpoint, guard):
        def closed(receipt):
            receipt['contract'] = contract
            if contract == NETWORK_CONTRACT:
                receipt['result']['checks'] = {
                    'source_physical_subject': 'The selected gate',
                    'object_relation': 'selected_building',
                    'relation_basis': {'fragment': 'The gate opened as a museum.',
                                       'chunk_ids': receipt['result']['chunk_ids']},
                    'material_kind': 'history', 'temporal_basis': 'historical',
                    'temporal_preserved': True}
            checkpoint(receipt)
        return await original(payload, checkpoint=closed, guard=guard)

    service.providers.gemini.verify_agent_evidence = verify
    result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
    assert result['status'] == 'supported'
    return service, job, reader, calls


async def rest_and_measure(service, job):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.settings, service)),
            base_url='http://projection', headers={
                'Authorization': 'Bearer ' + reveal(service.settings.device_token)}) as client:
        response = await client.get('/v1/stories/' + job['story_id'])
    assert response.status_code == 200
    visible = response.json()
    case = {'story_id': job['story_id'], 'selected_candidate_id': 'wiki:77',
            'eligible_facts': [{'assertion_id': fact['fact_id']} for fact in visible['facts']]}
    harness.enrich_evidence(service, case, visible)
    return visible, case


@pytest.mark.asyncio
@pytest.mark.parametrize('contract', [CONTRACT, NETWORK_CONTRACT])
async def test_original_confirmed_fact_rest_and_own_read_measurement(tmp_path, contract):
    service, job, reader, calls = await confirmed(tmp_path, contract)
    try:
        with service.store.connection() as db:
            original = db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0]
        visible, case = await rest_and_measure(service, job)
        fact = visible['facts'][0]
        assert fact['eligibility'] == 'eligible'
        assert fact['subject_scope'] == 'selected_building'
        assert fact['confirmation']['confirmation_attempt_id'] == 'verification-original'
        assert fact['confirmation']['confirmation_model'] == 'verifier-model'
        assert fact['confirmation']['confirmed_fragment'] == CANDIDATE['quote']
        assert fact['confirmation']['agent'] == AGENT
        assert case['rest_eligible_proved_count'] == 1
        assert case['own_read_supported_fact_count'] == 1
        assert len(calls) == 1
        with service.store.connection() as db:
            assert db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0] == original
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['unknown_contract', 'invalid_basis', 'missing_checks', 'reference_only', 'foreign_chunk'])
async def test_invalid_original_proof_is_not_exported_or_counted(tmp_path, damage):
    service, job, reader, calls = await confirmed(tmp_path, NETWORK_CONTRACT)
    try:
        with service.store.tx() as db:
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            operation = basis['operation']
            if damage == 'unknown_contract':
                operation['contract'] = 'unknown-confirmation-contract'
            elif damage == 'invalid_basis':
                basis['claim'] = 'A different unaccepted assertion'
                # No separate valid canonical admission may repair this damaged basis.
                db.execute('UPDATE poi_research_assertions SET review_proof_json=?', ('{}',))
            elif damage == 'missing_checks':
                del operation['confirmation_result']['checks']
            elif damage == 'reference_only':
                operation['confirmation_result']['checks']['material_kind'] = 'reference_only'
                operation['confirmation_checks'] = copy.deepcopy(operation['confirmation_result']['checks'])
            else:
                operation['confirmation_result']['chunk_ids'] = ['outside-owned-source']
            db.execute('UPDATE fact_assertions SET accepted_basis_json=?', (json.dumps(basis),))
        visible, case = await rest_and_measure(service, job)
        assert all('confirmation' not in fact for fact in visible['facts'])
        assert case['own_read_supported_fact_count'] == 0
        assert len(calls) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('inherited', [False, True])
async def test_older_v2_basis_projects_original_closed_receipt_and_bounded_excerpt(tmp_path, inherited):
    service, job, reader, calls = await confirmed(tmp_path, NETWORK_CONTRACT)
    try:
        original_story = job['story_id']
        if inherited:
            from test_identity_lifecycle import create
            original_story = create(service, client='original-canonical-proof')['id']
        with service.store.tx() as db:
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            operation = basis['operation']
            result = operation.pop('confirmation_result')
            long_fragment = ' '.join(['original'] * 33)
            result['confirmed_fragment'] = operation['confirmed_fragment'] = long_fragment
            from street_story.agent_evidence import confirmation_excerpt
            operation['excerpt_projection']['confirmed_fragment'] = confirmation_excerpt(long_fragment)
            receipt = {'contract': NETWORK_CONTRACT, 'phase': 'completed',
                       'provider_send_state': 'response_closed', 'result': result}
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                       (json.dumps(receipt), 'verification-original'))
            if inherited:
                db.execute('UPDATE research_provider_attempts SET story_id=? WHERE attempt_id=?',
                           (original_story, 'verification-original'))
                basis['operation'] = {'canonical_admission': {
                    'source_story_id': original_story, 'operation': operation}}
            db.execute('UPDATE fact_assertions SET accepted_basis_json=?', (json.dumps(basis),))
        visible, case = await rest_and_measure(service, job)
        confirmation = visible['facts'][0]['confirmation']
        assert len(confirmation['confirmed_fragment'].split()) == 25
        assert confirmation['confirmed_fragment_kind'] == 'model_confirmed_excerpt'
        assert confirmation['confirmed_fragment_truncated'] is True
        assert case['own_read_supported_fact_count'] == 1
        assert len(calls) == 1
        with service.store.connection() as db:
            stored = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            original_operation = stored['operation']
            if inherited:
                original_operation = original_operation['canonical_admission']['operation']
            assert original_operation['confirmed_fragment'] == long_fragment
            assert 'confirmation_result' not in original_operation
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unknown_confirmation_does_not_gain_rest_proof_on_reopen(tmp_path):
    service, job, reader, _, _ = await setup(tmp_path)
    service.__class__ = RuntimeStreetStoryService
    calls = []

    async def unknown(payload, *, checkpoint, guard):
        calls.append(payload)
        checkpoint({'contract': NETWORK_CONTRACT, 'attempt_id': 'original-unknown',
                    'phase': 'unknown', 'model_id': 'verifier-model'})
        return {'status': 'unverified'}

    service.providers.gemini.verify_agent_evidence = unknown
    try:
        await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        visible, case = await rest_and_measure(service, job)
        assert visible['facts'] == []
        assert case['own_read_supported_fact_count'] == 0
        assert len(calls) == 1
    finally:
        await reader.search_http.aclose()
