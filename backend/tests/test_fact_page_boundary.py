import copy
import json
from types import SimpleNamespace

import pytest

from street_story.fact_page_boundary import isolate_rows
from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import FACT_PAGE_SCHEMA
from test_headless_fact_pool import fixture, result, partial


def test_bad_row_isolation_keeps_raw_and_blocks_relation_dependents():
    good = {'text': 'Own claim', 'existing_fact_id': '', 'confidence': .9, 'passage_ids': [1],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'review_reason': 'Own evidence'}
    raw = {'facts': [good, {**good, 'text': '', 'existing_fact_id': 'old-A'},
        {**good, 'text': 'Dependent claim', 'conflicts_with_existing': ['old-A']}],
        'source_matches_poi': True, 'source_content_valid': True, 'continuation_needed': False}
    original = copy.deepcopy(raw)
    isolated = isolate_rows(raw, FACT_PAGE_SCHEMA, passage_ids={1})
    assert isolated['result']['facts'] == [good]
    assert isolated['row_isolation']['accepted_indices'] == [0]
    assert isolated['row_isolation']['rejected_indices'] == [1]
    assert isolated['row_isolation']['dependent_indices'] == [2]
    assert isolated['raw_result'] == raw == original
    assert isolated['result']['continuation_needed'] is False
    assert isolate_rows({**raw, 'facts': [raw['facts'][1], raw['facts'][2]]}, FACT_PAGE_SCHEMA) is None


def test_root_or_relation_failure_cannot_be_repaired_by_row_isolation():
    raw = {'facts': [], 'source_matches_poi': True, 'source_content_valid': True}
    assert isolate_rows(raw, FACT_PAGE_SCHEMA) is None


def test_two_boundaries_keep_original_indices_and_original_raw():
    from street_story.fact_page_boundary import isolate_result
    good = {'text': 'Own claim', 'existing_fact_id': '', 'confidence': .9, 'passage_ids': [1],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'review_reason': 'Own evidence'}
    raw = {'facts': [{**good, 'text': ''}, good, {**good, 'text': 'Foreign passage', 'passage_ids': [99]}],
        'source_matches_poi': True, 'source_content_valid': True, 'continuation_needed': False}
    first = isolate_rows(raw, FACT_PAGE_SCHEMA)
    envelope = {'result': first['result'], 'receipt': {k: first[k] for k in ('raw_result', 'row_isolation')}}
    second = isolate_result(envelope, FACT_PAGE_SCHEMA, passage_ids={1})
    assert second['result']['facts'] == [good]
    assert second['raw_result'] == raw
    assert second['row_isolation']['accepted_indices'] == [1]
    assert second['row_isolation']['rejected_indices'] == [0, 2]
    assert isolate_result(second, FACT_PAGE_SCHEMA, passage_ids={1}) == second


@pytest.mark.asyncio
async def test_live_boundary_keeps_independent_closed_rows_before_whole_schema_gate(tmp_path):
    from test_autonomous_live_research import client, binding
    from test_mvp_research import service
    svc, _, story = service(tmp_path)
    good = {'text': 'Own claim', 'existing_fact_id': '', 'confidence': .9, 'passage_ids': [1],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'review_reason': 'Own evidence'}
    raw = {'facts': [good, {**good, 'text': '', 'claim_key': 'bad'}],
        'source_matches_poi': True, 'source_content_valid': True, 'continuation_needed': False}
    provider, calls = client(svc, 'valid', tool_args=raw)
    owned = {**binding(svc, story), 'semantic_contract': 'source-reading-assessment-v2'}
    closed = await provider._run('facts', 'Frozen own evidence', owned, FACT_PAGE_SCHEMA)
    assert closed['result']['facts'] == [good]
    assert closed['receipt']['raw_result'] == raw
    assert closed['receipt']['row_isolation']['rejected_indices'] == [1]
    assert closed['receipt']['text_sends'] == 1
    assert [name for name, _ in calls] == ['start', 'input', 'stop']


@pytest.mark.asyncio
async def test_live_review_boundary_keeps_original_mixed_rows_for_own_validator(tmp_path):
    from test_autonomous_live_research import client, binding
    from test_mvp_research import service
    from street_story.live import FUNCTIONS
    svc, _, story = service(tmp_path)
    schema = next(tool['parameters'] for tool in FUNCTIONS if tool['name'] == 'finalize_fact_review')
    raw = {'packet_ref': 'frozen-own-packet', 'decisions': [
        {'fact': 0, 'evidence': [0], 'verdict': 'supported', 'reason': 'Own passage.',
            'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
            'claims': ['Own claim'], 'basis_quotes': ['Own claim']},
        {'fact': 1, 'evidence': [0], 'verdict': 'invalid-verdict'}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    provider, calls = client(svc, 'valid', tool_args=raw)
    owned = {**binding(svc, story), 'semantic_contract': 'own-evidence-review-rows-v1'}
    closed = await provider._run('facts', 'Frozen own evidence', owned, schema)
    assert closed['result'] == closed['receipt']['raw_result'] == raw
    assert closed['receipt']['row_validation_pending'] is True
    assert closed['receipt']['text_sends'] == 1
    assert [name for name, _ in calls] == ['start', 'input', 'stop']
    raw.update(continuation_needed=False, facts=[{'conflicts_with_existing': {'unknown': 'scope'}}])
    assert isolate_rows(raw, FACT_PAGE_SCHEMA) is None


@pytest.mark.asyncio
async def test_closed_assessment_commits_independent_row_once_and_retains_partial_error(tmp_path):
    svc, job = fixture(tmp_path, count=1)
    calls, raw = [], []
    async def extract(page, story, context):
        response = result(page)
        original = response['result']['facts'][0]
        response['result']['facts'].extend([
            {**original, 'text': '', 'claim_key': 'invalid', 'existing_fact_id': 'old-A'},
            {**original, 'text': 'Dependent claim', 'claim_key': 'dependent', 'conflicts_with_existing': ['old-A']}])
        response['result'].update(research_sufficient=True, research_sufficient_basis={'candidate_indices': [1]})
        response['receipt']['binding'] = {'semantic_contract': 'source-reading-assessment-v2'}
        calls.append(page['chunk_id'])
        raw.append(copy.deepcopy(response['result']))
        return response
    svc.providers.research = SimpleNamespace(client=SimpleNamespace(model_id='controlled-model'), extract_fact_page=extract)
    harness = HeadlessFacts(svc)
    await partial(harness, job)
    await partial(harness, job)
    with svc.store.connection() as db:
        facts = db.execute('SELECT * FROM fact_assertions').fetchall()
        assert len(facts) == 1 and facts[0]['eligibility'] == 'eligible'
        assert db.execute("SELECT count(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 1
        assert db.execute('SELECT status,error_code FROM research_chunk_runs').fetchone()[:] == ('failed', 'malformed_fact_rows')
        saved = json.loads(db.execute("SELECT value_json FROM research_checkpoints WHERE stage LIKE 'headless_fact_result:%'").fetchone()[0])['extracted']
        assert saved['raw_result'] == raw[0]
        assert saved['row_isolation']['dependent_indices'] == [2]
    assert len(calls) == 1
    assert svc.story(job['story_id'])['facts'][0]['selected'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_field', ['text', 'evidence_quotes'])
async def test_interactive_save_preserves_good_own_row_and_marks_bad_row_partial(tmp_path, bad_field):
    from test_facts_research_finish import fallback, findings, QUOTES
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    try:
        chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
        args = findings(chunk, QUOTES)
        bad = args['facts'][1]
        bad[bad_field] = '' if bad_field == 'text' else ['A quotation outside this frozen source.']
        original = copy.deepcopy(args)
        value = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'mixed-save', 'args': args})
        assert len(value['facts']) == 2 and args == original
        assert value['completed'] is False
        with svc.store.connection() as db:
            assert db.execute('SELECT status,error_code FROM research_chunk_runs').fetchone()[:] == ('failed', 'malformed_fact_rows')
            batch = json.loads(db.execute('SELECT payload_json FROM research_chunk_batches').fetchone()[0])
            assert batch['raw_rows'] == original['facts']
            assert sum(len(stage['rejected_indices']) for stage in batch['row_isolation'].values()) == 1
        replay = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'mixed-save-again', 'args': original})
        assert replay == value
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_mixed_review_accepts_only_independent_own_decision_and_preserves_conflict_scope(tmp_path):
    from test_facts_research_finish import fallback, findings, QUOTES
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    try:
        chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
        await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'before-mixed-review', 'args': findings(chunk, QUOTES)})
        packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
        assert packet['total_facts'] == 3
        with svc.store.connection() as db:
            payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?', (packet['packet_ref'],)).fetchone()[0])
        decisions = [{'fact': i, 'evidence': [0], 'verdict': 'supported', 'claims': [item['text']],
            'basis_quotes': [item['evidence'][0]['text']], 'atomic': True, 'support_complete': True,
            'qualifiers_preserved': True} for i, item in enumerate(payload['items'])]
        decisions[1]['basis_quotes'] = ['Foreign literal quote.']
        decisions[2]['equivalent_to'] = 1
        args = {'packet_ref': packet['packet_ref'], 'decisions': decisions,
            'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
        original = copy.deepcopy(args)
        value = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'mixed-review', 'args': args})
        assert value['eligible_count'] == 1 and value['unreviewed_count'] == 2
        assert value['complete'] is False and value['coverage_complete'] is False
        assert args == original
        with svc.store.connection() as db:
            rejection = json.loads(db.execute('SELECT payload_json FROM live_review_row_rejections').fetchone()[0])
            assert rejection['raw_request'] == original
            assert rejection['row_isolation']['dependent_indices'] == [2]
            assert db.execute('SELECT count(*) FROM poi_research_assertions WHERE eligibility=\'eligible\'').fetchone()[0] == 1
        replay = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'mixed-review-replay', 'args': original})
        assert replay == value
    finally:
        await reader.search_http.aclose()
