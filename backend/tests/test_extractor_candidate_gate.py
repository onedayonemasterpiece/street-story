"""Real-store candidate authority and bounded explicit Mira review controls."""
import copy
import json
from types import SimpleNamespace

import pytest

from street_story.research_budget import PAGE_UNITS, response_units
from street_story.service import ConflictError
from test_facts_research_finish import QUOTES, fallback, findings


@pytest.fixture(autouse=True)
def controlled_public_dns(monkeypatch):
    from street_story import article_media
    original = article_media.cached_public_page
    async def public(host):
        return '8.8.8.8'
    async def acquire(store, client, raw, **kwargs):
        kwargs.setdefault('resolver', public)
        return await original(store, client, raw, **kwargs)
    monkeypatch.setattr(article_media, 'cached_public_page', acquire)


async def call(adapter, session, name, args, command='candidate-test'):
    return await adapter.execute_tool(session, {'name': name, 'id': command, 'args': args})


def final_args(packet, decisions):
    return {'packet_ref': packet['packet_ref'], 'decisions': decisions,
            'relations_complete': True, 'conflicts': [], 'coverage_complete': False,
            'missing_aspects': ['Further source coverage may continue.']}


def positive(item):
    return {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
            'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
            'claims': [item['text']], 'basis_quotes': [item['text']],
            'reason': 'Explicit Mira decision based on this own literal passage.'}


def publication_state(svc, sid):
    with svc.store.connection() as db:
        story = dict(svc._story_row(db, sid))
        selected = dict(db.execute('SELECT assertion_id,owner_selected FROM fact_assertions WHERE story_id=?', (sid,)))
        research = json.loads(story['research_json'])
    return {'selected': {fid for fid, selected in selected.items() if selected},
            'draft': story['draft_text'], 'concept': research.get('publication_concept')}


def assertions(svc, sid):
    with svc.store.connection() as db:
        return {r['assertion_id']: dict(r) for r in db.execute('SELECT * FROM fact_assertions WHERE story_id=?', (sid,))}


async def prepared(tmp_path, *, inventory_reviewed=True, candidate_count=3):
    svc, adapter, session, _, run_id, helpers, reader = await fallback(tmp_path)
    first = await call(adapter, session, 'get_research_chunk', {'run_id': run_id})
    await call(adapter, session, 'save_research_facts', findings(first, QUOTES[:1], continuation=True), 'legacy-first')
    packet = await call(adapter, session, 'get_review_packet', {'run_id': run_id, 'allow_partial_review': True})
    await call(adapter, session, 'finalize_fact_review', final_args(packet, [positive(packet['items'][0])]), 'legacy-review')
    existing = next(iter(assertions(svc, session.resource_id)))
    adapter._select_facts(session.resource_id, 'owner-choice', {'fact_ids': [existing]})
    adapter._edit_text(session.resource_id, 'owner-draft', {'expected_text_revision': 0,
        'new_text': QUOTES[0], 'change_summary': 'Owner retained publication text.'})
    with svc.store.tx() as db:
        story = svc._story_row(db, session.resource_id)
        research = json.loads(story['research_json'])
        research['publication_concept'] = 'Owner retained concept'
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    before = publication_state(svc, session.resource_id)
    chunk = await call(adapter, session, 'get_research_chunk', {'run_id': run_id})
    args = findings(chunk, QUOTES[:candidate_count])
    if candidate_count > 3:
        args['facts'].append(copy.deepcopy(args['facts'][0]))
    args.update(extractor_candidates=True, inventory_reviewed=inventory_reviewed, batch_reviewed=True,
                source_matches_poi=True, source_content_valid=True)
    for fact in args['facts']:
        fact.update(existing_fact_id=existing, selected=True, verdict='supported', atomic=True,
                    support_complete=True, qualifiers_preserved=True, review_reason='Extractor advice is untrusted.')
    saved = await call(adapter, session, 'save_research_facts', args, 'extractor-batch')
    return svc, adapter, session, run_id, reader, helpers, existing, before, saved


@pytest.mark.asyncio
async def test_new_candidates_have_no_existing_id_selection_or_eligibility_authority(tmp_path):
    svc, adapter, session, run, reader, _, existing, before, saved = await prepared(tmp_path)
    try:
        pending = assertions(svc, session.resource_id)
        candidate_ids = {f['fact_id'] for f in saved['facts']}
        assert len(candidate_ids) == 3 and existing not in candidate_ids
        assert all(pending[fid]['eligibility'] == 'unreviewed' and not pending[fid]['owner_selected'] for fid in candidate_ids)
        assert pending[existing]['eligibility'] == 'eligible' and publication_state(svc, session.resource_id) == before
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        assert packet['total_facts'] == 3 and len(packet.get('nearby_existing_claims', [])) <= 8
        assert existing in {f['fact_id'] for f in packet['nearby_existing_claims']}
        assert response_units('get_review_packet', packet) <= PAGE_UNITS
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_explicit_mira_gate_accepts_first_good_candidate_and_withholds_duplicates_conflicts(tmp_path):
    svc, adapter, session, run, reader, helpers, existing, before, saved = await prepared(tmp_path)
    try:
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        decisions = [positive(item) for item in packet['items']]
        for item, decision in zip(packet['items'], decisions):
            if item['text'] == QUOTES[0]:
                decision['equivalent_to_existing'] = existing
            elif item['text'] == QUOTES[2]:
                decision['conflicts_with_existing'] = [existing]
        result = await call(adapter, session, 'finalize_fact_review', final_args(packet, decisions), 'mira-candidate-review')
        assert not result['coverage_complete'] and not result['complete']
        state = assertions(svc, session.resource_id)
        by_text = {f['text']: f['fact_id'] for f in saved['facts']}
        assert state[by_text[QUOTES[1]]]['eligibility'] == 'eligible'
        assert state[by_text[QUOTES[0]]]['eligibility'] == 'withheld'
        assert state[by_text[QUOTES[2]]]['eligibility'] == 'withheld'
        assert state[existing]['eligibility'] == 'eligible'
        assert publication_state(svc, session.resource_id) == before
        with svc.store.connection() as db:
            poi = dict(db.execute('SELECT assertion_id,eligibility FROM poi_research_assertions'))
        assert all(poi[fid] == state[fid]['eligibility'] for fid in by_text.values())
        assert not helpers
        exhausted = await call(adapter, session, 'get_review_packet', {'run_id': run})
        assert exhausted['pending_candidates'] == 0 and not exhausted['review_available']
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing_qualifiers', 'false_qualifiers', 'compound', 'borrowed_quote', 'foreign_relation'])
async def test_positive_candidate_requires_explicit_qualifiers_own_quote_and_scoped_relations(tmp_path, defect):
    svc, adapter, session, run, reader, _, existing, before, saved = await prepared(tmp_path)
    try:
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        item = next(item for item in packet['items'] if item['text'] == QUOTES[1])
        decision = positive(item)
        if defect == 'missing_qualifiers':
            decision.pop('qualifiers_preserved')
        elif defect == 'false_qualifiers':
            decision['qualifiers_preserved'] = False
        elif defect == 'compound':
            decision['claims'] = ['First proposition.', 'Second proposition.']
        elif defect == 'borrowed_quote':
            decision['basis_quotes'] = ['A fabricated quote absent from this evidence.']
        else:
            decision['equivalent_to_existing'] = 'foreign-unlisted-claim'
        with pytest.raises(ConflictError):
            await call(adapter, session, 'finalize_fact_review', final_args(packet, [decision]), 'invalid-positive')
        state = assertions(svc, session.resource_id)
        assert all(state[f['fact_id']]['eligibility'] == 'unreviewed' for f in saved['facts'])
        assert state[existing]['eligibility'] == 'eligible' and publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['foreign_actor', 'stale_story', 'stale_nearby'])
async def test_foreign_or_stale_packet_never_commits_candidate_decisions(tmp_path, defect):
    svc, adapter, session, run, reader, _, existing, before, saved = await prepared(tmp_path)
    try:
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        actor = session
        if defect == 'foreign_actor':
            actor = SimpleNamespace(resource_id=session.resource_id, state={}, actor={'sub': 'foreign-owner'})
        else:
            with svc.store.tx() as db:
                if defect == 'stale_story':
                    db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (session.resource_id,))
                else:
                    db.execute('UPDATE fact_assertions SET revision_digest=? WHERE story_id=? AND assertion_id=?',
                               ('changed-nearby-revision', session.resource_id, existing))
        with pytest.raises(ConflictError):
            await call(adapter, actor, 'finalize_fact_review', final_args(packet, [positive(item) for item in packet['items']]), 'stale-final')
        state = assertions(svc, session.resource_id)
        assert all(state[f['fact_id']]['eligibility'] == 'unreviewed' for f in saved['facts'])
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_extractor_candidate_mode_cannot_call_legacy_reconciler_to_overwrite_existing_id(tmp_path, monkeypatch):
    from street_story.live import StreetStoryLiveAdapter
    original = StreetStoryLiveAdapter._save_research_facts
    calls = []
    async def save(adapter, session, command, args):
        if args.get('extractor_candidates'):
            async def dangerous(incoming, existing):
                calls.append(True)
                return {'matches': {0: existing[0]['fact_id']}, 'complete': True, 'decisions': []}
            adapter.service.providers.gemini.reconcile_fact_identities = dangerous
        return await original(adapter, session, command, args)
    monkeypatch.setattr(StreetStoryLiveAdapter, '_save_research_facts', save)
    svc, _, session, _, reader, _, existing, before, saved = await prepared(tmp_path, inventory_reviewed=False)
    try:
        assert not calls
        assert existing not in {f['fact_id'] for f in saved['facts']}
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_nearby_claims_budget_keeps_whole_claims_and_one_evidence_slice(tmp_path):
    svc, adapter, session, run, reader, _, existing, before, _ = await prepared(tmp_path)
    try:
        with svc.store.tx() as db:
            originals = {table: dict(db.execute('SELECT * FROM '+table+' WHERE story_id=? LIMIT 1', (session.resource_id,)).fetchone())
                         for table in ('facts', 'fact_assertions')}
            for n in range(8):
                for table, template in originals.items():
                    value = dict(template)
                    for key in ('fact_id', 'assertion_id'):
                        if key in value:
                            value[key] = f'nearby-{n}'
                    if table == 'facts':
                        value['text'] = (f'Nearby readonly claim {n}. ' + 'Complete qualifying detail. '*20)[:500]
                        value['selected'] = 0
                    else:
                        value.update(eligibility='eligible', review_status='eligible', owner_selected=0)
                    db.execute('INSERT INTO '+table+'('+','.join(value)+') VALUES('+','.join('?' for _ in value)+')',tuple(value.values()))
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        assert packet['items'] and response_units('get_review_packet', packet) <= PAGE_UNITS
        assert len(packet.get('nearby_existing_claims', [])) <= 8
        with svc.store.connection() as db:
            for claim in packet.get('nearby_existing_claims', []):
                assert claim['text'] == db.execute('SELECT text FROM facts WHERE story_id=? AND fact_id=?',
                                                  (session.resource_id, claim['fact_id'])).fetchone()[0]
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_legacy_packet_ignores_caller_supplied_candidate_scope(tmp_path):
    svc, adapter, session, _, run, _, reader = await fallback(tmp_path)
    try:
        chunk = await call(adapter, session, 'get_research_chunk', {'run_id': run})
        await call(adapter, session, 'save_research_facts', findings(chunk, QUOTES), 'legacy-save-all')
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        args = final_args(packet, [positive(item) for item in packet['items']])
        args['_candidate_scope'] = []
        args.update(coverage_complete=True, missing_aspects=[])
        result = await call(adapter, session, 'finalize_fact_review', args, 'legacy-all-review')
        assert result['complete'] and result['eligible_count'] == 3
        assert all(f['eligibility'] == 'eligible' for f in assertions(svc, session.resource_id).values())
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_small_candidate_packets_advance_remaining_candidates_without_whole_inventory_review(tmp_path):
    svc, adapter, session, run, reader, _, existing, before, saved = await prepared(tmp_path, candidate_count=4)
    try:
        assert len(saved['facts']) == 4
        packet = await call(adapter, session, 'get_review_packet', {'run_id': run})
        assert packet['total_facts'] == 3
        decisions = [{'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'insufficient'}
                     for item in packet['items']]
        await call(adapter, session, 'finalize_fact_review', final_args(packet, decisions), 'first-small-review')
        remaining = await call(adapter, session, 'get_review_packet', {'run_id': run})
        assert remaining['total_facts'] == 1
        assert response_units('get_review_packet', remaining) <= PAGE_UNITS
        assert assertions(svc, session.resource_id)[existing]['eligibility'] == 'eligible'
        assert publication_state(svc, session.resource_id) == before
    finally:
        await reader.search_http.aclose()
