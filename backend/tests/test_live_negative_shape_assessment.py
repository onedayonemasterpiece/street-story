"""New guarded exact-text shape assessments, distinct from support growth."""
import pytest

from street_story.fact_ledger import accept_fact_basis, refresh_review_status, set_owner_selection
from street_story.source_assessment import VALUE_CONTRACT
from test_fact_ledger import fact, persist
from test_facts_research_finish import fallback, QUOTES


@pytest.mark.asyncio
@pytest.mark.parametrize('negative', ['compound', 'qualifier_loss', 'support_only', 'changed_text'])
async def test_exact_old_claim_shape_negative_and_support_only_continuity(tmp_path, negative):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    try:
        session.state.update(live_first_research=True, semantic_reading_contract=VALUE_CONTRACT)
        chunk = await adapter.execute_tool(session, {'name':'get_research_chunk','args':{'run_id':run_id}})
        assert chunk.get('evidence_passages'), chunk.get('full_source_attempts')
        text = QUOTES[0] + ' ' + QUOTES[1]
        claim = fact('old-compound', text, 'https://old.example/own', text)
        fid = persist(svc.store, session.resource_id, [claim], 'old-run', 'old-batch')[0]
        with svc.store.tx() as db:
            ids = [r[0] for r in db.execute('SELECT evidence_id FROM fact_evidence_spans')]
            accept_fact_basis(db, session.resource_id, fid, ids, operation={'receipt_id':'old-assessment'}, now=svc.store.now())
            refresh_review_status(db, session.resource_id, svc.store.now())
            set_owner_selection(db, session.resource_id, [fid], svc.store.now())
            basis = db.execute('SELECT accepted_basis_json FROM fact_assertions WHERE assertion_id=?',(fid,)).fetchone()[0]
        observed = QUOTES[0] if negative == 'changed_text' else text
        await adapter.execute_tool(session, {'name':'save_research_facts','id':'new-guarded-negative','args':{
            'facts':[{'text':observed,'existing_fact_id':fid,'passage_ids':[0],
                'verdict':'insufficient','atomic':negative not in {'compound','changed_text'},
                'support_complete':False,'qualifiers_preserved':negative != 'qualifier_loss',
                'review_reason':'Controlled new exact-claim model assessment; not a pending recheck.',
                'confidence':.9,'selected':False}],
            'batch_reviewed':True,'inventory_reviewed':True,'source_matches_poi':True,'source_content_valid':True}})
        with svc.store.connection() as db:
            row = db.execute('SELECT * FROM fact_assertions WHERE assertion_id=?',(fid,)).fetchone()
            assert row['display_text'] == text
            if negative in {'compound','qualifier_loss'}:
                assert row['eligibility'] == 'withheld' and row['review_status'] == 'withheld'
                assert row['accepted_basis_json'] is None
                assert row['owner_selected'] == 1
            else:
                assert row['eligibility'] == 'eligible' and row['accepted_basis_json'] == basis
                assert row['owner_selected'] == 1
        if negative in {'compound', 'qualifier_loss'}:
            # Refresh/reopen alone cannot undo the new negative. A separately
            # guarded positive assessment must supply its own current basis.
            with svc.store.tx() as db:
                refresh_review_status(db, session.resource_id, svc.store.now())
                assert db.execute('SELECT eligibility FROM fact_assertions WHERE assertion_id=?', (fid,)).fetchone()[0] == 'withheld'
            packet = await adapter.execute_tool(session, {'name':'get_review_packet', 'args':{'run_id':run_id, 'fact_ids':[fid]}})
            await adapter.execute_tool(session, {'name':'finalize_fact_review', 'id':'new-positive-assessment', 'args':{
                'packet_ref':packet['packet_ref'], 'decisions':[{'fact':0, 'evidence':[0], 'verdict':'supported',
                    'atomic':True, 'support_complete':True, 'qualifiers_preserved':True, 'claims':[text],
                    'basis_quotes':[text]}],
                'relations_complete':True, 'conflicts':[], 'coverage_complete':False, 'missing_aspects':[]}})
            with svc.store.connection() as db:
                row = db.execute('SELECT * FROM fact_assertions WHERE assertion_id=?', (fid,)).fetchone()
                assert row['display_text'] == text and row['eligibility'] == 'eligible' and row['owner_selected'] == 1
                assert row['accepted_basis_json'] and row['accepted_basis_json'] != basis
    finally:
        await reader.search_http.aclose()
