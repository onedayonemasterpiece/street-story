from types import SimpleNamespace

import pytest

from test_facts_research_finish import fallback, findings, QUOTES
from street_story.service import ConflictError
from street_story.research_budget import PAGE_UNITS, response_units


@pytest.mark.asyncio
async def test_short_packet_semantics_negative_scope_stale_and_replay(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert packet['total_facts'] == 3 and response_units('get_review_packet', packet) <= PAGE_UNITS
    assert 'revision_digest' not in str(packet)
    foreign = SimpleNamespace(resource_id=session.resource_id, state={}, actor={'sub': 'another-owner'})
    with pytest.raises(ConflictError):
        await adapter.execute_tool(foreign, {'name': 'get_review_packet', 'args': {'packet_ref': packet['packet_ref']}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'role_mismatch' if n == 1 else 'supported'} for n in range(3)], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    invalid = {**args, 'decisions': [{'fact': 0, 'evidence': [999], 'verdict': 'supported'}]}
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'invalid', 'args': invalid})
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review', 'args': args})
    assert result['complete'] and result['eligible_count'] == 2 and result['withheld_count'] == 1
    replay = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'replay', 'args': args})
    assert replay == result
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_packet_revision_and_cross_page_gate(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in range(3)], 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'stage', 'args': args})
    assert staged['review_saved'] and not staged['complete']
    assert all(f['eligibility'] == 'unreviewed' for f in adapter._get_facts(session.resource_id, {})['facts'])
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'stale', 'args': {**args, 'relations_complete': True}})
    assert error.value.code == 'live_review_packet_stale'
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_241_assertions_finish_via_bounded_decisions(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES[:1])})
    # Structural boundary only; no 241-fact semantic online success claimed.
    with svc.store.tx() as db:
        originals = {table: dict(db.execute('SELECT * FROM ' + table + ' LIMIT 1').fetchone()) for table in ['facts', 'fact_assertions', 'fact_observations', 'fact_evidence_spans']}
        for n in range(240):
            for table, original in originals.items():
                item = dict(original)
                for key, prefix in [('fact_id', 'large-f'), ('assertion_id', 'large-f'), ('observation_id', 'large-o'), ('evidence_id', 'large-e')]:
                    if key in item:
                        item[key] = prefix + str(n)
                cols = ','.join(item)
                db.execute('INSERT INTO ' + table + '(' + cols + ') VALUES(' + ','.join('?' for _ in item) + ')', tuple(item.values()))
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert packet['total_facts'] == 241 and packet['has_more']
    cursor, seen = 0, set()
    while True:
        page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'packet_ref': packet['packet_ref'], 'cursor': cursor}})
        assert response_units('get_review_packet', page) <= PAGE_UNITS
        seen.update(i['fact'] for i in page['items'])
        if not page['has_more']:
            break
        cursor = page['next_cursor']
    assert len(seen) == 241
    args = {'packet_ref': packet['packet_ref'], 'conflicts': [], 'coverage_complete': True, 'missing_aspects': [], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in range(240)]}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'first-240', 'args': args})
    assert staged['remaining_facts'] == 1 and not staged['complete']
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'last', 'args': {**args, 'decisions': [{'fact': 240, 'evidence': [0], 'verdict': 'supported'}], 'relations_complete': True}})
    assert result['complete'] and result['eligible_count'] == 241
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_unchanged_semantic_decisions_reused_without_claiming_cross_coverage(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0], 'verdict': 'supported'}], 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'first', 'args': args})
    assert staged['remaining_facts'] == 2
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
    replacement = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert any(i['fact'] == 0 and i['saved_verdict'] == 'supported' for i in replacement['items'])
    args = {**args, 'packet_ref': replacement['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in [1, 2]]}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'rest', 'args': args})
    assert staged['remaining_facts'] == 0 and not staged['complete']
    assert staged['cross_packet_review_required']
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'relations', 'args': {**args, 'decisions': [], 'relations_complete': True}})
    assert result['complete'] and result['eligible_count'] == 3
    await reader.search_http.aclose()
