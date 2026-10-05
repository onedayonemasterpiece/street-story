"""Only checked model recommendations can create a new factual research scope."""
import json

import pytest

from street_story.headless_facts import HeadlessFacts
from street_story.errors import RetryableProviderError
from street_story.fact_ledger import set_owner_selection
from street_story.research_runs import begin_research_run
from test_headless_facts import controlled_public_dns as controlled_public_dns, fixture

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
            next_job = dict(db.execute('SELECT * FROM jobs WHERE id<>?', (job['id'],)).fetchone())
            payload = json.loads(next_job['payload_json'])
            assert payload == pending
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
    {'research_sufficient': True}, {'source_matches_poi': False}, {'source_content_valid': False},
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
            begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='Museum details', scope='museum details',
                               expected_story_revision=row['revision'], identity_generation=0, run_id='memory-run', now=svc.store.now())
        captured = []
        original = researcher.extract_fact_page

        async def inspect(page, story, context):
            captured.append(context)
            return await original(page, story, context)

        researcher.extract_fact_page = inspect
        # All remembered unread sources are now attached before new discovery;
        # one model page is committed and the existing cursor continues later.
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
            assert db.execute('SELECT COUNT(*) FROM facts WHERE story_id=?', (sid,)).fetchone()[0] == 71
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
