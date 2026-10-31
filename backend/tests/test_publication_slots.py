"""Owner publication slots: one command, two native queued posts, one central edit."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from street_story.publication_slots import DEFAULT_DESTINATIONS, choose_slot, delivery_time
from street_story.service import canonical
from test_publication_precision import live_ready


def ledger():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE publish_intents(scheduled_for TEXT,request_json TEXT,state TEXT)")
    return db


def test_nearest_slot_skips_occupied_evening_and_honors_daypart():
    db = ledger()
    now = datetime(2026, 10, 31, 18, 0, tzinfo=timezone(timedelta(hours=2)))
    db.execute("INSERT INTO publish_intents VALUES(?,?,?)",
               ("2026-10-31T19:10:00+02:00",
                canonical({"destinations": [DEFAULT_DESTINATIONS[0]]}), "scheduled"))
    slot = choose_slot(db, now=now)
    assert slot.isoformat() == "2026-11-01T09:10:00+02:00"
    assert choose_slot(db, day="tomorrow", period="evening", now=now).isoformat() == (
        "2026-11-01T19:10:00+02:00")
    with pytest.raises(ValueError, match="No free publication slot"):
        choose_slot(db, day="today", period="evening", now=now)



def test_explicit_test_channel_does_not_occupy_production_slots():
    db = ledger()
    now = datetime(2026, 10, 31, 21, 0, tzinfo=timezone(timedelta(hours=2)))
    db.execute("INSERT INTO publish_intents VALUES(?,?,?)",
               ("2026-11-01T09:10:00+02:00",
                canonical({"destinations": ["street_story_e2e_20260928_tg"],
                           "slot_at": "2026-11-01T09:10:00+02:00"}), "scheduled"))
    assert choose_slot(db, now=now).isoformat() == "2026-11-01T09:10:00+02:00"
    db.execute("INSERT INTO publish_intents VALUES(?,?,?)",
               ("2026-11-01T09:10:00+02:00",
                canonical({"destinations": [DEFAULT_DESTINATIONS[0]],
                           "slot_at": "2026-11-01T09:10:00+02:00"}), "scheduled"))
    assert choose_slot(db, now=now).isoformat() == "2026-11-01T19:10:00+02:00"


def test_provider_times_differ_without_extra_slot_occupancy():
    slot = datetime(2026, 11, 1, 9, 10, tzinfo=timezone(timedelta(hours=2)))
    tg, vk = [delivery_time(slot, alias) for alias in DEFAULT_DESTINATIONS]
    assert tg == "2026-11-01T09:10:00+02:00"
    assert vk == "2026-11-01T09:20:00+02:00"
    assert delivery_time(slot, "street_story_e2e_20260928_tg") == tg
    db = ledger()
    for due in (tg, vk):
        db.execute("INSERT INTO publish_intents VALUES(?,?,?)",
                   (due, canonical({"slot_at": tg, "destinations": [DEFAULT_DESTINATIONS[0]]}), "scheduled"))
    with pytest.raises(ValueError, match="No free publication slot"):
        choose_slot(db, day="tomorrow", period="morning",
                    now=datetime(2026, 10, 31, 8, tzinfo=timezone(timedelta(hours=2))))


def default_bootstrap():
    return {"routing_revision": 1,
            "destinations": [{"kind": "destination", "alias": alias,
                              "label": "История за поворотом", "provider": provider}
                             for alias, provider in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))],
            "capabilities": [{"destination": alias, "operation": "publish", "surface": "post",
                              "provider": provider, "status": "supported"}
                             for alias, provider in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))]}


@pytest.mark.asyncio
async def test_single_owner_publish_creates_two_independent_native_intents(tmp_path):
    svc, adapter, session, _ = live_ready(tmp_path)

    async def bootstrap():
        return default_bootstrap()

    svc.providers.vibepublish.bootstrap = bootstrap
    # Model may carry an old E2E target and absolute time. Ignore both for a bare publish.
    response = await adapter._publish_story(session.resource_id, "one-owner-command", {
        "day": "tomorrow", "destinations": ["street_story_e2e_20260928_tg"],
        "scheduled_for": "2026-11-01T23:59:00+02:00"})
    assert response["accepted"]
    with svc.store.connection() as db:
        rows = list(db.execute("SELECT * FROM publish_intents ORDER BY rowid"))
    assert len(rows) == 2
    requests = [json.loads(row["request_json"]) for row in rows]
    assert [item["destinations"] for item in requests] == [[DEFAULT_DESTINATIONS[0]], [DEFAULT_DESTINATIONS[1]]]
    due = [datetime.fromisoformat(item["scheduled_for"]) for item in requests]
    assert due[1] - due[0] == timedelta(minutes=10)
    assert requests[0]["publication_group"] == requests[1]["publication_group"]
    assert all(row["state"] == "pending" and row["vibepublish_operation_id"] is None for row in rows)
    # A replay of the same prepared owner command must never consume a second slot.
    adapter._confirm_publication(session.resource_id, "same-command-retry", {
        "confirmation_id": response["confirmation_id"],
        "slot_intent": {"day": "tomorrow", "period": None},
    })
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM publish_intents").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_text_edit_propagates_to_both_existing_provider_publications(tmp_path):
    svc, adapter, session, _ = live_ready(tmp_path)
    sid = session.resource_id
    svc.mutate_publish(sid, "initial-two", {
        "destinations": list(DEFAULT_DESTINATIONS), "slot_intent": {"day": "tomorrow"}})
    with svc.store.tx() as db:
        intents = list(db.execute("SELECT id FROM publish_intents ORDER BY rowid"))
        for index, row in enumerate(intents):
            db.execute("UPDATE publish_intents SET state='scheduled',"
                       "vibepublish_publication_id=?,vibepublish_revision=1,receipt_json=? WHERE id=?",
                       (f"pub-provider-{index}", canonical({"state": "scheduled"}), row["id"]))
    updates = []

    async def publication_update(pub_id, revision, change, key):
        updates.append((pub_id, revision, change, key))
        return {"operation_id": "edit-" + pub_id}

    async def status(op_id):
        return {"operation_id": op_id, "state": "verified", "operation_complete": True, "revision": 2}

    svc.providers.vibepublish.publication_update = publication_update
    svc.providers.vibepublish.status = status
    adapter._edit_text(sid, "edit-owner-text", {
        "expected_text_revision": 0,
        "new_text": "New BFU story text [Карта](https://maps.example/place)",
        "change_summary": "Corrected owner wording"})
    with svc.store.connection() as db:
        jobs = [dict(j) for j in db.execute(
            "SELECT * FROM jobs WHERE kind='publication_edit' ORDER BY created_at,rowid")]
    assert len(jobs) == 2
    for job in jobs:
        await svc._run_publication_edit(job)
    assert {x[0] for x in updates} == {"pub-provider-0", "pub-provider-1"}
    assert all(x[1] == 1 and x[2]["kind"] == "edit" and
               "media" not in x[2] for x in updates)
    by_pub = {pub_id: change for pub_id, _, change, _ in updates}
    assert by_pub["pub-provider-0"]["content"]["paragraphs"][0][-1] == {
        "kind": "link", "label": "Карта", "url": "https://maps.example/place"}
    assert by_pub["pub-provider-1"]["content"] == {
        "text": "New BFU story text Карта (https://maps.example/place)",
        "format": "plain"}
    with svc.store.connection() as db:
        rows = list(db.execute("SELECT * FROM publish_intents"))
    assert all(row["vibepublish_revision"] == 2 and
               json.loads(row["receipt_json"])["_publication_edit"]["phase"] == "verified"
               for row in rows)


def test_undo_and_verbatim_changes_propagate_to_both_native_posts(tmp_path):
    svc, adapter, session, _ = live_ready(tmp_path)
    sid = session.resource_id
    svc.mutate_publish(sid, "published-edit-paths", {
        "destinations": list(DEFAULT_DESTINATIONS),
        "slot_intent": {"day": "tomorrow"},
    })
    with svc.store.tx() as db:
        rows = list(db.execute("SELECT id FROM publish_intents ORDER BY rowid"))
        for i, row in enumerate(rows):
            db.execute("UPDATE publish_intents SET state='scheduled',"
                       "vibepublish_publication_id=?,vibepublish_revision=1 WHERE id=?",
                       (f"original-{i}", row["id"]))
    adapter._edit_text(sid, "first-edit", {
        "expected_text_revision": 0, "new_text": "First version",
        "change_summary": "Owner edited the publication",
    })
    adapter._undo(sid, "undo-first-edit")
    adapter._literal_begin(session, {"position": "end"})
    session.state["literal"]["buffer"].append("Дословное дополнение автора")
    adapter._literal_finish(session, "literal-after-schedule")
    with svc.store.connection() as db:
        jobs = [json.loads(row["payload_json"]) for row in db.execute(
            "SELECT payload_json FROM jobs WHERE kind='publication_edit' ORDER BY rowid")]
    assert len(jobs) == 6
    assert all(sum(job["intent_id"] == row["id"] for job in jobs) == 3 for row in rows)
    assert all("Дословное дополнение автора" in job["text"] for job in jobs[4:])


@pytest.mark.asyncio
async def test_image_edit_propagates_to_both_original_native_posts(tmp_path):
    svc, _, session, _ = live_ready(tmp_path)
    story_id = session.resource_id
    svc.mutate_publish(story_id, "image-case", {
        "destinations": list(DEFAULT_DESTINATIONS), "slot_intent": {"day": "tomorrow"}})
    with svc.store.tx() as db:
        rows = list(db.execute("SELECT id FROM publish_intents ORDER BY rowid"))
        for index, row in enumerate(rows):
            db.execute("UPDATE publish_intents SET state='scheduled',"
                       "vibepublish_publication_id=?,vibepublish_revision=3,receipt_json=? WHERE id=?",
                       (f"existing-post-{index}", canonical({"state": "scheduled"}), row["id"]))
        story = svc._story_row(db, story_id)
        svc._queue_publication_edits(db, story_id,
                                     text=str(story["draft_text"]),
                                     asset_ref="asset_verified_new_image", change_id="visual:2")
    calls = []

    async def publication_update(pub_id, rev, change, key):
        calls.append((pub_id, rev, change))
        return {"operation_id": "changed-" + pub_id}

    async def status(op_id):
        return {"operation_id": op_id, "state": "verified",
                "operation_complete": True, "revision": 4}

    svc.providers.vibepublish.publication_update = publication_update
    svc.providers.vibepublish.status = status
    with svc.store.connection() as db:
        jobs = [dict(row) for row in db.execute(
            "SELECT * FROM jobs WHERE kind='publication_edit' ORDER BY rowid")]
    assert len(jobs) == 2
    for job in jobs:
        await svc._run_publication_edit(job)
    assert {x[0] for x in calls} == {"existing-post-0", "existing-post-1"}
    assert all(rev == 3 and change["kind"] == "edit" and
               "content" not in change and
               change["media"][0]["source"]["id"] == "asset_verified_new_image"
               for _, rev, change in calls)


def test_cancel_enqueues_both_provider_posts_of_one_slot(tmp_path):
    svc, _, session, _ = live_ready(tmp_path)
    story_id = session.resource_id
    svc.mutate_publish(story_id, "cancel-slot", {
        "destinations": list(DEFAULT_DESTINATIONS), "slot_intent": {"day": "tomorrow"}})
    with svc.store.tx() as db:
        for index, row in enumerate(list(db.execute(
                "SELECT id FROM publish_intents ORDER BY rowid"))):
            db.execute("UPDATE publish_intents SET state='scheduled',"
                       "vibepublish_publication_id=?,vibepublish_revision=1 WHERE id=?",
                       (f"native-post-{index}", row["id"]))
    svc.mutate_cancel(story_id, "cancel-both", {})
    with svc.store.connection() as db:
        rows = list(db.execute("SELECT state,cancel_request_key FROM publish_intents ORDER BY rowid"))
        jobs = list(db.execute("SELECT * FROM jobs WHERE kind='cancel'"))
    assert len(rows) == len(jobs) == 2
    assert all(row["state"] == "cancel_pending" and row["cancel_request_key"] for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "attempted", "wrong_destination", "incomplete"])
async def test_telegram_preview_new_receipt_proves_exact_zero_dispatch_before_supported(tmp_path, invalid):
    svc, _, _, _ = live_ready(tmp_path)
    alias = DEFAULT_DESTINATIONS[0]
    observed = {"preview": 0, "bootstrap": 0}

    async def bootstrap():
        observed["bootstrap"] += 1
        snap = default_bootstrap()
        snap["capabilities"][0]["status"] = ("needs_review" if observed["bootstrap"] == 1
                                                   else "supported")
        return snap

    async def publish(body, key):
        assert body["mode"] == "preview" and body["to"] == [alias]
        observed["preview"] += 1
        return {"operation_id": "op_preview", "state": "accepted"}

    async def status(operation_id):
        assert operation_id == "op_preview"
        return {"operation_id": operation_id, "state": "needs_approval",
                "operation_complete": invalid != "incomplete", "review_token": "review-1",
                "deliveries": [{"destination": ("other" if invalid == "wrong_destination" else alias),
                                "provider": "telegram", "state": "needs_approval",
                                "observed": ("attempted" if invalid == "attempted" else "not_attempted")}]}

    svc.providers.vibepublish.bootstrap = bootstrap
    svc.providers.vibepublish.publish = publish
    svc.providers.vibepublish.status = status
    if invalid is None:
        result = await svc._publication_bootstrap([alias])
        assert result["routing_revision"] == 1
        assert observed == {"preview": 1, "bootstrap": 2}
    else:
        from street_story.providers import PermanentProviderError
        with pytest.raises(PermanentProviderError, match="did not prove no-dispatch"):
            await svc._publication_bootstrap([alias])
        assert observed["preview"] == 1
        assert observed["bootstrap"] == 1
