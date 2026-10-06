"""Unfinished source coverage survives four checked sources and empty discovery."""
import json

import httpx
import pytest

from street_story.providers import GeminiClient
from street_story.research_runs import begin_research_run, persist_source_version, register_discovered_source, run_manifest, set_run_state
from street_story.service import ConflictError
from test_live_editor import make_service, mark_identity_ready
from test_source_purpose_reuse import finish_text


@pytest.fixture
def public_dns(monkeypatch):
    from street_story import article_media
    original = article_media.cached_public_page
    async def resolve(host):
        return '8.8.8.8'
    async def acquire(store, client, raw, **kwargs):
        kwargs.setdefault('resolver', resolve)
        return await original(store, client, raw, **kwargs)
    monkeypatch.setattr(article_media, 'cached_public_page', acquire)


def scoped_sources(tmp_path, count=5):
    svc, adapter, session, events = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    session.state.update(live_first_research=True, research_run_id='many-sources')
    urls = [f'https://archive.example/checked-{i}' for i in range(count)]
    with svc.store.tx() as db:
        story = svc._story_row(db, session.resource_id)
        begin_research_run(db, story_id=session.resource_id, poi_key='wiki:77', goal='Check architectural history',
                           scope='architecture', expected_story_revision=story['revision'], identity_generation=0,
                           run_id='many-sources', now=svc.store.now())
        set_run_state(db, 'many-sources', 'extracting', now=svc.store.now())
        for i, url in enumerate(urls):
            register_discovered_source(db, run_id='many-sources', url=url, title=f'Checked source {i}', status='discovered', now=svc.store.now())
            if i < 4:
                document = persist_source_version(db, run_id='many-sources', requested_url=url, final_url=url,
                    title=f'Checked source {i}', content_type='text/html', http_status=200, redirect_chain=[],
                    normalized_text=f'Checked source {i} supplies no eligible new assertions. ' * 3,
                    read_status='complete', now=svc.store.now())
                finish_text(db, 'many-sources', document)
        research = json.loads(story['research_json'])
        research['live_web_searches'] = [{'research_run_id': 'many-sources', 'source_urls': urls,
            'coverage_goal': 'Check architectural history', 'extraction_scope': 'architecture',
            'save_batch_id': 'saved-discovery', 'discovery_only': True}]
        research['grounding_sources'] = [{'url': url, 'title': f'Checked source {i}', 'type': 'web', 'supports': []}
                                         for i, url in enumerate(urls)]
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    return svc, adapter, session, events, urls


@pytest.mark.asyncio
async def test_fifth_source_read_and_saved_after_four_checked_sources_without_replaying_prefix(tmp_path, public_dns):
    svc, adapter, session, _, urls = scoped_sources(tmp_path)
    fetched = []
    async def transport(request):
        fetched.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/html'},
                              text='<main><p>Fifth checked source has no additional eligible assertions. It discusses this gate.</p></main>')
    reader = GeminiClient(svc.settings, svc.store)
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    svc.providers.gemini._fetch_page_documents = reader._fetch_page_documents
    try:
        with svc.store.connection() as db:
            before = run_manifest(db, 'many-sources')
        assert before['counts']['chunk_batches_total'] == 4
        page = await adapter._get_research_chunk(session, {'run_id': 'many-sources'})
        assert page['evidence_passages'] and not page.get('all_chunks_processed')
        assert len(fetched) == 1 and httpx.URL(fetched[0]).path == httpx.URL(urls[4]).path
        saved = await adapter.execute_tool(session, {'name': 'save_research_facts', 'id': 'checked-fifth',
            'args': {'facts': [], 'batch_reviewed': True, 'source_matches_poi': True, 'source_content_valid': True}})
        assert saved['completed'] is True
        with svc.store.connection() as db:
            after = run_manifest(db, 'many-sources')
        assert after['counts']['chunk_batches_total'] == 5
        assert after['run']['state'] == 'completed'
        assert {b['batch_id']: b['payload_sha256'] for b in before['chunk_batches']} == {
            b['batch_id']: b['payload_sha256'] for b in after['chunk_batches'] if b['batch_id'] in {x['batch_id'] for x in before['chunk_batches']}}
    finally:
        await reader.search_http.aclose()


def test_continuation_after_four_checked_sources_requests_fifth_and_keeps_run_writable(tmp_path):
    svc, adapter, session, _, _ = scoped_sources(tmp_path)
    writes = []
    adapter.write = lambda _, event: writes.append(event)
    adapter._continue_pending_research(session)
    assert session.state['research_continuation_queued'] is True
    assert len(writes) == 1 and 'get_research_chunk' in writes[0]['text']
    assert 'three full-source' not in writes[0]['text']
    with svc.store.connection() as db:
        assert run_manifest(db, 'many-sources')['run']['state'] == 'extracting'


@pytest.mark.asyncio
async def test_inventory_guard_cannot_summarize_zero_findings_while_fifth_source_unread(tmp_path):
    _, adapter, session, _, _ = scoped_sources(tmp_path)
    with pytest.raises(ConflictError, match='Zero new durable observations'):
        await adapter.execute_tool(session, {'name': 'get_facts', 'args': {}})


@pytest.mark.parametrize('path', ['continue', 'read'])
@pytest.mark.asyncio
async def test_empty_discovery_is_partial_in_both_completion_paths(tmp_path, path):
    svc, adapter, session, _ = make_service(tmp_path)
    mark_identity_ready(svc, session.resource_id)
    session.state.update(live_first_research=True, research_run_id='empty-discovery')
    with svc.store.tx() as db:
        story = svc._story_row(db, session.resource_id)
        begin_research_run(db, story_id=session.resource_id, poi_key='wiki:77', goal='Find more checked facts',
                           scope='history', expected_story_revision=story['revision'], identity_generation=0,
                           run_id='empty-discovery', now=svc.store.now())
        set_run_state(db, 'empty-discovery', 'extracting', now=svc.store.now())
    if path == 'continue':
        adapter._continue_pending_research(session)
    else:
        page = await adapter._get_research_chunk(session, {'run_id': 'empty-discovery'})
        assert page['completed'] is False and page['partial'] is True
    with svc.store.connection() as db:
        manifest = run_manifest(db, 'empty-discovery')
        assert manifest['run']['state'] == 'partial'
        assert manifest['run']['completed_at'] is None
        assert manifest['counts']['sources_discovered'] == 0


@pytest.mark.asyncio
async def test_live_failed_sources_are_checked_without_a_total_three_source_cap(tmp_path):
    from test_facts_research_finish import fallback, URL
    from street_story.research_runs import register_discovered_source
    svc, adapter, session, _, run_id, _, reader = await fallback(tmp_path)
    session.state['live_first_research'] = True
    with svc.store.tx() as db:
        for n in range(5):
            register_discovered_source(db, run_id=run_id, url=f'https://source{n}.example/page', title='Source', status='snippet_only', now=n + 1)
    calls = []
    async def empty(urls, context):
        calls.extend(urls)
        return []
    svc.providers.gemini._fetch_page_documents = empty
    result = await adapter._get_research_chunk(session, {'run_id': run_id, 'source_url': URL})
    assert len(calls) == 6
    assert result['partial'] and not result['completed'] and result['full_source_attempts'] == 6
    with svc.store.connection() as db:
        assert run_manifest(db, run_id)['run']['status_detail'] == 'live_no_new_confirmed_facts'
    await reader.search_http.aclose()
