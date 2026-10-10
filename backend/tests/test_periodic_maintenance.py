import asyncio
from types import SimpleNamespace

import pytest

from test_live_editor import make_service


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
