from types import SimpleNamespace

import pytest

from test_facts_research_finish import fallback, findings, QUOTES
from street_story.service import ConflictError
from street_story.research_budget import PAGE_UNITS, response_units


@pytest.mark.asyncio
async def test_scoped_reconsideration_keeps_unrelated_review_and_requires_new_own_decision(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-scope', 'args': findings(chunk, QUOTES)})
    full = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': full['packet_ref'], 'decisions': [
        {'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in range(3)],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'initial-review', 'args': args})
    with svc.store.connection() as db:
        ids = [r[0] for r in db.execute('SELECT assertion_id FROM fact_assertions ORDER BY assertion_id')]
        old_receipt = db.execute('SELECT result_json FROM live_review_packets WHERE packet_ref=?', (full['packet_ref'],)).fetchone()[0]
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'fact_ids': [ids[1]]}})
    assert packet['total_facts'] == 1 and packet['items'][0]['saved_verdict'] is None
    assert all(c['fact_id'] != ids[1] for c in packet['nearby_existing_claims'])
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'narrow-review', 'args': {
        'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0],
            'verdict': 'insufficient', 'claims': [packet['items'][0]['text']]}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}})
    assert not result['complete']
    with svc.store.connection() as db:
        statuses = dict(db.execute('SELECT assertion_id,eligibility FROM fact_assertions'))
        assert statuses == {ids[0]: 'eligible', ids[1]: 'withheld', ids[2]: 'eligible'}
        assert db.execute('SELECT result_json FROM live_review_packets WHERE packet_ref=?', (full['packet_ref'],)).fetchone()[0] == old_receipt
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_scoped_review_rejects_foreign_claim_without_changing_eligibility(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-foreign', 'args': findings(chunk, QUOTES)})
    with svc.store.connection() as db:
        before = list(db.execute('SELECT assertion_id,eligibility FROM fact_assertions'))
    with pytest.raises(ConflictError, match='current own assertions'):
        await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'fact_ids': ['foreign-claim']}})
    with svc.store.connection() as db:
        assert list(db.execute('SELECT assertion_id,eligibility FROM fact_assertions')) == before
        assert db.execute('SELECT COUNT(*) FROM live_review_packets').fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_scoped_repair_followup_addresses_replacement_without_whole_inventory(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-repair-scope', 'args': findings(chunk, QUOTES)})
    with svc.store.connection() as db:
        fid = db.execute('SELECT assertion_id FROM fact_assertions ORDER BY assertion_id LIMIT 1').fetchone()[0]
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'fact_ids': [fid]}})
    text = packet['items'][0]['text']
    repaired = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'repair-small', 'args': {
        'packet_ref': packet['packet_ref'], 'repairs': [{'fact': 0, 'reason': 'Reattach exact own support.',
            'facts': [{'text': text, 'claim_key': 'own-scope', 'evidence': [0]}]}]}})
    assert repaired['next_args']['fact_ids'] == [fid]
    fresh = await adapter.execute_tool(session, {'name': repaired['next_tool'], 'args': repaired['next_args']})
    assert fresh['total_facts'] == 1 and fresh['items'][0]['text'] == text
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_repair_withholds_shared_parent_before_replacement_review_and_resumes_review(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-original', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'admit-original', 'args': {
        'packet_ref': packet['packet_ref'], 'decisions': [
            {'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in range(3)],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}})
    with svc.store.connection() as db:
        fid = db.execute('SELECT assertion_id FROM poi_research_assertions WHERE eligibility=\'eligible\' LIMIT 1').fetchone()[0]
    scoped = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'fact_ids': [fid]}})
    repaired = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'replace-meaning', 'args': {
        'packet_ref': scoped['packet_ref'], 'repairs': [{'fact': 0, 'reason': 'Narrow the independently reviewed meaning.',
            'facts': [{'text': 'Уточнённый первый тезис.', 'claim_key': 'narrowed', 'evidence': [0]}]}]}})
    child = repaired['next_args']['fact_ids'][0]
    with svc.store.connection() as db:
        assert {r[0] for r in db.execute('SELECT eligibility FROM poi_research_assertions WHERE assertion_id=?', (fid,))} == {'withheld'}
        assert db.execute('SELECT eligibility FROM fact_assertions WHERE assertion_id=?', (child,)).fetchone()[0] == 'unreviewed'
        assert db.execute('SELECT COUNT(*) FROM poi_research_assertions WHERE assertion_id=? AND eligibility=\'eligible\'', (child,)).fetchone()[0] == 0
    await adapter.execute_tool(session, {'name': repaired['next_tool'], 'args': repaired['next_args']})
    with svc.store.tx() as db:
        db.execute("UPDATE research_runs SET state='partial' WHERE run_id=?", (run_id,))
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model='gemini-3.8-live')
    assert initialized['capability'] == 'review'
    assert initialized['context']['research_run']['pending_review_fact_ids'] == [child]
    assert 'get_review_packet' in {t['name'] for t in initialized['configuration']['functions']}
    await reader.search_http.aclose()


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
async def test_unfinished_semantic_decisions_not_reused_as_final_review(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0], 'verdict': 'supported'}], 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'first', 'args': args})
    assert staged['remaining_facts'] == 2
    assert staged['next_tool'] == 'get_review_packet'
    remaining_page = await adapter.execute_tool(session, {
        'name': staged['next_tool'], 'args': staged['next_args']})
    assert remaining_page['packet_ref'] == packet['packet_ref']
    assert remaining_page['items'][0]['fact'] == 1
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
    replacement = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert all(i['saved_verdict'] is None for i in replacement['items'])
    args = {**args, 'packet_ref': replacement['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in [0, 1, 2]]}
    staged = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'rest', 'args': args})
    assert staged['remaining_facts'] == 0 and not staged['complete']
    assert staged['cross_packet_review_required']
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'relations', 'args': {**args, 'decisions': [], 'relations_complete': True}})
    assert result['complete'] and result['eligible_count'] == 3
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_new_policy_supersedes_wrong_completed_review_without_rewriting_receipt(tmp_path, monkeypatch):
    from street_story import review_packets
    from street_story.fact_ledger import refresh_review_status
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'supported'} for n in range(3)], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    original = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review1', 'args': args})
    assert original['eligible_count'] == 3
    monkeypatch.setattr(review_packets, 'POLICY_VERSION', 'test-new-policy')
    fresh = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id, 'supersedes_packet_ref': packet['packet_ref'], 'recheck_facts': [1]}})
    assert all(i['saved_verdict'] is None for i in fresh['items'])
    with svc.store.tx() as db:
        refresh_review_status(db, session.resource_id, svc.store.now())
        pending = db.execute('SELECT eligibility FROM fact_assertions WHERE story_id=? AND assertion_id=(SELECT assertion_id FROM fact_assertions WHERE story_id=? ORDER BY assertion_id LIMIT 1 OFFSET 1)', (session.resource_id, session.resource_id)).fetchone()[0]
        assert pending == 'unreviewed'
    new = {**args, 'packet_ref': fresh['packet_ref'], 'decisions': [{'fact': n, 'evidence': [0], 'verdict': 'insufficient' if n == 1 else 'supported'} for n in range(3)]}
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review2', 'args': new})
    assert result['eligible_count'] == 2 and result['withheld_count'] == 1
    replay = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'old-replay', 'args': args})
    assert replay == original
    assert sum(f['eligibility'] == 'eligible' for f in adapter._get_facts(session.resource_id, {})['facts']) == 2
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_packets WHERE result_json IS NOT NULL').fetchone()[0] == 2
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_equivalence_requires_explicit_support_and_supported_canonical(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    # Reflexive equivalence cannot silently become a positive support verdict.
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'eq-only', 'args': {**args, 'decisions': [{'fact': 0, 'evidence': [0], 'verdict': 'equivalent', 'equivalent_to': 0}]}})
    decisions = [{'fact': n, 'evidence': [0], 'verdict': 'supported', 'equivalent_to': 1 if n in [0, 1] else 2} for n in range(3)]
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'explicit-support', 'args': {**args, 'decisions': decisions}})
    assert result['complete'] and result['eligible_count'] == 2 and result['withheld_count'] == 1
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_own_quote_and_decomposition_checks_reject_borrowed_or_compound_positive(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save-negative-test', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    item = packet['items'][0]
    args = {'packet_ref': packet['packet_ref'], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    foreign_quote = next(quote for quote in QUOTES if quote != item['text'])
    for decision in [
        {'fact': 0, 'evidence': [0], 'verdict': 'supported', 'claims': [item['text']], 'basis_quotes': [foreign_quote]},
        {'fact': 0, 'evidence': [0], 'verdict': 'supported', 'claims': ['First person.', 'Second person.'], 'basis_quotes': [item['text']]},
    ]:
        with pytest.raises(ConflictError):
            await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'wrong-basis', 'args': {**args, 'decisions': [decision]}})
    assert all(f['eligibility'] == 'unreviewed' for f in adapter._get_facts(session.resource_id, {})['facts'])
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_resumed_review_frames_inventory_as_candidates_and_preserves_canonical_identity(tmp_path):
    import json
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    with svc.store.tx() as db:
        db.execute("UPDATE research_runs SET state='verifying' WHERE run_id=?", (run_id,))
        row = db.execute('SELECT research_json FROM stories WHERE id=?', (session.resource_id,)).fetchone()
        research = json.loads(row[0])
        identity = research['visual_identity']
        identity['candidate_id'] = 'confirmed-place'
        identity['candidate_name'] = 'Бранденбургские ворота (Калининград)'
        identity['candidates'] = [{'candidate_id': 'confirmed-place', 'name': identity['candidate_name'], 'entity_aliases': ['Brandenburger Tor, Kaliningrad']}]
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model='gemini-3.8-live')
    assert initialized['context']['facts'] == [] and initialized['context']['candidate_count'] == 3
    assert initialized['context']['visual_identity']['canonical_name'] == 'Бранденбургские ворота (Калининград)'
    assert initialized['context']['visual_identity']['aliases'] == ['Brandenburger Tor, Kaliningrad']
    assert initialized['context']['poi_location'] == {'latitude': 54.7, 'longitude': 20.5}
    assert initialized['configuration']['system_instruction'].startswith('Current phase: independent verification')
    assert initialized['capability'] == 'review'
    assert 'get_review_packet' in {tool['name'] for tool in initialized['configuration']['functions']}
    assert 'continue_story' in {tool['name'] for tool in initialized['configuration']['functions']}
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    assert packet['total_facts'] == 3
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_insufficient_can_withhold_without_inventing_supporting_evidence(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': findings(chunk, QUOTES)})
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': n, 'evidence': [] if n == 0 else [0], 'verdict': 'insufficient' if n == 0 else 'supported'} for n in range(3)], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review', 'args': args})
    assert result['complete'] and result['eligible_count'] == 2 and result['withheld_count'] == 1
    replay = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'again', 'args': args})
    assert replay == result
    await reader.search_http.aclose()
