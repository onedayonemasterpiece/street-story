"""One model response, frozen relationship proof, and ordinary ledger recovery."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story.agent_evidence import (
    CONTRACT, NETWORK_CONTRACT, NETWORK_SCHEMA, SCHEMA, confirmation_consistency_error,
    verify_with_gemini,
)
from test_agent_evidence import AGENT, CANDIDATE, setup


def response(payload, *, legacy=False):
    cid = payload['source']['chunks'][0]['chunk_id']
    row = {'verdict': 'supported', 'text': payload['candidate']['text'],
        'claim_key': 'controlled-claim', 'subject_scope': 'selected_building',
        'confirmed_fragment': payload['candidate']['quote'], 'source_date': '2005',
        'chunk_ids': [cid], 'reason': 'Controlled model assessment.',
        'existing_fact_id': '', 'conflicts_with_existing': []}
    if not legacy:
        row['checks'] = {'source_physical_subject': 'Controlled selected building',
            'object_relation': 'selected_building',
            'relation_basis': {'fragment': 'The described place is this building.', 'chunk_ids': [cid]},
            'material_kind': 'history', 'temporal_basis': 'historical', 'temporal_preserved': True}
    return row


async def controlled(tmp_path, change=lambda _row: None, *, legacy=False, after=None):
    service, job, reader, fetches, _ = await setup(tmp_path)
    calls = []

    async def verify(payload, *, checkpoint, guard):
        guard()
        calls.append(copy.deepcopy(payload))
        row = response(payload, legacy=legacy)
        change(row)
        receipt = {'attempt_id': 'original-controlled-confirmation', 'phase': 'completed',
            'provider_id': 'google', 'model_id': 'controlled-model',
            'contract': CONTRACT if legacy else NETWORK_CONTRACT,
            'provider_send_state': 'response_closed', 'result': row}
        checkpoint(receipt)
        if after:
            after()
        return {'result': row, 'receipt': receipt}

    service.providers.gemini.verify_agent_evidence = verify
    return service, job, reader, fetches, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [
    ('unrelated', 'confirmation_object_relation_unestablished'),
    ('unestablished', 'confirmation_object_relation_unestablished'),
    ('site_scope', 'confirmation_object_scope_inconsistent'),
    ('reference', 'confirmation_reference_only'),
    ('time', 'confirmation_temporal_inconsistent'),
    ('modality', 'confirmation_temporal_inconsistent'),
    ('relation_chunk', 'confirmation_relationship_references_invalid'),
    ('relation_quote', 'confirmation_relationship_references_invalid'),
    ('fact_quote', 'confirmation_references_invalid'),
    ('missing_checks', 'confirmation_incomplete'),
    ('malformed_checks', 'confirmation_incomplete'),
])
async def test_supported_but_inconsistent_model_decisions_never_enter_ledger(tmp_path, case, reason):
    def change(row):
        checks = row['checks']
        if case in {'unrelated', 'unestablished'}:
            checks['object_relation'] = case
        elif case == 'site_scope':
            checks['object_relation'] = 'documented_site'
        elif case == 'reference':
            checks['material_kind'] = 'reference_only'
        elif case == 'time':
            checks['temporal_basis'] = 'unestablished'
        elif case == 'modality':
            checks['temporal_preserved'] = False
        elif case == 'relation_chunk':
            checks['relation_basis']['chunk_ids'] = ['other-version-chunk']
        elif case == 'relation_quote':
            checks['relation_basis']['fragment'] = 'word ' * 26
        elif case == 'fact_quote':
            row['confirmed_fragment'] = 'word ' * 26
        elif case == 'missing_checks':
            del row['checks']
        else:
            row['checks'] = {'object_relation': 'selected_building'}
    service, job, reader, _, calls = await controlled(tmp_path, change)
    try:
        result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert result['status'] == 'unverified' and result['reason'] == reason
        assert service.story(job['story_id'])['facts'] == []
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM fact_assertions').fetchone()[0] == 0
            original = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0])
            assert original['result']['verdict'] == 'supported'
            assert original['contract'] == NETWORK_CONTRACT
        await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert len(calls) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('temporal,site', [('historical', False), ('planned', False), ('timeless', True)])
async def test_consistent_model_result_keeps_scope_date_and_normal_accepted_basis(tmp_path, temporal, site):
    def change(row):
        row['checks']['temporal_basis'] = temporal
        if site:
            row['checks']['object_relation'] = 'documented_site'
            row['subject_scope'] = 'complex_or_site'
    service, job, reader, fetches, calls = await controlled(tmp_path, change)
    try:
        first = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert first['status'] == 'supported'
        fact = service.story(job['story_id'])['facts'][0]
        assert fact['eligibility'] == 'eligible' and fact['sources'][0]['source_date'] == '2005'
        assert fact['sources'][0]['confirmation_checks']['temporal_basis'] == temporal
        with service.store.connection() as db:
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            assert basis['operation']['contract'] == NETWORK_CONTRACT
            assert basis['operation']['confirmation_checks']['object_relation'] == ('documented_site' if site else 'selected_building')
            assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'eligible'
        assert await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT) == first
        assert len(calls) == len(fetches) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_closed_v1_recovery_keeps_original_contract_without_network_or_new_checks(tmp_path):
    def interrupt():
        raise RuntimeError('after original closed response')
    service, job, reader, _, calls = await controlled(tmp_path, legacy=True, after=interrupt)
    try:
        with pytest.raises(RuntimeError):
            await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        async def no_read(*_args, **_kwargs):
            raise AssertionError('No new source read or inference on recovery')
        service.providers.gemini._fetch_page_documents = no_read
        result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert result['status'] == 'supported' and len(calls) == 1
        with service.store.connection() as db:
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            receipt = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts').fetchone()[0])
            assert basis['operation']['contract'] == receipt['contract'] == receipt['binding']['contract'] == CONTRACT
            assert 'confirmation_checks' not in basis['operation']
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_relationship_chunk_is_frozen_in_the_same_accepted_basis(tmp_path):
    def change(row):
        row['checks']['relation_basis']['chunk_ids'] = ['relationship-chunk']
    service, job, reader, _, _ = await controlled(tmp_path, change)
    try:
        fetch = reader._fetch_page_documents
        async def two_chunks(*args, **kwargs):
            docs = await fetch(*args, **kwargs)
            with service.store.tx() as db:
                db.execute('INSERT INTO source_chunks SELECT ?,source_version_id,ordinal+1,core_start,core_end,'
                    'context_start,context_end,chunk_text,created_at FROM source_chunks LIMIT 1', ('relationship-chunk',))
            return docs
        service.providers.gemini._fetch_page_documents = two_chunks
        result = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert result['status'] == 'supported'
        with service.store.connection() as db:
            spans = list(db.execute('SELECT chunk_id,evidence_id FROM fact_evidence_spans'))
            ids = {r[0] for r in spans}
            assert 'relationship-chunk' in ids and len(ids) == 2
            basis = json.loads(db.execute('SELECT accepted_basis_json FROM fact_assertions').fetchone()[0])
            assert {ev['evidence_id'] for ev in basis['evidence']} == {r[1] for r in spans}
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_v2_unknown_keeps_original_operation_after_contract_change(tmp_path):
    service, job, reader, _, _ = await controlled(tmp_path)
    calls = []
    async def unknown(payload, *, checkpoint, guard):
        calls.append(payload)
        checkpoint({'attempt_id': 'same-unknown', 'contract': NETWORK_CONTRACT,
            'phase': 'unknown', 'provider_send_state': 'possibly_sent'})
        return {'status': 'unverified'}
    service.providers.gemini.verify_agent_evidence = unknown
    try:
        await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        second = await service.confirm_agent_fact(job, 'headless-run', CANDIDATE, AGENT)
        assert second['reason'] == 'original_confirmation_pending' and len(calls) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [False, True])
async def test_actual_google_wire_requires_v2_checks_and_retains_original_raw_result(missing):
    payload = {'candidate': CANDIDATE, 'source': {'source_version_id': 'v', 'content_sha256': 'sha',
        'chunks': [{'chunk_id': 'c'}]}}
    raw = response(payload)
    if missing:
        del raw['checks']
    captured, receipts = [], []
    async def generate(_key, _timeout, contents, config, **kwargs):
        captured.append(config.model_dump(mode='json', exclude_none=True))
        kwargs['before_provider_send']()
        return SimpleNamespace(text=json.dumps(raw), usage_metadata=None)
    async def execute(_operation, call, **_kwargs):
        return await call('controlled-key', 1)
    client = SimpleNamespace(_generate=generate, store=SimpleNamespace(now=lambda: 10),
        research_routes=[('controlled-model', None, None, SimpleNamespace(execute=execute))])
    result = await verify_with_gemini(client, payload, lambda r: receipts.append(copy.deepcopy(r)), lambda: None)
    schema = captured[0]['response_json_schema']
    assert schema['required'][0] == 'checks' and list(schema['properties'])[0] == 'checks'
    assert receipts[-1]['contract'] == NETWORK_CONTRACT and receipts[-1]['schema_sha256']
    assert receipts[-1]['raw_result'] == raw
    if missing:
        assert 'result' not in result and receipts[-1]['phase'] == 'failed'
    else:
        assert result['result'] == raw


def test_network_check_requirement_does_not_mutate_legacy_schema():
    payload = {'candidate': CANDIDATE, 'source': {'chunks': [{'chunk_id': 'c'}]}}
    legacy = response(payload, legacy=True)
    assert 'checks' not in SCHEMA['properties'] and 'checks' not in SCHEMA['required']
    assert confirmation_consistency_error(legacy, {'contract': CONTRACT}, {'c'}) is None
    assert confirmation_consistency_error(legacy, {'contract': NETWORK_CONTRACT}, {'c'}) == 'confirmation_incomplete'
    assert NETWORK_SCHEMA['required'][0] == 'checks'
