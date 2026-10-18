import json

import pytest

from street_story.fact_ledger import accept_fact_basis, accepted_basis_valid, refresh_review_status, set_owner_selection
from street_story.poi_memory import persist_research_memory, sync_poi_review_from_story
from test_fact_ledger import fact, persist
from test_facts_research_finish import fallback, QUOTES


@pytest.mark.asyncio
async def test_explicit_model_equivalence_preserves_admission_selection_and_observation(tmp_path):
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    try:
        session.state['live_first_research'] = True
        original_text = QUOTES[0]
        incoming_text = 'Переформулированное моделью эквивалентное утверждение.'
        claim = fact('original', original_text, 'https://old.example/own', original_text)
        fid = persist(svc.store, session.resource_id, [claim], 'original-run', 'original-batch')[0]
        with svc.store.tx() as db:
            ids = [r[0] for r in db.execute('SELECT evidence_id FROM fact_evidence_spans')]
            accept_fact_basis(db, session.resource_id, fid, ids, operation={'receipt_id': 'original-assessment'}, now=svc.store.now())
            refresh_review_status(db, session.resource_id, svc.store.now())
            set_owner_selection(db, session.resource_id, [fid], svc.store.now())
            original = db.execute('SELECT accepted_basis_json FROM fact_assertions WHERE story_id=? AND assertion_id=?', (session.resource_id, fid)).fetchone()[0]
            identity = json.loads(svc._story_row(db, session.resource_id)['research_json'])['visual_identity']
            persist_research_memory(db, identity, [{**claim, 'fact_id': fid}], claim['sources'], 'own', svc.store.now())
            sync_poi_review_from_story(db, session.resource_id, svc.store.now())
            proof = db.execute('SELECT review_proof_json FROM poi_research_assertions WHERE assertion_id=?', (fid,)).fetchone()[0]
        chunk = await adapter.execute_tool(session, {'name': 'get_research_chunk', 'args': {'run_id': run_id}})
        await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'equivalent-support', 'args': {
            'run_id': run_id, 'batch_reviewed': True, 'inventory_reviewed': True,
            'facts': [{'text': incoming_text, 'existing_fact_id': fid, 'passage_ids': [0],
                'verdict': 'supported', 'atomic': True, 'support_complete': True,
                'qualifiers_preserved': True, 'review_reason': 'Model explicitly declares the same claim.',
                'confidence': .95, 'selected': False}]}})
        with svc.store.connection() as db:
            assertion = db.execute('SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id=?', (session.resource_id, fid)).fetchone()
            assert assertion['display_text'] == original_text
            assert assertion['accepted_basis_json'] == original and accepted_basis_valid(db, session.resource_id, assertion)
            assert assertion['eligibility'] == 'eligible' and assertion['owner_selected'] == 1
            assert db.execute('SELECT review_proof_json FROM poi_research_assertions WHERE assertion_id=?', (fid,)).fetchone()[0] == proof
            assert db.execute('SELECT 1 FROM fact_observations WHERE story_id=? AND assertion_id=? AND text=?', (session.resource_id, fid, incoming_text)).fetchone()
            assert db.execute('SELECT COUNT(*) FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.assertion_id=?', (session.resource_id, fid)).fetchone()[0] >= 2
        assert chunk['source_url']
    finally:
        await reader.search_http.aclose()
