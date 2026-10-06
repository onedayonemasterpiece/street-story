"""Discovery retains every source; page budgets do not cap cumulative coverage."""
import json

import httpx
import pytest
from street_story.errors import RetryableProviderError
from street_story.fact_ledger import set_owner_selection
from street_story.headless_facts import HeadlessFacts
from street_story.research_runs import begin_research_run, run_manifest
from test_headless_facts import URL, fixture, review_candidates
from test_headless_facts import controlled_public_dns as controlled_public_dns


@pytest.mark.asyncio
async def test_more_than_three_sources_accumulate_and_completed_scope_reuses_history(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    sid = job['story_id']
    sources = [URL, *[f'https://archive.example/gate-history-{i}' for i in range(1, 5)]]
    original_extract = researcher.extract_fact_page
    try:
        # Preserve an existing owner-selected fact and publication before more research.
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        await review_candidates(svc, sid, 'headless-run')
        with svc.store.tx() as db:
            seed = db.execute('SELECT fact_id FROM facts WHERE story_id=?', (sid,)).fetchone()[0]
            set_owner_selection(db, sid, [seed], svc.store.now())
            story = svc._story_row(db, sid)
            research = json.loads(story['research_json'])
            research['publication_concept'] = 'Owner concept'
            db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?', ('Owner draft', json.dumps(research), sid))
        await reader.search_http.aclose()

        async def transport(request):
            url = str(request.url)
            fetches.append(url)
            index = next(i for i, source in enumerate(sources) if httpx.URL(source).path == request.url.path)
            text = f'The gate houses documented exhibit number {index}. This public article records the history of that exhibit.'
            return httpx.Response(200, headers={'content-type': 'text/html'}, text='<main><p>' + text + '</p></main>')

        reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(transport))

        async def search(query, story):
            researcher.searches += 1
            return {'sources': [{'url': url, 'title': f'Gate archive {i}'} for i, url in enumerate(sources)],
                    'receipt': {'backend': 'controlled-search'}}

        async def extract(page, story, context):
            result = await original_extract(page, story, context)
            index = sources.index(page['source_url'])
            if index:
                result['result']['facts'] = [{
                    'claim_key': f'exhibit-{index}', 'existing_fact_id': '',
                    'text': f'The gate houses documented exhibit number {index}.',
                    'confidence': .95, 'passage_ids': [page['evidence_passages'][0]['passage_id']],
                    'source_refs': [], 'evidence_refs': [], 'selected': False,
                    'verdict': 'supported', 'atomic': True, 'support_complete': True,
                    'qualifiers_preserved': True, 'review_reason': 'Literal statement in the supplied source passage.',
                }]
            return result

        researcher.search_articles = search
        researcher.extract_fact_page = extract
        # MORE starts from the full saved article inventory, including unread
        # URLs and a completed unchanged scope, with no mandatory rediscovery.
        with svc.store.tx() as db:
            for url in sources[1:]:
                db.execute('INSERT INTO poi_research_sources(poi_key,url,title,supports_json,last_query,first_seen_at,last_seen_at) '
                           'VALUES(?,?,?,?,?,?,?)', ('wiki:77', url, 'Saved gate archive', '[]', 'prior query', 0, 0))

        async def finish(run_id, goal, scope):
            with svc.store.tx() as db:
                story = svc._story_row(db, sid)
                begin_research_run(db, story_id=sid, poi_key='wiki:77', goal=goal, scope=scope,
                                   expected_story_revision=story['revision'], identity_generation=0,
                                   run_id=run_id, now=svc.store.now())
            for attempt in range(8):
                before = len(researcher.pages)
                try:
                    await HeadlessFacts(svc).run(job, run_id, goal, scope)
                except RetryableProviderError:
                    pass
                assert len(researcher.pages) - before <= 3
                with svc.store.connection() as db:
                    manifest = run_manifest(db, run_id)
                    stored = [r['url'] for r in db.execute('SELECT url FROM research_run_sources WHERE run_id=?', (run_id,))]
                    assert set(stored) == set(sources)  # All URLs are durable after the first discovery.
                    assert manifest['run']['extraction_scope'] == scope
                    ready = manifest['run']['state'] in {'completed', 'verifying'}
                if ready:
                    await review_candidates(svc, sid, run_id)
                    with svc.store.connection() as db:
                        return attempt + 1, run_manifest(db, run_id)
            pytest.fail('Unread discovered sources never completed')

        attempts, manifest = await finish('missing-history-run', 'Fill missing exhibit history aspects', 'history')
        assert attempts >= 2 and manifest['counts']['chunks_skipped_completed'] == 1
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'", (sid,)).fetchone()[0] == 5
            assert db.execute('SELECT owner_selected FROM fact_assertions WHERE story_id=? AND assertion_id=?', (sid, seed)).fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND owner_selected=1', (sid,)).fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0] == 5
            current = svc._story_row(db, sid)
            assert current['draft_text'] == 'Owner draft'
            assert json.loads(current['research_json'])['publication_concept'] == 'Owner concept'
        before = len(researcher.model_units), len(fetches)
        _, manifest = await finish('reused-history-run', 'Continue the completed historical aspect', 'history')
        assert manifest['counts']['chunks_skipped_completed'] == 5
        assert (len(researcher.model_units), len(fetches)) == before
        # A new requested aspect reuses article bytes, and keeps its own extraction scope.
        await finish('museum-scope-run', 'Fill missing museum chronology aspects', 'museum chronology')
        assert len(researcher.model_units) == before[0] + 5
        assert len(fetches) == before[1]
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'", (sid,)).fetchone()[0] == 5
            assert db.execute('SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND owner_selected=1', (sid,)).fetchone()[0] == 1
            current = svc._story_row(db, sid)
            assert current['draft_text'] == 'Owner draft'
            assert json.loads(current['research_json'])['publication_concept'] == 'Owner concept'
    finally:
        await reader.search_http.aclose()
