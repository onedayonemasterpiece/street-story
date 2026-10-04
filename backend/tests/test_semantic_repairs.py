from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from test_facts_research_finish import fallback
from street_story.service import ConflictError
from street_story.research_runs import chunk_checkpoint


async def saved_candidates(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    body = (Path(__file__).parents[1] / 'tools/fixtures/facts-research/royal-gates-wikipedia-20261003.txt').read_text().splitlines()
    antecedent = body[9]
    shop = body[11]
    facade = next(line for line in body if 'горельефы короля Чехии' in line)
    document = '\n'.join([antecedent, shop, facade])
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/html'}, text='<main>' + document.replace('\n', '<br>') + '</main>')))
    chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    while chunk['has_more_passages']:
        chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
    recipe = {key: chunk[key] for key in ('chunk_id', 'batch_id', 'batch_index', 'expected_story_revision')}
    recipe.update(run_id=run_id, inventory_reviewed=True, facts=[
        {'claim_key': 'dated-shop', 'text': 'В 1976 году в Королевских воротах разместился книжный магазин № 6.', 'confidence': .95, 'selected': True, 'evidence_quotes': [shop.replace('\xa0', ' ')]},
        {'claim_key': 'bundled-figures', 'text': 'На фасаде изображены Отакар II, Фридрих I и герцог Альбрехт I.', 'confidence': .95, 'selected': True, 'evidence_quotes': [facade.replace('\xa0', ' ')]},
    ])
    await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'save', 'args': recipe})
    return svc, adapter, session, run_id, reader, chunk


async def packet_items(adapter, session, run_id):
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': run_id}})
    items = list(packet['items'])
    while packet['has_more']:
        packet = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': packet['next_args']})
        items.extend(packet['items'])
    return packet, items


@pytest.mark.asyncio
async def test_context_repairs_anaphora_then_split_preserves_lineage_selection_and_replay(tmp_path):
    svc, adapter, session, run_id, reader, chunk = await saved_candidates(tmp_path)
    with svc.store.connection() as db:
        before = chunk_checkpoint(db, run_id, chunk['chunk_id'])
    packet, items = await packet_items(adapter, session, run_id)
    dated = next(item for item in items if '1976' in item['text'])
    ctx_args = {'packet_ref': packet['packet_ref'], 'fact': dated['fact'], 'evidence': 0}
    context = await adapter.execute_tool(session, {'name': 'get_review_context', 'args': ctx_args})
    contexts = list(context['passages'])
    while context['has_more']:
        context = await adapter.execute_tool(session, {'name': 'get_review_context', 'args': context['next_args']})
        contexts.extend(context['passages'])
    antecedent = next(item for item in contexts if '1976' in item['text'])
    args = {'packet_ref': packet['packet_ref'], 'fact': dated['fact'], 'reason': 'Attach the literal antecedent; preserve the same claim and selection.', 'facts': [{'text': dated['text'], 'claim_key': 'dated-shop', 'evidence': [0], 'context_refs': [antecedent['context_ref']]}]}
    repaired = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'repair1', 'args': args})
    assert repaired['selection_preserved']
    replay = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'repair1-again', 'args': args})
    assert replay == repaired
    packet, items = await packet_items(adapter, session, run_id)
    combined = next(item for item in items if 'Отакар' in item['text'])
    children = [{'text': text, 'claim_key': f'figure-{n}', 'evidence': [0]} for n, text in enumerate(['На фасаде изображён король Чехии Отакар II.', 'На фасаде изображён король Пруссии Фридрих I.', 'На фасаде изображён герцог Пруссии Альбрехт I.'])]
    split = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'split', 'args': {'packet_ref': packet['packet_ref'], 'fact': combined['fact'], 'reason': 'Separate independently selectable depictions.', 'facts': children}})
    assert not split['selection_preserved'] and len(split['fact_ids']) == 3
    assert all(not f['owner_selected'] for f in adapter._get_facts(session.resource_id, {})['facts'] if f['fact_id'] in split['fact_ids'])
    packet, items = await packet_items(adapter, session, run_id)
    assert packet['total_facts'] == 4
    result = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review', 'args': {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': n, 'evidence': sorted({item['evidence'] for item in items if item['fact'] == n}), 'verdict': 'supported'} for n in range(4)], 'relations_complete': True, 'conflicts': [], 'coverage_complete': True, 'missing_aspects': []}})
    assert result['complete'] and result['eligible_count'] == 4
    with svc.store.connection() as db:
        assert chunk_checkpoint(db, run_id, chunk['chunk_id']) == before
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 4
        parent = db.execute('SELECT review_status,owner_selected FROM fact_assertions WHERE assertion_id=?', (split['parent_fact_id'],)).fetchone()
        assert parent['review_status'] == 'withheld' and not parent['owner_selected']
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_context_and_late_repair_reject_foreign_scope_or_changed_revision(tmp_path):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    packet, items = await packet_items(adapter, session, run_id)
    args = {'packet_ref': packet['packet_ref'], 'fact': items[0]['fact']}
    foreign = SimpleNamespace(resource_id=session.resource_id, actor={'sub': 'foreign'}, state={})
    with pytest.raises(ConflictError):
        await adapter.execute_tool(foreign, {'name': 'get_review_context', 'args': args})
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'repair_research_fact', 'id': 'late', 'args': {**args, 'reason': 'Late model reply', 'facts': [{'text': 'Новое утверждение.', 'claim_key': 'late', 'evidence': [0]}]}})
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_repair_invalidates_dependent_outputs_and_does_not_select_new_meanings(tmp_path):
    import json
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    packet, items = await packet_items(adapter, session, run_id)
    combined = next(item for item in items if 'Отакар' in item['text'])
    dated = next(item for item in items if '1976' in item['text'])
    with svc.store.tx() as db:
        row = db.execute('SELECT research_json FROM stories WHERE id=?', (session.resource_id,)).fetchone()
        research = json.loads(row[0])
        assertion = db.execute('SELECT assertion_id,revision_digest FROM fact_assertions WHERE story_id=? AND display_text=?', (session.resource_id, combined['text'])).fetchone()
        parent_id, digest = assertion['assertion_id'], assertion['revision_digest']
        research['draft_fact_revisions'] = {parent_id: digest}
        db.execute('UPDATE stories SET research_json=?,visual_context_json=? WHERE id=?', (json.dumps(research), json.dumps({'fact_revision_bundle': {parent_id: digest}}), session.resource_id))
    result = await adapter.execute_tool(session, {'name': 'repair_research_fact', 'args': {'packet_ref': packet['packet_ref'], 'fact': combined['fact'], 'reason': 'Narrow to one depiction.', 'facts': [{'text': 'На фасаде изображён Отакар II.', 'claim_key': 'one', 'evidence': [0]}]}})
    with svc.store.connection() as db:
        story = db.execute('SELECT research_json,visual_context_json FROM stories WHERE id=?', (session.resource_id,)).fetchone()
        assert json.loads(story[0])['draft_needs_refresh']
        assert json.loads(story[1])['stale']
        assert not db.execute('SELECT owner_selected FROM fact_assertions WHERE assertion_id=?', (result['fact_ids'][0],)).fetchone()[0]
        assert db.execute('SELECT owner_selected FROM fact_assertions WHERE story_id=? AND display_text=?', (session.resource_id, dated['text'])).fetchone()[0]
    await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('child', [None, {'text': 'X', 'claim_key': 'x', 'evidence': [0], 'confidence': float('nan')}, {'text': 'X', 'claim_key': 'x', 'evidence': [0], 'confidence': 'certain'}])
async def test_malformed_repair_is_atomic_and_leaves_original_observations(tmp_path, child):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    packet, items = await packet_items(adapter, session, run_id)
    with svc.store.connection() as db:
        before = db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0]
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'repair_research_fact', 'args': {'packet_ref': packet['packet_ref'], 'fact': items[0]['fact'], 'reason': 'Malformed model reply.', 'facts': [child]}})
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == before
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_cancelled_context_cannot_be_used_for_late_repair(tmp_path):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    packet, items = await packet_items(adapter, session, run_id)
    session.state['research_cancelled'] = True
    for name, args in [('get_review_context', {'packet_ref': packet['packet_ref'], 'fact': items[0]['fact']}), ('repair_research_fact', {'packet_ref': packet['packet_ref'], 'fact': items[0]['fact'], 'reason': 'Late after Stop.', 'facts': [{'text': 'X', 'claim_key': 'x', 'evidence': [0]}]})]:
        with pytest.raises(ConflictError):
            await adapter.execute_tool(session, {'name': name, 'args': args})
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_batch_repair_rolls_back_every_parent_when_one_reference_is_invalid(tmp_path):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    packet, items = await packet_items(adapter, session, run_id)
    args = {'packet_ref': packet['packet_ref'], 'repairs': [
        {'fact': items[0]['fact'], 'reason': 'Valid first change', 'facts': [{'text': 'Новая формулировка.', 'claim_key': 'new', 'evidence': [0]}]},
        {'fact': items[-1]['fact'], 'reason': 'Invalid second change', 'facts': [{'text': 'Другая формулировка.', 'claim_key': 'other', 'evidence': [999]}]},
    ]}
    with svc.store.connection() as db:
        before = db.execute('SELECT revision FROM stories WHERE id=?', (session.resource_id,)).fetchone()[0]
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'name': 'repair_research_fact', 'args': args})
    with svc.store.connection() as db:
        assert db.execute('SELECT revision FROM stories WHERE id=?', (session.resource_id,)).fetchone()[0] == before
        assert db.execute('SELECT COUNT(*) FROM live_fact_repairs').fetchone()[0] == 0
        assert db.execute('SELECT state FROM live_review_attempts WHERE packet_ref=?', (packet['packet_ref'],)).fetchone()[0] == 'pending'
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_configured_review_advice_is_scoped_cached_and_cannot_grant_eligibility(tmp_path):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    calls = []
    async def helper(items, context):
        calls.append(items)
        return {'model': 'configured-test-model', 'decisions': [{'fact': item['fact'], 'verdict': 'insufficient', 'needs_context': True, 'reason': 'Controlled helper advice.', 'propositions': [item['text']], 'replacement_texts': []} for item in items]}
    svc.providers.gemini.assess_fact_candidates = helper
    packet, _ = await packet_items(adapter, session, run_id)
    assert packet['next_tool'] == 'assess_review_packet'
    args = {'packet_ref': packet['packet_ref']}
    reply = await adapter.execute_tool(session, {'name': 'assess_review_packet', 'args': args})
    replay = await adapter.execute_tool(session, {'name': 'assess_review_packet', 'args': args})
    assert replay == reply and len(calls) == 1 and reply['helper_available']
    assert all(f['eligibility'] == 'unreviewed' for f in adapter._get_facts(session.resource_id, {})['facts'])
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_packets WHERE result_json IS NOT NULL').fetchone()[0] == 0
    await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_review_helper_late_result_rejected_after_revision_changed(tmp_path):
    svc, adapter, session, run_id, reader, _ = await saved_candidates(tmp_path)
    async def helper(items, context):
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
        return {'model': 'configured-test-model', 'decisions': []}
    svc.providers.gemini.assess_fact_candidates = helper
    packet, _ = await packet_items(adapter, session, run_id)
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'name': 'assess_review_packet', 'args': {'packet_ref': packet['packet_ref']}})
    assert error.value.code == 'live_review_packet_stale'
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_assessments').fetchone()[0] == 0
    await reader.search_http.aclose()
