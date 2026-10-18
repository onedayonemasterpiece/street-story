import asyncio
import json
from types import SimpleNamespace

import pytest

from test_live_editor import make_service


def test_claim_maintains_only_the_selected_story_budget(tmp_path, monkeypatch):
    from street_story import research_budget
    from test_identity_lifecycle import create, make_service as identity_service
    svc, _ = identity_service(tmp_path)
    stories = [create(svc, client=f'budget-claim-{i}') for i in range(3)]
    assert len({story['id'] for story in stories}) == 3
    for story in stories:
        svc.ensure_identity(story['id'])
    calls = []
    ensure = research_budget.ensure_budget

    def addressed(service, story_id, **kwargs):
        calls.append(story_id)
        return ensure(service, story_id, **kwargs)

    monkeypatch.setattr(research_budget, 'ensure_budget', addressed)
    claimed = svc._claim(claim_kind='identity')
    assert claimed and calls == [claimed['story_id']]
    calls.clear()
    assert svc._claim(claim_kind='visual') is None
    assert calls == []


@pytest.mark.asyncio
async def test_periodic_maintenance_migrates_only_active_legacy_queued_budgets(tmp_path, monkeypatch):
    from street_story import research_budget
    from test_identity_lifecycle import create, make_service as identity_service
    svc, _ = identity_service(tmp_path)
    active, current, terminal = [create(svc, client=f'budget-maintenance-{i}') for i in range(3)]
    for story in (active, current, terminal):
        svc.ensure_identity(story['id'])
    with svc.store.tx() as db:
        for story in (active, terminal):
            research = json.loads(svc._story_row(db, story['id'])['research_json'])
            research['research_budget']['policy_version'] = 'photo-research-wave-v1'
            research['research_budget']['identity_deadline_at'] = 180
            research['research_budget']['deadline_at'] = 480
            if story is terminal:
                research['automatic_research_outcome'] = {'outcome': 'deadline_exceeded', 'reason': 'historical'}
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
        before_jobs = [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')]
        before_terminal = svc._story_row(db, terminal['id'])['research_json']
    calls = []
    ensure = research_budget.ensure_budget

    def addressed(service, story_id, **kwargs):
        calls.append(story_id)
        return ensure(service, story_id, **kwargs)

    monkeypatch.setattr(research_budget, 'ensure_budget', addressed)
    monkeypatch.setattr(svc, '_observe_terminal_attempts', lambda: None)
    await svc.maintenance_tick()
    assert calls == [active['id']]
    calls.clear()
    await svc.maintenance_tick()
    assert calls == []
    with svc.store.connection() as db:
        budget = json.loads(svc._story_row(db, active['id'])['research_json'])['research_budget']
        assert budget['policy_version'] == 'photo-research-continuation-v2'
        assert budget['deadline_at'] is None and budget['identity_deadline_at'] is None
        assert svc._story_row(db, terminal['id'])['research_json'] == before_terminal
        assert [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')] == before_jobs


@pytest.mark.asyncio
async def test_periodic_original_readback_runs_while_all_worker_lanes_are_busy(tmp_path, monkeypatch):
    from street_story.app import create_app
    svc, _, session, _ = make_service(tmp_path)
    active = asyncio.Event()
    recovered = asyncio.Event()
    attempts = []

    async def busy_job(**kwargs):
        active.set()
        await asyncio.Event().wait()

    async def accounting():
        assert active.is_set()
        attempts.append('read_original')
        if len(attempts) == 1:
            raise LookupError('temporary readback failure')
        recovered.set()

    monkeypatch.setattr(svc, 'worker_story_ids', lambda **kwargs: [session.resource_id])
    monkeypatch.setattr(svc, 'run_once', busy_job)
    monkeypatch.setattr(svc, '_observe_terminal_attempts', lambda: None)
    svc.providers.research = SimpleNamespace(recover_accounting=accounting)
    app = create_app(svc.settings, svc)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(recovered.wait(), 3)
        assert attempts == ['read_original', 'read_original']


@pytest.mark.asyncio
async def test_idle_and_periodic_maintenance_serialize_original_accounting(tmp_path, monkeypatch):
    svc, _, _, _ = make_service(tmp_path)
    active = 0
    maximum = 0
    calls = 0

    async def accounting():
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.01)
        calls += 1
        active -= 1

    monkeypatch.setattr(svc, '_observe_terminal_attempts', lambda: None)
    svc.providers.research = SimpleNamespace(recover_accounting=accounting)
    await asyncio.gather(svc.maintenance_tick(), svc.maintenance_tick(), svc.maintenance_tick())
    assert calls == 3 and maximum == 1
