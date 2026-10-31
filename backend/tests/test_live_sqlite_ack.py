from __future__ import annotations

import asyncio
import threading
import time

import pytest

from street_story.live import record_live_diagnostic
from test_live_editor import make_service


@pytest.mark.asyncio
async def test_live_audio_ack_is_not_starved_by_locked_diagnostics_and_transcripts(tmp_path):
    service, adapter, session, _ = make_service(tmp_path)
    locked = threading.Event()

    def hold_writer():
        db = service.store.connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(0.65)  # Reproduces the large-story projection writer lock.
            db.execute("ROLLBACK")
        finally:
            db.close()

    writer = threading.Thread(target=hold_writer, daemon=True)
    writer.start()
    assert await asyncio.to_thread(locked.wait, 2)

    # The socket transport schedules audio ACKs on this very event loop.
    loop = asyncio.get_running_loop()
    ack = loop.create_future()
    loop.call_later(0.05, ack.set_result, "audio_ack")
    started = loop.time()
    diagnostic = record_live_diagnostic(
        service, session.resource_id, session.id, "android", "pcm_enqueued", {"bytes": 960}
    )
    adapter._persist_live_message(session, "user", "На улице")
    adapter._persist_live_message(session, "user", "На улице стоит здание")
    adapter._finalize_live_message(session)
    saved = adapter._persist_live_message(session, "assistant", "Понимаю")
    try:
        assert await asyncio.wait_for(ack, timeout=0.3) == "audio_ack"
        assert loop.time() - started < 0.3
    finally:
        await asyncio.to_thread(writer.join, 2)

    await asyncio.wait_for(asyncio.wrap_future(diagnostic), 3)
    await asyncio.wait_for(asyncio.wrap_future(saved), 3)
    story = await asyncio.to_thread(service.story, session.resource_id)
    assert [(item["role"], item["text"]) for item in story["live_messages"]] == [
        ("user", "На улице стоит здание"),
        ("assistant", "Понимаю"),
    ]
    assert story["live_messages"][0]["final"]
