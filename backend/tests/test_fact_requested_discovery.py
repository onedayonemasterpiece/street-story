"""A requested new query survives complete cached scope reuse and route waits."""
import json

import httpx
import pytest
from street_story.errors import RetryableProviderError
from street_story.fact_ledger import set_owner_selection
from street_story.headless_facts import HeadlessFacts
from street_story.research_runs import begin_research_run, run_manifest
from test_headless_facts import URL, fixture, review_candidates
from test_headless_facts import controlled_public_dns as controlled_public_dns

QUERY = 'Gate archive accession newly digitized exhibit records'
NEW_URL = 'https://archive.example/new-exhibit-records'
NEW_CLAIM = 'The gate museum acquired its documented archive in 2008.'


async def requested_cached_scope(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
    sid = job['story_id']
    await review_candidates(svc, sid, 'headless-run')
    with svc.store.tx() as db:
        seed = db.execute('SELECT fact_id FROM facts WHERE story_id=?', (sid,)).fetchone()[0]
        set_owner_selection(db, sid, [seed], svc.store.now())
        row = svc._story_row(db, sid)
        research = json.loads(row['research_json'])
        research['publication_concept'] = 'Owner concept'
        db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?',
                   ('Owner draft', json.dumps(research), sid))
        begin_research_run(db, story_id=sid, poi_key='wiki:77', goal='Find missing archive details', scope='history',
                           expected_story_revision=row['revision'], identity_generation=0,
                           run_id='requested-run', now=svc.store.now())
    payload = json.loads(job['payload_json'])
    job['payload_json'] = json.dumps({**payload, 'research_query': QUERY})
    return svc, job, researcher, reader, fetches, seed


@pytest.mark.asyncio
async def test_requested_query_after_complete_scope_reuse_finds_new_eligible_fact(tmp_path):
    svc, job, researcher, reader, fetches, seed = await requested_cached_scope(tmp_path)
    original_extract = researcher.extract_fact_page
    queries = []
    await reader.search_http.aclose()

    async def transport(request):
        fetches.append(str(request.url))
        assert request.url.path == '/new-exhibit-records'
        return httpx.Response(200, headers={'content-type': 'text/html'},
                              text='<main><p>' + NEW_CLAIM + ' Public archive accession record.</p></main>')

    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    async def discover(query, story):
        queries.append(query)
        assert len(researcher.pages) == 1  # Completed cached extraction was skipped.
        assert len(fetches) == 1  # Cached bytes were reused before fresh discovery.
        return {'sources': [{'url': NEW_URL, 'title': 'New accession record'}],
                'receipt': {'backend': 'controlled-search'}}

    async def extract(page, story, context):
        result = await original_extract(page, story, context)
        assert page['source_url'] == NEW_URL
        result['result']['facts'] = [{
            'claim_key': 'museum-archive-acquisition', 'existing_fact_id': '', 'text': NEW_CLAIM,
            'confidence': .95, 'passage_ids': [page['evidence_passages'][0]['passage_id']],
            'verdict': 'supported', 'atomic': True, 'support_complete': True,
            'qualifiers_preserved': True, 'review_reason': 'Literal archive date in the supplied public source.',
        }]
        return result

    researcher.search_articles = discover
    researcher.extract_fact_page = extract
    try:
        with pytest.raises(RetryableProviderError, match='research_fact_next_page'):
            await HeadlessFacts(svc).run(job, 'requested-run', 'Find missing archive details', 'history')
        await HeadlessFacts(svc).run(job, 'requested-run', 'Find missing archive details', 'history')
        assert queries == [QUERY] and len(fetches) == 2
        assert len(researcher.pages) == 2
        with svc.store.connection() as db:
            assert run_manifest(db, 'requested-run')['run']['state'] == 'verifying'
            assert db.execute("SELECT a.eligibility FROM fact_assertions a JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id WHERE a.story_id=? AND f.text=?", (job['story_id'], NEW_CLAIM)).fetchone()[0] == 'unreviewed'
            assert db.execute("SELECT eligibility FROM poi_research_assertions WHERE text=?", (NEW_CLAIM,)).fetchone()[0] == 'unreviewed'
        await review_candidates(svc, job['story_id'], 'requested-run')
        with svc.store.connection() as db:
            manifest = run_manifest(db, 'requested-run')
            assert manifest['run']['state'] == 'completed'
            assert manifest['counts']['chunks_skipped_completed'] == 1
            assert {source['url'] for source in manifest['sources']} == {URL, NEW_URL}
            new = db.execute('SELECT f.fact_id,a.eligibility FROM facts f JOIN fact_assertions a '
                             'ON a.story_id=f.story_id AND a.assertion_id=f.fact_id '
                             'WHERE f.story_id=? AND f.text=?', (job['story_id'], NEW_CLAIM)).fetchone()
            assert new and new['eligibility'] == 'eligible'
            assert db.execute('SELECT eligibility FROM poi_research_assertions WHERE assertion_id=?',
                              (new['fact_id'],)).fetchone()[0] == 'eligible'
            assert db.execute('SELECT owner_selected FROM fact_assertions WHERE assertion_id=?', (seed,)).fetchone()[0] == 1
            row = svc._story_row(db, job['story_id'])
            assert row['draft_text'] == 'Owner draft'
            assert json.loads(row['research_json'])['publication_concept'] == 'Owner concept'
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_requested_discovery_wait_resumes_without_reextracting_completed_scope(tmp_path):
    svc, job, researcher, reader, fetches, _ = await requested_cached_scope(tmp_path)
    queries = []

    async def search(query, story):
        queries.append(query)
        if len(queries) == 1:
            raise RetryableProviderError('controlled_route_wait', retry_at=svc.store.now()+30)
        return {'sources': [{'url': URL, 'title': 'Already checked source'}],
                'receipt': {'backend': 'controlled-search'}}

    researcher.search_articles = search
    try:
        with pytest.raises(RetryableProviderError, match='controlled_route_wait'):
            await HeadlessFacts(svc).run(job, 'requested-run', 'Find missing archive details', 'history')
        with svc.store.connection() as db:
            manifest = run_manifest(db, 'requested-run')
            assert manifest['run']['state'] == 'partial'
            assert manifest['run']['status_detail'] == 'research_fact_discovery_pending'
            assert manifest['counts']['chunks_skipped_completed'] == 1
        await HeadlessFacts(svc).run(job, 'requested-run', 'Find missing archive details', 'history')
        await HeadlessFacts(svc).run(job, 'requested-run', 'Find missing archive details', 'history')
        assert queries == [QUERY, QUERY]
        assert len(fetches) == len(researcher.pages) == 1
        with svc.store.connection() as db:
            assert run_manifest(db, 'requested-run')['run']['state'] == 'completed'
            research = json.loads(svc._story_row(db, job['story_id'])['research_json'])
            entry = next(item for item in research['live_web_searches'] if item['research_run_id'] == 'requested-run')
            assert entry['search_provider'] == 'controlled-search'
    finally:
        await reader.search_http.aclose()
