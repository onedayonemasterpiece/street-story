"""Caption delivery is a durable product mutation, never an optimistic voice promise."""
from __future__ import annotations

import asyncio
import json

import pytest

from street_story.live import StreetStoryLiveAdapter, ensure_live_schema
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.service import canonical
from test_caption_writer import prepared


def request(command_id="caption-owner-1"):
    return {"name": "edit_text", "id": command_id, "args": {
        "expected_text_revision": 0,
        "change_summary": "Автор попросил готовый текст",
        "writing_request": "Да, готовь текст по выбранным фактам и концепции."}}


def connection_again(service):
    reloaded = MvpLocationStreetStoryService(service.settings, service.providers)
    ensure_live_schema(reloaded)
    return reloaded


@pytest.mark.asyncio
async def test_false_voice_promise_has_no_job_and_legacy_orphan_is_reported_not_spinner(tmp_path):
    service, adapter, session = prepared(tmp_path)
    sid = session.resource_id
    assert service.story(sid)["draft_writing"] is None
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE story_id=? AND kind='caption'", (sid,)).fetchone()[0] == 0
    # A voice reply like "Почти готово" must never constitute an accepted request.
    assert adapter._compact_context(adapter._topic_state(sid))["draft_writing"] is None
    with service.store.tx() as db:
        row = service._story_row(db, sid)
        research = json.loads(row["research_json"] or "{}")
        research["draft_writing"] = {"started_at": service.store.now(), "command_id": "lost-old-coroutine"}
        db.execute("UPDATE stories SET research_json=? WHERE id=?", (canonical(research), sid))
    now = service.story(sid)
    assert now["draft_writing"] is None
    assert now["draft_writing_last"]["error"] == "caption_job_missing"
    assert now["draft_text"].startswith("Старый текст.")  # no loss/reset


@pytest.mark.asyncio
async def test_accepted_job_persists_before_writer_and_completes_after_live_callback_loss(tmp_path):
    service, adapter, session = prepared(tmp_path)
    sid = session.resource_id
    started_writing = asyncio.Event()
    continue_writing = asyncio.Event()
    calls = []

    async def write_caption(**kwargs):
        calls.append(kwargs)
        started_writing.set()
        await continue_writing.wait()
        return {"text": "Готовый связный текст.\n\nУлица 1\n#Калининград", "model": "gemini-3.5-flash-lite"}

    service.providers.gemini.write_caption = write_caption
    started = await adapter.execute_tool(session, request())
    assert started["writing"] == "started" and started["job_id"].startswith("job_")
    with service.store.connection() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (started["job_id"],)).fetchone()
        assert job["kind"] == "caption" and job["state"] in {"ready", "running"}
        assert json.loads(job["payload_json"])["args"]["writing_request"].startswith("Да,")
    await asyncio.wait_for(started_writing.wait(), 12)
    session.closed = True  # Lost web-socket callback must not cancel the worker.
    continue_writing.set()
    await asyncio.wait_for(adapter._writing_tasks[sid], 12)
    new_connection = connection_again(service)
    stored = new_connection.story(sid)
    assert stored["draft_writing"] is None
    assert stored["draft_text"].startswith("Готовый связный текст.")
    assert stored["draft_composed_by"] == "writer:gemini-3.5-flash-lite"
    assert stored["draft_writing_last"]["ok"] is True
    assert stored["draft_writing_last"]["job_id"] == started["job_id"]
    assert stored["draft_writing_last"]["text_revision"] == 1
    fresh_reader = StreetStoryLiveAdapter(new_connection, lambda *_: None, lambda *_: None)
    readback = fresh_reader._compact_context(fresh_reader._topic_state(sid))
    assert readback["draft_text"].startswith("Готовый связный текст.")
    assert readback["draft_writing_last"]["job_id"] == started["job_id"]
    assert len(calls) == 1

    # A later exact replay or lease recovery must not send a second generation.
    repeated = await adapter.execute_tool(session, request())
    assert repeated["writing"] == "completed"
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',lease_until=0 WHERE id=?", (started["job_id"],))
    assert await new_connection.run_once(claim_kind="caption", claim_story_id=sid)
    assert len(calls) == 1
    assert new_connection.story(sid)["draft_text"] == stored["draft_text"]


@pytest.mark.asyncio
async def test_queue_survives_crash_before_dispatch_and_recovers_same_owner_request(tmp_path):
    service, adapter, session = prepared(tmp_path)
    sid = session.resource_id
    calls = []

    async def write_caption(**kwargs):
        calls.append(kwargs)
        return {"text": "Текст после перезапуска.\n\nУлица 1\n#Калининград",
                "model": "gemini-3.5-flash-lite"}

    service.providers.gemini.write_caption = write_caption
    started = await adapter.execute_tool(session, request("crash-before-run"))
    task = adapter._writing_tasks[sid]
    task.cancel()  # simulates dropped in-memory starter before it runs
    await asyncio.gather(task, return_exceptions=True)
    with service.store.tx() as db:
        job = db.execute("SELECT state FROM jobs WHERE id=?", (started["job_id"],)).fetchone()
        # The starter may be cancelled before claiming, or just after taking
        # its lease. An actual restarted worker must respect a live lease and
        # reclaim the same job only after it expires (not steal a live task).
        assert job["state"] in {"ready", "running"}
        if job["state"] == "running":
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?",
                       (service.store.now()-1, started["job_id"]))
    restarted = connection_again(service)
    restarted.recover_jobs()
    assert await restarted.run_once(claim_kind="caption", claim_story_id=sid)
    after = restarted.story(sid)
    assert after["draft_text"].startswith("Текст после перезапуска.")
    assert after["draft_writing"] is None and after["draft_writing_last"]["ok"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_caption_exposes_terminal_job_without_destroying_old_draft(tmp_path):
    from street_story.gemini import GeminiUnavailable

    service, adapter, session = prepared(tmp_path)
    sid = session.resource_id

    async def unavailable(**_kwargs):
        raise GeminiUnavailable(0, "all_caption_writer_models_unavailable")

    service.providers.gemini.write_caption = unavailable
    accepted = await adapter.execute_tool(session, request("writer-unavailable"))
    await adapter._writing_tasks[sid]
    state = connection_again(service).story(sid)
    assert state["draft_writing"] is None
    assert state["draft_writing_last"]["ok"] is False
    assert state["draft_writing_last"]["error"] == "caption_writer_unavailable"
    assert state["draft_text"].startswith("Старый текст.")
    with service.store.connection() as db:
        row = db.execute("SELECT state,last_error FROM jobs WHERE id=?", (accepted["job_id"],)).fetchone()
    assert row["state"] == "failed"
    assert row["last_error"] == "caption_writer_unavailable"
    assert (await adapter.execute_tool(session, request("writer-unavailable")))["writing"] == "failed"
