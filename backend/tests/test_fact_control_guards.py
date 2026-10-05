"""Fact intake keeps the frozen Stop epoch through every awaited boundary."""
import json
from types import SimpleNamespace

import pytest

from street_story.headless_facts import HeadlessFacts
from street_story.live import StreetStoryLiveAdapter
from street_story.research_control import resume_research, stop_research
from street_story.service import ConflictError
from test_headless_facts import controlled_public_dns as controlled_public_dns, fixture


def stop_resume(svc, sid):
    stop_research(svc, sid, purpose='facts')
    resume_research(svc, sid, purpose='facts')


def fresh_job(svc, job):
    with svc.store.connection() as db:
        return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())


@pytest.mark.asyncio
async def test_fact_specific_discovery_is_preferred_and_legacy_search_remains_compatible(tmp_path):
    svc, job, researcher, reader, _ = await fixture(tmp_path)
    calls = []
    original = researcher.search_articles

    async def fact_search(query, story):
        calls.append(story['_fact_research_control_revision'])
        return await original(query, story)

    researcher.search_fact_articles = fact_search
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        assert calls == [0] and researcher.searches == 1
        assert len(svc.story(job['story_id'])['facts']) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['search', 'fetch', 'extract'])
async def test_stop_resume_rejects_old_async_result_before_next_provider_or_commit(tmp_path, boundary):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    sid = job['story_id']
    if boundary == 'search':
        original = researcher.search_articles

        async def interrupted_search(query, story):
            result = await original(query, story)
            stop_resume(svc, sid)
            return result

        researcher.search_articles = interrupted_search
    elif boundary == 'fetch':
        original = svc.providers.gemini._fetch_page_documents

        async def interrupted_fetch(*args):
            result = await original(*args)
            stop_resume(svc, sid)
            return result

        svc.providers.gemini._fetch_page_documents = interrupted_fetch
    else:
        researcher.after_extract = lambda _: stop_resume(svc, sid)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with svc.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM research_chunk_batches').fetchone()[0] == 0
            assert db.execute("SELECT state FROM research_runs WHERE run_id='headless-run'").fetchone()[0] == 'partial'
            if boundary == 'search':
                assert db.execute('SELECT COUNT(*) FROM research_run_sources').fetchone()[0] == 0
        assert not researcher.pages if boundary != 'extract' else len(researcher.pages) == 1
        if boundary == 'search':
            assert not fetches
        # A fresh attempt adopts the new epoch and reuses the frozen recipe.
        if boundary == 'search':
            researcher.search_articles = original
        elif boundary == 'fetch':
            svc.providers.gemini._fetch_page_documents = original
        else:
            researcher.after_extract = None
        await HeadlessFacts(svc).run(fresh_job(svc, job), 'headless-run', 'Find historical facts', 'history')
        assert len(svc.story(sid)['facts']) == 1
        if boundary == 'extract':
            assert len(researcher.model_units) == 1  # Known result reuse, no blind duplicate inference.
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_persistent_stop_blocks_search_and_page_provider_calls(tmp_path):
    svc, job, researcher, reader, fetches = await fixture(tmp_path)
    stop_research(svc, job['story_id'], purpose='facts')
    adapter = StreetStoryLiveAdapter(svc, emit=lambda *_: None, write=lambda *_: None)
    session = SimpleNamespace(id='controlled-live', resource_id=job['story_id'], state={}, closed=False)
    try:
        await HeadlessFacts(svc).run(job, 'headless-run', 'Find historical facts', 'history')
        with pytest.raises(ConflictError) as blocked:
            await adapter._search_web(session, 'blocked-search', {'query': 'Gate history'})
        assert blocked.value.code == 'live_research_stopped'
        with pytest.raises(ConflictError) as blocked:
            await adapter._get_research_chunk(session, {'run_id': 'headless-run'})
        assert blocked.value.code == 'live_research_stopped'
        assert researcher.searches == 0 and not researcher.pages and not fetches
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_failure', [False, True])
async def test_live_search_late_success_or_failure_cannot_overwrite_resumed_run(tmp_path, provider_failure):
    svc, job, _, reader, _ = await fixture(tmp_path)
    adapter = StreetStoryLiveAdapter(svc, emit=lambda *_: None, write=lambda *_: None)
    session = SimpleNamespace(id='controlled-live', resource_id=job['story_id'], state={'recent_user': []}, closed=False, model='controlled')
    original = svc.providers.gemini.search_web

    async def interrupted_search(*args):
        result = await original(*args)
        stop_resume(svc, job['story_id'])
        if provider_failure:
            raise RuntimeError('Controlled late provider failure')
        return result

    svc.providers.gemini.search_web = interrupted_search
    try:
        with pytest.raises(ConflictError) as stale:
            await adapter._search_web(session, 'interrupted-live-search', {'query': 'Gate history'})
        assert stale.value.code == 'live_research_control_changed'
        with svc.store.connection() as db:
            run = db.execute('SELECT state,status_detail FROM research_runs WHERE run_id=?',
                             (session.state['research_run_id'],)).fetchone()
            assert dict(run) == {'state': 'partial', 'status_detail': 'explicit_resume'}
            assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 0
        assert not session.state.get('research_pending_page')
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_live_epoch_is_sticky_and_stop_resume_cannot_advance_old_cursor(tmp_path):
    svc, job, _, reader, _ = await fixture(tmp_path)
    adapter = StreetStoryLiveAdapter(svc, emit=lambda *_: None, write=lambda *_: None)
    session = SimpleNamespace(id='controlled-live', resource_id=job['story_id'], state={}, closed=False)
    try:
        with svc.store.connection() as db:
            adapter._research_run_guard(db, session, 'headless-run')
        assert session.state['fact_research_control_revision'] == 0
        stop_resume(svc, job['story_id'])
        with pytest.raises(ConflictError) as stale:
            await adapter._get_research_chunk(session, {'run_id': 'headless-run'})
        assert stale.value.code == 'live_research_control_changed'
        assert not session.state.get('research_pending_page')
        assert session.state['fact_research_control_revision'] == 0
        new = SimpleNamespace(id='fresh-live', resource_id=job['story_id'], state={}, closed=False)
        with svc.store.connection() as db:
            adapter._research_run_guard(db, new, 'headless-run')
        assert new.state['fact_research_control_revision'] > 0
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_identity_stop_does_not_cancel_fact_epoch(tmp_path):
    svc, job, _, reader, _ = await fixture(tmp_path)
    adapter = StreetStoryLiveAdapter(svc, emit=lambda *_: None, write=lambda *_: None)
    session = SimpleNamespace(id='controlled-live', resource_id=job['story_id'], state={}, closed=False)
    try:
        with svc.store.connection() as db:
            adapter._research_run_guard(db, session, 'headless-run')
        stop_research(svc, job['story_id'], purpose='identity')
        with svc.store.connection() as db:
            adapter._research_run_guard(db, session, 'headless-run')
        assert session.state['fact_research_control_revision'] == 0
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_old_scope_stop_does_not_block_current_generation(tmp_path):
    svc, job, _, reader, _ = await fixture(tmp_path)
    adapter = StreetStoryLiveAdapter(svc, emit=lambda *_: None, write=lambda *_: None)
    session = SimpleNamespace(id='controlled-live', resource_id=job['story_id'], state={}, closed=False)
    try:
        with svc.store.tx() as db:
            story = svc._story_row(db, job['story_id'])
            research = json.loads(story['research_json'])
            research.update(fact_research_cancelled=True, research_controls={'facts': {
                'stopped': True, 'photo_sha256': story['photo_sha256'], 'identity_generation': -1, 'revision': 4}})
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
        with svc.store.connection() as db:
            adapter._research_run_guard(db, session, 'headless-run')
        assert session.state['fact_research_control_revision'] == 4
    finally:
        await reader.search_http.aclose()
