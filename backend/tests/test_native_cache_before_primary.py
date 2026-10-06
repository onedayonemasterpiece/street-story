"""Exact cache hits avoid provider sends; misses retain ordinary route order."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_native_comparison_reuse import setup
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.service import canonical, ConflictError
from street_story.research_control import stop_research


def ready(tmp_path):
    adapter, origin, current, unit, snapshot, context, receipt = setup(tmp_path)
    adapter.native_vision = SimpleNamespace(
        available=True, compare_visual=AsyncMock(side_effect=AssertionError("no native inference permitted"))
    )
    adapter.primary_vision = SimpleNamespace(
        available=True, compare_visual=AsyncMock(return_value={"result": {"status": "uncertain"}, "receipt": {"provider": "google"}})
    )
    with adapter.service.store.connection() as db:
        story = dict(adapter.service._story_row(db, current["story_id"]))
    return adapter, origin, current, unit, snapshot, context, receipt, story


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["match", "mismatch"])
async def test_verified_exact_cache_before_available_primary_preserves_original_verdict(tmp_path, status):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    receipt["result"]["status"] = status
    with adapter.service.store.tx() as db:
        db.execute("UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?", (canonical(receipt), origin["attempt_id"]))
    result = await adapter.visual_verdict(snapshot, story, VERDICT_SCHEMA, context)
    assert result["result"] == receipt["result"] and result["receipt"]["model"] == receipt["model"]
    assert result["receipt"]["reused_from"]["attempt_id"] == origin["attempt_id"]
    assert result["receipt"]["usage"] == {"totalTokens": 0, "cost": 0} and result["receipt"]["inference_performed"] is False
    adapter.primary_vision.compare_visual.assert_not_awaited()
    adapter.native_vision.compare_visual.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pixels", "context", "schema", "model", "profile", "unknown", "disabled_route"])
async def test_cache_miss_or_unqualified_cache_keeps_primary_and_creates_no_attempt(tmp_path, change):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    with adapter.service.store.tx() as db:
        db.execute("DELETE FROM research_provider_attempts WHERE attempt_id=?", (current["attempt_id"],))
    schema = json.loads(canonical(VERDICT_SCHEMA))
    if change == "pixels":
        snapshot += b"changed"
    elif change == "context":
        context = canonical({**json.loads(context), "new_hypothesis": "different candidate"})
    elif change == "schema":
        schema["properties"]["confidence"]["minimum"] = 0.99
    elif change == "model":
        receipt["model"] = "unqualified"
    elif change == "profile":
        receipt["profile_verified"] = False
    elif change == "unknown":
        receipt["phase"] = "unknown"
    elif change == "disabled_route":
        adapter.native_vision.available = False
    with adapter.service.store.tx() as db:
        db.execute("UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?", (canonical(receipt), origin["attempt_id"]))
    result = await adapter.visual_verdict(snapshot, story, schema, context)
    assert result["receipt"]["provider"] == "google"
    adapter.primary_vision.compare_visual.assert_awaited_once()
    adapter.native_vision.compare_visual.assert_not_awaited()
    with adapter.service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM research_provider_attempts WHERE story_id=?", (story["id"],)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_existing_unknown_native_turn_is_not_overwritten_by_origin_cache(tmp_path):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    pending = {"binding": current, "phase": "unknown", "thread_id": "pending-thread", "turn_id": "pending-turn"}
    with adapter.service.store.tx() as db:
        db.execute("UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?", (canonical(pending), current["attempt_id"]))
    result = await adapter.visual_verdict(snapshot, story, VERDICT_SCHEMA, context)
    assert result["receipt"]["provider"] == "google"
    adapter.native_vision.compare_visual.assert_not_awaited()
    with adapter.service.store.connection() as db:
        assert (
            json.loads(
                db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?", (current["attempt_id"],)).fetchone()[0]
            )
            == pending
        )


@pytest.mark.asyncio
async def test_stop_fence_applies_before_cache_hit_or_any_provider(tmp_path):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    stop_research(adapter.service, story["id"], purpose="identity")
    with pytest.raises(ConflictError):
        await adapter.visual_verdict(snapshot, story, VERDICT_SCHEMA, context)
    adapter.primary_vision.compare_visual.assert_not_awaited()
    adapter.native_vision.compare_visual.assert_not_awaited()


@pytest.mark.asyncio
async def test_superseded_worker_cannot_use_cache_or_send_to_primary(tmp_path):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    story.update(_research_job_id="missing-job", _research_job_attempt=1)
    with pytest.raises(ConflictError):
        await adapter.visual_verdict(snapshot, story, VERDICT_SCHEMA, context)
    adapter.primary_vision.compare_visual.assert_not_awaited()
    adapter.native_vision.compare_visual.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered", [False, True])
async def test_own_completed_replay_must_equal_verified_origin_before_skipping_primary(tmp_path, tampered):
    adapter, origin, current, unit, snapshot, context, receipt, story = ready(tmp_path)
    saved = adapter.reuse_native_verdict(current, unit, snapshot, VERDICT_SCHEMA, context)
    if tampered:
        saved["profile_verified"] = False
        with adapter.service.store.tx() as db:
            db.execute("UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?", (canonical(saved), current["attempt_id"]))
    result = await adapter.visual_verdict(snapshot, story, VERDICT_SCHEMA, context)
    if tampered:
        assert result["receipt"]["provider"] == "google"
        adapter.primary_vision.compare_visual.assert_awaited_once()
    else:
        assert result["result"] == receipt["result"]
        adapter.primary_vision.compare_visual.assert_not_awaited()
    adapter.native_vision.compare_visual.assert_not_awaited()
