"""Only checked model recommendations can create a new factual research scope."""
import json
from types import SimpleNamespace

import pytest
from street_story.errors import RetryableProviderError
from street_story.fact_conflicts import record_fact_review_scan
from street_story.fact_ledger import refresh_review_status, set_owner_selection
from street_story.headless_facts import HeadlessFacts
from street_story.poi_memory import sync_poi_review_from_story
from street_story.research_runs import begin_research_run
from test_headless_facts import controlled_public_dns as controlled_public_dns
from test_headless_facts import fixture, review_candidates

QUERY = 'Gate municipal archive museum opening chronology'
GOAL = 'Find the missing documented museum chronology'
PLAN = {'research_sufficient': False, 'next_research_query': QUERY, 'next_research_goal': GOAL,
        'source_matches_poi': True, 'source_content_valid': True}


@pytest.mark.asyncio
async def test_model_owned_continuation_uses_existing_joined_job_and_exact_query(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    original = researcher.extract_fact_page

    async def insufficient(*args):
        result = await original(*args)
        result['result'].update(PLAN)
        return result

    researcher.extract_fact_page = insufficient
    try:
        with svc.store.tx() as db:
            row = svc._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?', ('Owner draft', json.dumps(research), row['id']))
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        await review_candidates(svc, job['story_id'], 'headless-run')
        with svc.store.tx() as db:
            row = svc._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            pending = research['pending_fact_request']
            assert pending['research_query'] == QUERY and pending['coverage_goal'] == GOAL
            assert pending['extraction_scope'].startswith('model:')
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
            assert row['draft_text'] == 'Owner draft' and research['publication_concept'] == 'Owner concept'
            # This is the existing terminal worker hook, not a new scheduler.
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job['id'],))
            assert svc._resume_joined_fact_request(db, job['story_id'])
            next_id = db.execute('SELECT id FROM jobs WHERE id<>?', (job['id'],)).fetchone()[0]
            db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (next_id,))
            next_job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (next_id,)).fetchone())
            payload = json.loads(next_job['payload_json'])
            assert {key: payload[key] for key in pending} == pending
            budget = research['research_budget']
            assert payload['research_started_at'] == budget['started_at']
            assert payload['research_deadline_at'] == budget['deadline_at']
            begin_research_run(db, story_id=job['story_id'], poi_key='wiki:77', goal=GOAL, scope=payload['extraction_scope'],
                               expected_story_revision=row['revision'], identity_generation=0, run_id='next-scope', now=svc.store.now())
        queries = []
        original_search = researcher.search_articles

        async def searched(query, story):
            queries.append(query)
            if len(queries) == 1:
                raise RetryableProviderError('controlled_search_wait', retry_at=svc.store.now()+60)
            return await original_search(query, story)

        researcher.search_articles = searched
        # Extraction produces candidates; discovery continues after separate Live review.
        await HeadlessFacts(svc).run(next_job, 'next-scope', GOAL, payload['extraction_scope'])
        assert queries == []
        await review_candidates(svc, job['story_id'], 'next-scope')
        with pytest.raises(RetryableProviderError, match='controlled_search_wait'):
            await HeadlessFacts(svc).run(next_job, 'next-scope', GOAL, payload['extraction_scope'])
        pages_before_retry = len(researcher.pages)
        await HeadlessFacts(svc).run(next_job, 'next-scope', GOAL, payload['extraction_scope'])
        assert len(researcher.pages) == pages_before_retry  # No repeated model extraction after discovery failure.
        assert queries == [QUERY, QUERY]  # Exact accepted recipe survives retry.
        with svc.store.connection() as db:
            research = json.loads(svc._story_row(db, job['story_id'])['research_json'])
            assert len(research['fact_research_continuations']) == 1
            assert 'pending_fact_request' not in research  # Identical model recommendation cannot loop.
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'research_sufficient': True}, {'source_matches_poi': False}, {'source_content_valid': None},
    {'next_research_goal': ''}, {'next_research_query': ''}, {'next_research_query': 42},
    {'next_research_query': 'Бранденбургские ворота Find historical facts', 'next_research_goal': 'Find historical facts'},
])
async def test_unverified_sufficient_or_identical_recipe_cannot_schedule_next_scope(tmp_path, changes):
    svc, job, _, reader, _ = await fixture(tmp_path)
    try:
        helper = HeadlessFacts(svc)
        assert not helper._queue_model_continuation(job, 'headless-run', 'Find historical facts', 'history', {**PLAN, **changes}, 0)
        with svc.store.connection() as db:
            research = json.loads(svc._story_row(db, job['story_id'])['research_json'])
            assert 'pending_fact_request' not in research and 'fact_research_continuations' not in research
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [False, True])
async def test_rejected_gallery_hands_off_saved_query_without_repeating_extraction(tmp_path, monkeypatch, recover):
    from street_story.research_runs import run_manifest, manifest_complete
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    helper = HeadlessFacts(svc)
    original = researcher.extract_fact_page

    async def rejected(*args):
        response = await original(*args)
        response['result'].update(PLAN, source_content_valid=False, continuation_needed=True)
        return response

    researcher.extract_fact_page = rejected
    try:
        if recover:
            # Reproduce a deployed committed response whose continuation was
            # previously stranded. Recovery must consume it without inference.
            handoff = helper._handoff_rejected_source
            monkeypatch.setattr(helper, '_handoff_rejected_source', lambda *args: False)
            with pytest.raises(RetryableProviderError):
                await helper.run(job, 'headless-run', 'Find historical facts', 'history')
            monkeypatch.setattr(helper, '_handoff_rejected_source', handoff)
        await helper.run(job, 'headless-run', 'Find historical facts', 'history')
        assert len(researcher.pages) == len(researcher.model_units) == 1
        assert not svc.story(job['story_id'])['facts']
        with svc.store.tx() as db:
            manifest = run_manifest(db, 'headless-run')
            assert manifest['run']['state'] == 'partial'
            assert manifest['run']['status_detail'] == 'research_rejected_source_replaced'
            assert not manifest_complete(manifest)
            assert manifest['sources'][0]['error_code'] == 'not_article_text'
            db.execute("UPDATE jobs SET state='done' WHERE id=?", (job['id'],))
            assert svc._resume_joined_fact_request(db, job['story_id'])
            next_job = dict(db.execute('SELECT * FROM jobs WHERE id<>?', (job['id'],)).fetchone())
            db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (next_job['id'],))
            next_job['attempts'] = 1
            row = svc._story_row(db, job['story_id'])
            payload = json.loads(next_job['payload_json'])
            begin_research_run(db, story_id=job['story_id'], poi_key='wiki:77', goal=GOAL,
                scope=payload['extraction_scope'], expected_story_revision=row['revision'],
                identity_generation=0, run_id='replacement-run', now=svc.store.now())
        queries = []
        original_search = researcher.search_articles

        async def search(query, story):
            queries.append(query)
            return await original_search(query, story)

        researcher.search_articles = search
        researcher.extract_fact_page = original
        await helper.run(next_job, 'replacement-run', GOAL, payload['extraction_scope'])
        assert queries == [QUERY]  # The accepted gap query precedes old gallery reuse.
        assert svc.story(job['story_id'])['facts'][0]['eligibility'] == 'unreviewed'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('block', ['unknown_send', 'owner_edit'])
async def test_rejected_gallery_handoff_preserves_unknown_operations_and_owner_fence(tmp_path, monkeypatch, block):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    helper = HeadlessFacts(svc)
    original = researcher.extract_fact_page

    async def rejected(*args):
        response = await original(*args)
        response['result'].update(PLAN, source_content_valid=False, continuation_needed=True)
        return response

    researcher.extract_fact_page = rejected
    try:
        handoff = helper._handoff_rejected_source
        monkeypatch.setattr(helper, '_handoff_rejected_source', lambda *args: False)
        with pytest.raises(RetryableProviderError):
            await helper.run(job, 'headless-run', 'Find historical facts', 'history')
        monkeypatch.setattr(helper, '_handoff_rejected_source', handoff)
        if block == 'unknown_send':
            svc.store.checkpoint_put(job['id'], 'headless_fact_unit:later-unit', {'phase': 'unknown'})
        else:
            with svc.store.tx() as db:
                db.execute('UPDATE stories SET draft_text=? WHERE id=?', ('New owner draft', job['story_id']))
        assert not helper._handoff_rejected_source(job, 'headless-run', 'Find historical facts', 'history', 0)
        assert len(researcher.model_units) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_model_continuation_never_displaces_existing_owner_request(tmp_path):
    svc, job, _, reader, _ = await fixture(tmp_path)
    try:
        owner = {'input_revision': 'owner-request', 'coverage_goal': 'Owner requested architecture'}
        with svc.store.tx() as db:
            row = svc._story_row(db, job['story_id'])
            research = json.loads(row['research_json'])
            research['pending_fact_request'] = owner
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
        helper = HeadlessFacts(svc)
        assert helper._queue_model_continuation(job, 'headless-run', 'Find historical facts', 'history', PLAN, 0)
        assert not helper._queue_model_continuation(job, 'headless-run', 'Find historical facts', 'history', PLAN, 0)
        with svc.store.connection() as db:
            pending = json.loads(svc._story_row(db, job['story_id'])['research_json'])['pending_fact_request']
            assert pending['input_revision'] == 'owner-request'
            assert len(pending['queued_requests']) == 1
            assert pending['queued_requests'][0]['research_query'] == QUERY
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_full_poi_memory_exceeds_model_window_without_losing_ledger_or_selection(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    sid = job['story_id']
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        await review_candidates(svc, sid, 'headless-run')
        with svc.store.tx() as db:
            seed = db.execute('SELECT fact_id FROM facts WHERE story_id=?', (sid,)).fetchone()[0]
            set_owner_selection(db, sid, [seed], svc.store.now())
            row = svc._story_row(db, sid)
            research = json.loads(row['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?', ('Owner draft', json.dumps(research), sid))
            for i in range(70):
                url = f'https://archive.example/memory-{i}'
                sources = json.dumps([{'url': url, 'title': 'Memory', 'supports': [{'text': f'Documented memory claim {i}.'}]}])
                db.execute("INSERT INTO poi_research_assertions(poi_key,assertion_id,semantic_key,text,confidence,sources_json,"
                           "review_status,eligibility,created_at,updated_at) VALUES('wiki:77',?,?,?,.95,?,'eligible','eligible',0,0)",
                           (f'memory-{i}', f'claim-{i}', f'Documented memory claim {i}.', sources))
            for i in range(95):
                db.execute("INSERT INTO poi_research_sources(poi_key,url,title,supports_json,last_query,first_seen_at,last_seen_at) "
                           "VALUES('wiki:77',?,'Memory','[]','Previous query',0,0)", (f'https://archive.example/source-{i}',))
            svc._hydrate_poi_memory(db, svc._story_row(db, sid))
            # Construct the saved reviewed-memory fixture through the regular ledger.
            # Exact revisions and literal own evidence are required, not eligibility flags.
            reviewed = list(db.execute('SELECT a.assertion_id,a.revision_digest,f.text,f.sources_json '
                                       'FROM fact_assertions a JOIN facts f ON f.story_id=a.story_id '
                                       'AND f.fact_id=a.assertion_id WHERE a.story_id=?', (sid,)))
            assert len(reviewed) == 71
            for fact in reviewed:
                assert any(fact['text'] in support.get('text', '')
                           for source in json.loads(fact['sources_json'])
                           for support in source.get('supports', []))
            record_fact_review_scan(svc, sid, 'wiki:77', detector='controlled_saved_memory_fixture',
                                    run_id='memory-fixture-review',
                                    revision_bundle={f['assertion_id']: f['revision_digest'] for f in reviewed},
                                    conflict_ids=[], coverage_complete=True, missing_aspects=[], connection=db)
            refresh_review_status(db, sid, svc.store.now())
            sync_poi_review_from_story(db, sid, svc.store.now())
            row = svc._story_row(db, sid)
            begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='Museum details', scope='museum details',
                               expected_story_revision=row['revision'], identity_generation=0, run_id='memory-run', now=svc.store.now())
        captured = []
        original = researcher.extract_fact_page

        async def inspect(page, story, context):
            captured.append(context)
            return await original(page, story, context)

        researcher.extract_fact_page = inspect
        # All remembered unread sources are now attached before new discovery;
        # three independent model pages become candidates; the cursor continues later.
        with pytest.raises(RetryableProviderError, match='research_fact_next_page'):
            await HeadlessFacts(svc).run(job, 'memory-run', 'Museum details', 'museum details')
        context = captured[0]
        assert len(context['known_facts']) == 50
        assert context['known_inventory_total'] == 71 and context['known_inventory_omitted_count'] == 21
        assert len(context['_known_fact_inventory']) == 71
        assert context['known_inventory_complete'] is False and context['known_inventory_next_cursor'] is not None
        assert context['prior_poi_ledger_omitted_count'] == 11
        assert context['previously_processed_sources_omitted_count'] >= 15
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM facts WHERE story_id=?', (sid,)).fetchone()[0] == 74
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='unreviewed'", (sid,)).fetchone()[0] == 3
        # Review only the three new candidates; do not close the unread research scope.
        adapter = HeadlessFacts(svc).adapter
        session = SimpleNamespace(id='live_1234567890abcdef', resource_id=sid,
                                  model='gemini-3.8-live', state={})
        page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': {'run_id': 'memory-run'}})
        rows, nearby = [], page['nearby_existing_claims']
        while True:
            rows.extend(page['items'])
            if not page['has_more']:
                break
            page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': page['next_args']})
        assert any(claim['fact_id'] == seed for claim in nearby)
        decisions = []
        for item in rows:
            assert item['text'] in item['passage']
            assert item['text'] == next(claim['text'] for claim in nearby if claim['fact_id'] == seed)
            decisions.append({'fact': item['fact'], 'verdict': 'supported', 'evidence': [item['evidence']],
                              'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                              'claims': [item['text']], 'basis_quotes': [item['text']],
                              'equivalent_to_existing': seed, 'reason': 'Same literal proposition as the checked owner fact.'})
        assert len(decisions) == 3
        reviewed = await adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'review-memory-duplicates', 'args': {
            'packet_ref': page['packet_ref'], 'decisions': decisions, 'relations_complete': True,
            'coverage_complete': False, 'missing_aspects': [], 'conflicts': []}})
        assert reviewed['complete'] is False and reviewed['unreviewed_count'] == 0
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'", (sid,)).fetchone()[0] == 71
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='withheld'", (sid,)).fetchone()[0] == 3
            assert db.execute('SELECT owner_selected FROM fact_assertions WHERE story_id=? AND assertion_id=?', (sid, seed)).fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND owner_selected=1', (sid,)).fetchone()[0] == 1
            row = svc._story_row(db, sid)
            assert row['draft_text'] == 'Owner draft'
            assert json.loads(row['research_json'])['publication_concept'] == 'Owner concept'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_grounding_source_persistence_has_no_eighty_source_ceiling(tmp_path):
    svc, job, _, reader, _ = await fixture(tmp_path)
    try:
        sources = [{'url': f'https://archive.example/article-{i}', 'title': f'Archive {i}'} for i in range(95)]
        assert HeadlessFacts(svc)._bind_discovery(job, 'headless-run', 'History', 'history', sources, {}, 0)
        with svc.store.connection() as db:
            research = json.loads(svc._story_row(db, job['story_id'])['research_json'])
            assert len(research['grounding_sources']) == 95
            assert db.execute("SELECT COUNT(*) FROM research_run_sources WHERE run_id='headless-run'").fetchone()[0] == 95
    finally:
        await reader.search_http.aclose()
