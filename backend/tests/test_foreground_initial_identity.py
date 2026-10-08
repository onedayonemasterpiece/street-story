import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from street_story import app as app_module
from street_story.headless_identity import HeadlessIdentity
from test_identity_lifecycle import create, make_service


@pytest.mark.asyncio
async def test_existing_foreground_lane_claims_initial_identity_while_general_research_is_blocked(tmp_path, monkeypatch):
    svc, _ = make_service(tmp_path)
    old = create(svc, 'old-background')
    with svc.store.tx() as db:
        old_job = svc._enqueue_job(db, old['id'], 'research', 'background', {'queue_priority': 'background'})
    background_entered, release_background = asyncio.Event(), asyncio.Event()
    identity_entered, release_identity, visual_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def research(job):
        background_entered.set()
        await release_background.wait()
    async def identity(job):
        calls.append(('identity', job['id'], asyncio.current_task().get_name()))
        identity_entered.set()
        await release_identity.wait()
    async def visual(self, job):
        calls.append(('identity_visual', job['id'], asyncio.current_task().get_name()))
        visual_finished.set()
    monkeypatch.setattr(svc, '_run_research', research)
    monkeypatch.setattr(svc, '_run_identity', identity)
    monkeypatch.setattr(svc, '_schedule_identity_visual', lambda: None)
    monkeypatch.setattr(svc, '_schedule_confirmed_facts', lambda: None)
    monkeypatch.setattr(HeadlessIdentity, 'run', visual)
    monkeypatch.setattr(app_module, 'create_live_host', lambda *args: SimpleNamespace(stop_all=AsyncMock()))
    monkeypatch.setattr(app_module, 'install_live_socket_routes', lambda *args: None)
    app = app_module.create_app(svc.settings, svc)
    async with app.router.lifespan_context(app):
        try:
            await asyncio.wait_for(background_entered.wait(), 2)
            fresh = create(svc, 'fresh-owner-photo')
            svc.ensure_identity(fresh['id'])
            await asyncio.wait_for(identity_entered.wait(), 2)
            # Another worker cannot claim the same still-running identity.
            assert not await svc.run_once(claim_kind='identity')
            with svc.store.connection() as db:
                initial = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='identity'", (fresh['id'],)).fetchone())
                assert initial['state'] == 'running' and initial['attempts'] == 1
                assert db.execute('SELECT state FROM jobs WHERE id=?', (old_job,)).fetchone()[0] == 'running'
            with svc.store.tx() as db:
                visual_job = svc._enqueue_job(db, fresh['id'], 'identity_visual', 'visual-current', {})
            await asyncio.wait_for(visual_finished.wait(), 2)
            assert not release_identity.is_set()  # Ready refs do not wait for discovery's slow tail.
            assert calls == [('identity', initial['id'], 'street-story-identity-discovery-worker'),
                             ('identity_visual', visual_job, 'street-story-identity-visual-worker')]
            release_identity.set()
            await asyncio.sleep(0)
            with svc.store.connection() as db:
                completed = [dict(row) for row in db.execute('SELECT state,attempts FROM jobs WHERE id IN (?,?)', (initial['id'], visual_job))]
                assert len(completed) == 2 and all(row == {'state': 'done', 'attempts': 1} for row in completed)
            assert not release_background.is_set()
        finally:
            release_identity.set()
            release_background.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['identity', 'identity_visual'])
async def test_empty_foreground_claim_skips_background_fact_scheduling_and_accounting_recovery(tmp_path, monkeypatch, kind):
    svc, gemini = make_service(tmp_path)
    def forbidden_schedule():
        pytest.fail('Foreground identity must not schedule background facts')
    async def forbidden_recovery():
        pytest.fail('Empty foreground identity must not wait on quota accounting')
    monkeypatch.setattr(svc, '_schedule_confirmed_facts', forbidden_schedule)
    gemini.quota = SimpleNamespace(recover=forbidden_recovery)
    assert not await svc.run_once(claim_kind=kind)


@pytest.mark.asyncio
async def test_general_worker_retains_background_schedule_and_accounting_recovery(tmp_path, monkeypatch):
    svc, gemini = make_service(tmp_path)
    calls = []
    monkeypatch.setattr(svc, '_schedule_confirmed_facts', lambda: calls.append('facts'))
    async def recovery():
        calls.append('accounting')
    gemini.quota = SimpleNamespace(recover=recovery)
    assert not await svc.run_once(exclude_kind='identity_visual')
    assert calls == ['facts', 'accounting']


@pytest.mark.asyncio
async def test_initial_identity_claim_does_not_consume_visual_publish_or_research_jobs(tmp_path):
    svc, _ = make_service(tmp_path)
    story = create(svc)
    with svc.store.tx() as db:
        for kind in ['identity_visual', 'research', 'visual', 'publish']:
            svc._enqueue_job(db, story['id'], kind, kind, {})
    assert not await svc.run_once(claim_kind='identity')
    with svc.store.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT kind,state,attempts FROM jobs')]
    assert {row['kind'] for row in rows} == {'identity_visual', 'research', 'visual', 'publish'}
    assert all(row['state'] == 'ready' and row['attempts'] == 0 for row in rows)


@pytest.mark.asyncio
async def test_closed_client_facts_for_fresh_story_progress_while_older_research_waits(tmp_path, monkeypatch):
    svc, _ = make_service(tmp_path)
    older, fresh = create(svc, 'slow-facts'), create(svc, 'closed-client')
    entered, newer_finished, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def research(job):
        calls.append(job['story_id'])
        if job['story_id'] == older['id']:
            entered.set()
            await release.wait()
        else:
            newer_finished.set()
    monkeypatch.setattr(svc, '_run_research', research)
    monkeypatch.setattr(svc, '_schedule_identity_visual', lambda: None)
    monkeypatch.setattr(svc, '_schedule_confirmed_facts', lambda: None)
    monkeypatch.setattr(app_module, 'create_live_host', lambda *args: SimpleNamespace(stop_all=AsyncMock()))
    monkeypatch.setattr(app_module, 'install_live_socket_routes', lambda *args: None)
    with svc.store.tx() as db:
        slow = svc._enqueue_job(db, older['id'], 'research', 'slow-facts', {})
    app = app_module.create_app(svc.settings, svc)
    async with app.router.lifespan_context(app):
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with svc.store.tx() as db:
                new = svc._enqueue_job(db, fresh['id'], 'research', 'closed-client-facts', {})
                svc._enqueue_job(db, older['id'], 'research', 'same-story-continuation', {})
            await asyncio.wait_for(newer_finished.wait(), 2)
            await asyncio.sleep(0)
            assert calls == [older['id'], fresh['id']]
            assert not release.is_set()
            with svc.store.connection() as db:
                assert db.execute('SELECT state FROM jobs WHERE id=?', (slow,)).fetchone()[0] == 'running'
                assert db.execute('SELECT state FROM jobs WHERE id=?', (new,)).fetchone()[0] == 'done'
                assert db.execute('SELECT count(*) FROM live_messages').fetchone()[0] == 0
        finally:
            release.set()
