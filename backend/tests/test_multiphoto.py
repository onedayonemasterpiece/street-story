"""Selected photo identity, order, comic planning and native-album dispatch."""
from __future__ import annotations

import hashlib
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from street_story.app import create_app
from street_story.config import reveal
from street_story.runtime import RuntimeStreetStoryService
from street_story.service import ProviderBundle, ConflictError, canonical
from street_story.publication_slots import DEFAULT_DESTINATIONS
from test_mvp_visual import PHOTO, NoopOSM, NoopWikipedia, settings
from test_publication_precision import live_ready


def shot(color):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class ComicWriter:
    def __init__(self):
        self.calls = 0

    async def _lite_chain(self, contents, config, parse):
        self.calls += 1
        assert len(contents) == 4  # one prompt and three photos, not three model plans
        payload = json.loads(contents[0].rsplit("\n\n", 1)[-1])
        cards = [
            {"notes": [payload["place"], "В 1910 году здесь открылся театр.", "листай →"],
             "arrows": ["", "", ""], "fact_ids": ["fact-1"],
             "hook": "В 1910 году здесь открылся театр."},
            {"notes": ["В 1920 году была реконструкция"], "arrows": [""],
             "fact_ids": ["fact-2"], "hook": ""},
            {"notes": ["История за поворотом Калининград", payload["city"], payload["address"]],
             "arrows": ["", "", ""], "fact_ids": [], "hook": ""},
        ]
        return parse(json.dumps({"text": "Комикс об объекте", "cards": cards}, ensure_ascii=False), "test-writer")


class ComicVibe:
    def __init__(self):
        self.ingresses = []
        self.commands = []
        self.operations = {}
        self.published = []

    async def bootstrap(self):
        return {"routing_revision": 3,
                "destinations": [{"kind": "destination", "alias": alias, "label": alias,
                                  "provider": network}
                                 for alias, network in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))],
                "capabilities": [{"destination": alias, "operation": "publish",
                                  "surface": "post", "provider": network, "status": "supported"}
                                 for alias, network in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))]}

    async def ingress_asset(self, data, mime, key):
        self.ingresses.append(key)
        return {"asset_id": "original-" + str(len(self.ingresses))}

    async def visual(self, payload, key):
        command = payload["command"]
        self.commands.append(command)
        if command["kind"] == "tune":
            op = "visual-" + str(len(self.operations) + 1)
            self.operations[op] = {"asset": "card-" + str(len(self.operations) + 1), "done": False}
            return {"operation_id": op, "state": "accepted"}
        assert command["kind"] == "select"
        for op in self.operations.values():
            if op["asset"] == "card-" + command["candidate_id"].removeprefix("choice-"):
                op["done"] = True
        return {"state": "verified"}

    async def status(self, operation):
        assert len(self.operations) >= 3, 'all first-batch imagegens must launch before status polling'
        state = self.operations[operation]
        index = operation.removeprefix("visual-")
        choice = {"id": "choice-" + index, "asset_ref": state["asset"],
                  "sha256": "a" * 64, "width": 40, "height": 30,
                  "selection_token": "test-token"}
        return {"receipts": [{"operation_id": operation,
                "state": "verified" if state["done"] else "needs_selection",
                "visual_job_id": "job-" + index, "visual_revision": 1,
                "candidates": [choice],
                **({"selected_asset_ref": state["asset"], "selected_sha256": "a" * 64}
                   if state["done"] else {})}]}

    async def read_asset(self, ref):
        return shot("blue"), "image/jpeg"

    async def publish(self, payload, key):
        self.published.append((payload, key))
        return {"operation_id": "published-" + str(len(self.published)), "state": "scheduled"}


def make_runtime(tmp_path):
    writer, vp = ComicWriter(), ComicVibe()
    svc = RuntimeStreetStoryService(settings(tmp_path),
        ProviderBundle(NoopOSM(), NoopWikipedia(), writer, vp))
    story = svc.create_story(key="create-story", client_story_id="multi-original",
        photo_sha256=hashlib.sha256(PHOTO).hexdigest(), photo_mime_type="image/jpeg",
        photo_bytes=PHOTO, voice_protocol="voice-chunks-v2", lat=54.71, lon=20.45)
    return svc, story["id"], writer, vp


def upload_three(svc, story_id):
    ids = [str(uuid.uuid4()) for _ in range(2)]
    for i, photo_id in enumerate(ids):
        data = shot("red" if i == 0 else "green")
        assert svc.add_story_photo(story_id, "upload-" + str(i), photo_id, data, "image/jpeg") == {"photo_id": photo_id}
        assert svc.add_story_photo(story_id, "upload-" + str(i), photo_id, data, "image/jpeg") == {"photo_id": photo_id}
    return ids


def test_migrate_source_upload_identity_and_reorder_stales_without_changing_editorial(tmp_path):
    svc, sid, _, _ = make_runtime(tmp_path)
    with svc.store.connection() as db:
        original = db.execute("SELECT photo_id,position,path FROM story_photos WHERE story_id=?", (sid,)).fetchone()
    assert original["photo_id"] == "source" and original["position"] == 0
    assert original["path"].endswith("source.original")
    ids = upload_three(svc, sid)
    photos = svc.order_story_photos(sid, "order-one", {"photo_ids": ["source", *ids]})["photos"]
    assert [p["photo_id"] for p in photos] == ["source", *ids]
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='ready_to_publish',draft_text='Original text',"
                   "vibepublish_asset_ref='asset-before',processed_image_url='/old',"
                   "visual_context_json=? WHERE id=?",
                   (canonical({"content_revision": "visual-before", "stale": False}), sid))
    changed = svc.order_story_photos(sid, "order-two", {"photo_ids": [ids[1], "source", ids[0]]})
    assert [p["photo_id"] for p in changed["photos"]] == [ids[1], "source", ids[0]]
    assert changed["cards"] == [] and changed["processed_image_url"] is None
    assert changed["visual"]["stale"] is True
    assert changed["draft_text"] == "Original text"
    assert svc.order_story_photos(sid, "order-two", {"photo_ids": [ids[1], "source", ids[0]]})["photos"] == changed["photos"]
    with pytest.raises(ConflictError) as exc:
        svc.add_story_photo(sid, "upload-conflict", ids[0], shot("yellow"), "image/jpeg")
    assert exc.value.code == "photo_id_conflict"
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM story_photos WHERE story_id=?", (sid,)).fetchone()[0] == 3
    restarted = RuntimeStreetStoryService(settings(tmp_path), svc.providers)
    assert [p["photo_id"] for p in restarted.story(sid)["photos"]] == [ids[1], "source", ids[0]]
    assert restarted._cover_photo_bytes(sid) == shot("green")


def test_invalid_order_rejects_before_state_change(tmp_path):
    svc, sid, _, _ = make_runtime(tmp_path)
    before = svc.story(sid)["photos"]
    for ids in ([], ["source"] * 2, ["missing"], ["source"] * 9):
        with pytest.raises(ConflictError):
            svc.order_story_photos(sid, "bad-" + str(ids), {"photo_ids": ids})
        assert svc.story(sid)["photos"] == before


@pytest.mark.asyncio
async def test_one_writer_plan_and_three_vibepublish_cards(tmp_path):
    svc, sid, writer, vp = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    svc.order_story_photos(sid, "order-comic", {"photo_ids": ["source", *ids]})
    snapshot = {"comic": {"photo_ids": ["source", *ids],
                          "cards": [{"photo_id": photo_id} for photo_id in ["source", *ids]]},
                "selected_facts": [{"fact_id": "fact-1", "text": "В 1910 году открылся театр."},
                                   {"fact_id": "fact-2", "text": "В 1920 году была реконструкция."}],
                "place_name": "Театр", "place_selection": {"address": "Светлогорск, улица Тестовая, 1"},
                "strongest_fact": "fact-1", "photo_format": {}, "content_revision": "comic-1", "stale": False}
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing',visual_context_json=? WHERE id=?",
                   (canonical(snapshot), sid))
        svc._enqueue_job(db, sid, "visual", "visual:test-comic",
                         {"content_revision": "comic-1", "selected_fact_ids": ["fact-1", "fact-2"]})
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    plan = await svc._plan_comic(sid, snapshot)
    assert writer.calls == 1
    assert plan["cards"][0]["notes"] == ["Театр", "В 1910 году здесь открылся театр.", "листай →"]
    assert plan["cards"][-1]["notes"] == ["История за поворотом Калининград",
        "Светлогорск", "Светлогорск, улица Тестовая, 1"]
    assert plan["cards"][0]["fact_ids"] == ["fact-1"]
    assert plan["cards"][1]["fact_ids"] == ["fact-2"]
    assert plan["cards"][2]["fact_ids"] == []
    with svc.store.tx() as db:
        snapshot["comic"]["plan"] = plan
        db.execute("UPDATE stories SET visual_context_json=? WHERE id=?", (canonical(snapshot), sid))
    await svc._run_visual(job)
    result = svc.story(sid)
    assert result["state"] == "ready_to_publish"
    assert len(result["cards"]) == 3
    assert [cmd["kind"] for cmd in vp.commands].count("tune") == 3
    assert writer.calls == 1
    with svc.store.connection() as db:
        visual = json.loads(svc._story_row(db, sid)["visual_context_json"])
        assert [p["asset_ref"] for p in visual["comic"]["cards"]] == ["card-1", "card-2", "card-3"]



@pytest.mark.asyncio
async def test_comic_cover_footer_survive_writer_variation_without_map_address(tmp_path):
    """Writer prose is not an exact-string API; publisher labels are mechanical."""
    svc, sid, writer, _ = make_runtime(tmp_path)
    ids = upload_three(svc, sid)

    async def loose_plan(contents, config, parse):
        writer.calls += 1
        assert len(contents) == 4
        return parse(json.dumps({"text": "Комикс", "cards": [
            {"notes": ["В начале XX века открылся первый зал.", "Уцелел фасад.", "История продолжилась."],
             "fact_ids": ["fact-2"]},  # no title/CTA, strongest not first, no arrows or hook
            {"notes": ["Городская хроника"], "arrows": [], "fact_ids": ["fact-1"]},
            {"notes": ["До новых встреч!"], "arrows": [""], "fact_ids": []},
        ]}, ensure_ascii=False), "writer")
    writer._lite_chain = loose_plan
    snapshot = {
        "comic": {"photo_ids": ["source", *ids]}, "place_name": "Театр",
        "place_selection": {}, "selected_facts": [
            {"fact_id": "fact-1", "text": "Открыт театр."},
            {"fact_id": "fact-2", "text": "Сохранился фасад."}],
        "strongest_fact": "fact-1"}
    planned = await svc._plan_comic(sid, snapshot)
    assert writer.calls == 1
    cover = planned["cards"][0]
    assert len(cover["notes"]) == len(cover["arrows"]) == 3
    assert cover["notes"][0].startswith("Театр\n")
    assert cover["notes"][-1].endswith("\nлистай →")
    assert cover["fact_ids"] == ["fact-2"]  # strongest is advice, never a rejection
    assert planned["cards"][-1] == {"notes": ["История за поворотом Калининград", "Театр"],
                                    "arrows": ["", ""], "fact_ids": []}


@pytest.mark.asyncio
async def test_comic_footer_uses_known_city_without_inventing_address(tmp_path):
    svc, sid, writer, _ = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    async def short_plan(contents, config, parse):
        writer.calls += 1
        return parse(json.dumps({"text": "Комикс", "cards": [
            {"notes": ["Театр", "Репетиции шли здесь.", "листай →"], "fact_ids": []},
            {"notes": ["Архитектурная деталь"], "fact_ids": []},
            {"notes": ["Писательский финал"], "fact_ids": []}
        ]}, ensure_ascii=False), "writer")
    writer._lite_chain = short_plan
    planned = await svc._plan_comic(sid, {"comic": {"photo_ids": ["source", *ids]},
        "place_name": "Театр", "place_selection": {"city": "Пионерский"},
        "selected_facts": [], "strongest_fact": None})
    assert planned["cards"][-1]["notes"] == [
        "История за поворотом Калининград", "Пионерский", "Театр"]


def test_text_refinement_during_caption_job_rejects_without_losing_instruction(tmp_path):
    svc, sid, _, _ = make_runtime(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='Подпись' WHERE id=?", (sid,))
    first = {"instruction": "Напиши основную подпись"}
    next_text = {"instruction": "Сократи подпись Telegram до 1000 символов"}
    svc.mutate_refinement(sid, "first-caption", first)
    with pytest.raises(ConflictError) as exc:
        svc.mutate_refinement(sid, "later-caption", next_text)
    assert exc.value.code == "caption_busy"
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE story_id=? AND kind='caption'", (sid,)).fetchone()[0] == 1
        assert db.execute("SELECT key FROM idempotency WHERE key='later-caption'").fetchone() is None
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='done' WHERE story_id=? AND kind='caption'", (sid,))
    svc.mutate_refinement(sid, "later-caption", next_text)
    with svc.store.connection() as db:
        jobs = list(db.execute("SELECT state,payload_json FROM jobs WHERE story_id=? AND kind='caption' ORDER BY rowid", (sid,)))
    assert len(jobs) == 2
    assert jobs[-1]["state"] == "ready"
    assert json.loads(jobs[-1]["payload_json"])["args"]["writing_request"] == next_text["instruction"]


def test_empty_http_publish_body_uses_same_native_slots_and_assets(tmp_path):
    base, _adapter, session, _ = live_ready(tmp_path)
    sid = session.resource_id
    svc = RuntimeStreetStoryService(base.settings, base.providers)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='ready_to_publish',visual_context_json=?,"
                   "vibepublish_asset_ref='album-cover',processed_image_url='/v1/stories/%s/cards/0' WHERE id=?" % sid,
                   (canonical({"comic": {"photo_ids": ["source"],
                      "cards": [{"photo_id": "source", "asset_ref": "album-cover"}]},
                      "photo_format": {}, "content_revision": "album", "stale": False}), sid))
    app = create_app(base.settings, svc)
    with TestClient(app) as client:
        result = client.post(f"/v1/stories/{sid}/publish",
            headers={"Authorization": "Bearer " + reveal(base.settings.device_token),
                     "Idempotency-Key": "album-button-publish"},
            content=b"")
    assert result.status_code == 200, result.text
    with svc.store.connection() as db:
        rows = [json.loads(row["request_json"]) for row in db.execute(
            "SELECT request_json FROM publish_intents WHERE story_id=? ORDER BY rowid", (sid,))]
    assert [row["destinations"] for row in rows] == [[DEFAULT_DESTINATIONS[0]], [DEFAULT_DESTINATIONS[1]]]
    assert all(row["asset_refs"] == ["album-cover"] for row in rows)



def test_instruction_only_refinement_uses_existing_caption_job_without_voice(tmp_path):
    svc, sid, _writer, _vp = make_runtime(tmp_path)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET draft_text=? WHERE id=?", ("Подпись на 1500 знаков. " * 80, sid))
    body = {"instruction": "Сократи подпись Telegram до 1000 символов, сохрани факты и ссылки"}
    response = svc.mutate_refinement(sid, "shorten-1000", body)
    assert response["research_pending"] is not None
    with svc.store.connection() as db:
        jobs = list(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='caption'", (sid,)))
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload_json"])["args"] == {"writing_request": body["instruction"]}
    svc.mutate_refinement(sid, "shorten-1000", body)
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE story_id=? AND kind='caption'",
                          (sid,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_eight_photo_dispatch_never_exceeds_three_in_one_batch(tmp_path):
    from street_story.errors import RetryableProviderError
    svc, sid, _writer, vp = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    ids += [str(uuid.uuid4()) for _ in range(5)]
    for i, photo_id in enumerate(ids[2:]):
        svc.add_story_photo(sid, "extra-" + str(i), photo_id, shot("yellow"), "image/jpeg")
    all_ids = ["source", *ids]
    svc.order_story_photos(sid, "order-eight", {"photo_ids": all_ids})
    snapshot = {"comic": {"photo_ids": all_ids, "cards": [{"photo_id": photo_id} for photo_id in all_ids],
                           "plan": {"cards": [
                               {"notes": ["История дома"], "arrows": [""], "fact_ids": []}
                               for _ in all_ids]}},
                "selected_facts": [], "place_name": "Тестовый дом",
                "photo_format": {}, "content_revision": "eight-1", "stale": False}
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing',visual_context_json=? WHERE id=?",
                   (canonical(snapshot), sid))
        svc._enqueue_job(db, sid, "visual", "visual:test-eight",
                         {"content_revision": "eight-1", "selected_fact_ids": []})
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    for expected in (3, 6):
        with pytest.raises(RetryableProviderError):
            await svc._run_visual(job)
        assert len(vp.operations) == expected  # Existing VibePublish still owns its queue.
    await svc._run_visual(job)
    assert len(vp.operations) == 8
    assert len(svc.story(sid)["cards"]) == 8


@pytest.mark.asyncio
async def test_scheduled_telegram_album_media_change_keeps_native_assets_and_shows_owner_notice(tmp_path):
    svc, sid, _writer, vp = make_runtime(tmp_path)
    now = svc.store.now()
    original = {"destinations": [DEFAULT_DESTINATIONS[0]], "text": "Подпись",
                "asset_ref": "old-cover", "asset_refs": ["old-cover", "old-finale"],
                "scheduled_for": "2026-11-03T09:10:00+02:00"}
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='scheduled',visual_context_json=? WHERE id=?",
                   (canonical({"comic": {"cards": [
                       {"asset_ref": "new-cover"}, {"asset_ref": "new-finale"}]}}), sid))
        db.execute("INSERT INTO publish_intents(id,story_id,request_key,vibepublish_request_key,"
                   "request_json,state,scheduled_for,created_at,updated_at,"
                   "vibepublish_publication_id,vibepublish_revision,receipt_json) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("intent-album", sid, "button", "vp-album", canonical(original),
                    "scheduled", original["scheduled_for"], now, now, "native-id", 1,
                    canonical({"state": "scheduled"})))
        svc._queue_publication_edits(db, sid, text="Подпись",
                                     asset_ref="new-cover", change_id="regenerated")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='publication_edit'", ()).fetchone())
    # The fake intentionally has no publication_update: attempting media-edit would fail.
    await svc._run_publication_edit(job)
    with svc.store.connection() as db:
        row = svc._story_row(db, sid)
        req = json.loads(db.execute("SELECT request_json FROM publish_intents WHERE id='intent-album'").fetchone()[0])
    assert row["error_code"] == "telegram_album_edit_needs_review"
    assert row["error_message"] == ("Картинки запланированного альбома не обновляются. "
                                    "Отмените публикацию и опубликуйте заново.")
    assert req["asset_refs"] == original["asset_refs"]


def test_comic_card_reserves_brief_capacity_for_cover_and_final_instructions(tmp_path):
    """Live regression: optional editorial context must not crowd out comic labels."""
    svc, sid, _, _ = make_runtime(tmp_path)
    with svc.store.connection() as db:
        story = dict(svc._story_row(db, sid))
    context = {
        "comic": {"photo_ids": ["source", "other", "last"]},
        "place_name": "Хуфенская гимназия",
        "publication_concept": "Городская история с проверенными фактами. " * 100,
        "selected_facts": [],
    }
    plans = (
        {"notes": ["Хуфенская гимназия", "1915 год: открылось здание.", "листай →"],
         "arrows": ["", "", ""], "fact_ids": []},
        {"notes": ["История за поворотом Калининград", "Калининград", "Зоологическая улица, 2"],
         "arrows": ["", "", ""], "fact_ids": []},
    )
    for position, plan in ((0, plans[0]), (2, plans[1])):
        brief, _data, _mime, _transform = svc._comic_brief(
            story, context, position, {"photo_id": "source"}, plan)
        assert len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT
        assert ("листай →" if position == 0 else "Это ФИНАЛ комикса") in brief
        assert "Городская история" in brief
