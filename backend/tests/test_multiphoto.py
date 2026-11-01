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
        self.mutate_candidates = None
        self.pending_once = False

    async def bootstrap(self):
        return {"routing_revision": 3,
                "destinations": [{"kind": "destination", "alias": alias, "label": alias,
                                  "provider": network}
                                 for alias, network in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))],
                "capabilities": [{"destination": alias, "operation": "publish",
                                  "surface": "post", "provider": network, "status": "supported"}
                                 for alias, network in zip(DEFAULT_DESTINATIONS, ("telegram", "vk"))]}

    async def ingress_asset(self, data, mime, key):
        previous = next((item for item in self.ingresses if item["key"] == key), None)
        if previous:
            assert previous["source_bytes_sha"] == hashlib.sha256(data).hexdigest()
            return {"asset_id": previous["asset_id"], "sha256": previous["sha256"]}
        asset_id = "original-" + str(len(self.ingresses) + 1)
        sha = hashlib.sha256(data).hexdigest()
        self.ingresses.append({"key": key, "asset_id": asset_id, "sha256": sha,
                               "source_bytes_sha": sha})
        return {"asset_id": asset_id, "sha256": sha}

    async def visual(self, payload, key):
        command = payload["command"]
        self.commands.append(command)
        assert command["kind"] == "tune", "batch must not invoke legacy single-candidate select"
        previous = next((item for item in self.operations.values() if item["key"] == key), None)
        if previous:
            assert previous["command"] == command
            return {"operation_id": previous["id"], "state": "accepted"}
        op = "visual-" + str(len(self.operations) + 1)
        self.operations[op] = {"id": op, "key": key, "command": command}
        return {"operation_id": op, "state": "accepted"}

    async def status(self, operation):
        assert len(self.operations) == 1, "one imagegen operation for the entire album"
        if self.pending_once:
            self.pending_once = False
            return {"receipts": [{"operation_id": operation, "state": "processing"}]}
        command = self.operations[operation]["command"]
        assert len(command["sources"]) == command["candidates"] == len(command["formats"])
        choices = []
        for i, item in enumerate(command["sources"]):
            source_asset_ref = item["source"]["id"]
            source = next(item for item in self.ingresses if item["asset_id"] == source_asset_ref)
            fmt = command["formats"][i]
            choices.append({"id": f"choice-{i+1}", "asset_ref": f"card-{i+1}",
                "sha256": hashlib.sha256(f"card-{i+1}".encode()).hexdigest(),
                "source_index": i, "source_asset_ref": source_asset_ref,
                "source_sha256": source["sha256"],
                "width": 1080 if fmt == "post_4_5" else 40,
                "height": 1350 if fmt == "post_4_5" else 30,
                "format": fmt, "selection_token": "test-token"})
        if self.mutate_candidates:
            choices = self.mutate_candidates(choices)
        return {"receipts": [{"operation_id": operation, "state": "needs_selection",
                "visual_job_id": "job-album", "visual_revision": 1,
                "candidates": choices}]}

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
        "Светлогорск, улица Тестовая, 1"]
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
    assert [cmd["kind"] for cmd in vp.commands].count("tune") == 1
    assert len(vp.commands[0]["sources"]) == 3
    assert len(vp.operations) == 1
    assert writer.calls == 1
    with svc.store.connection() as db:
        visual = json.loads(svc._story_row(db, sid)["visual_context_json"])
        assert [p["asset_ref"] for p in visual["comic"]["cards"]] == ["card-1", "card-2", "card-3"]



@pytest.mark.asyncio
async def test_six_facts_fit_three_briefs_and_long_notes_fail_writer_validation(tmp_path):
    svc, sid, writer, _ = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    photo_ids = ["source", *ids]
    facts = [
        {"fact_id": f"fact-{i}", "text": text} for i, text in enumerate([
            "В 1944 и 1945 годах гимназия избежала серьёзных повреждений, несмотря на бомбардировки и штурм города.",
            "Здание возвели для Хуфенской гимназии, после открытия нового здания помещения использовали иначе.",
            "Окна старого актового зала гимназии украшало свинцовое декоративное остекление с видами города.",
            "Крутую крышу первоначального здания завершала высокая башня, увенчанная барочным куполом.",
            "Писатель Эрнст Вихерт преподавал в Хуфенской гимназии в Кёнигсберге в двадцатые годы.",
            "Новое здание Хуфенской гимназии открылось для учеников в 1915 году.",
        ], 1)
    ]
    context = {"comic": {"photo_ids": photo_ids}, "place_name": "Университетский колледж БФУ",
               "place_selection": {"address": "Калининград, Зоологическая улица, 2", "city": "Калининград"},
               "selected_facts": facts, "strongest_fact": "fact-1", "photo_format": {}}
    async def short_plan(contents, config, parse):
        writer.calls += 1
        assert "3–8 слов" in contents[0] and "12 слов" in contents[0]
        assert "«листай →» — отдельная подпись ТОЛЬКО обложки" in contents[0]
        cards = [
            {"notes": ["Университетский колледж БФУ", "Гимназия пережила бомбардировки и штурм.", "листай →"],
             "fact_ids": ["fact-1", "fact-2"]},
            {"notes": ["Здание строили для Хуфенской гимназии.", "Витражи зала показывали виды города.",
                       "Башню венчал барочный купол."], "fact_ids": ["fact-3", "fact-4", "fact-5", "fact-6"]},
            {"notes": ["До новых историй!"], "fact_ids": []},
        ]
        verbose = json.loads(json.dumps(cards, ensure_ascii=False))
        verbose[1]["notes"][0] = "Здание Хуфенской гимназии и его великолепная архитектурная история, подробно изложенная в этом очень длинном предложении."
        with pytest.raises(ValueError):
            parse(json.dumps({"text": "Слишком длинно", "cards": verbose}, ensure_ascii=False), "writer")
        cjk = json.loads(json.dumps(cards, ensure_ascii=False))
        cjk[1]["notes"][0] = "健康新 здание гимназии открылось в 1915 году"
        with pytest.raises(ValueError, match="selected evidence"):
            parse(json.dumps({"text": "Посторонние символы", "cards": cjk}, ensure_ascii=False), "writer")
        cta = json.loads(json.dumps(cards, ensure_ascii=False))
        cta[1]["notes"][0] += "\nлистай →"
        cta[1]["notes"][-1] = "листай →"
        sanitized = parse(json.dumps({"text": "CTA только на обложке", "cards": cta}, ensure_ascii=False), "writer")
        assert len(sanitized["cards"][1]["notes"]) == 2
        assert sanitized["cards"][1]["notes"][0] == cards[1]["notes"][0]
        assert all("листай" not in note for note in sanitized["cards"][1]["notes"])
        assert all("листай" not in note for note in sanitized["cards"][2]["notes"])
        return parse(json.dumps({"text": "Короткие надписи", "cards": cards}, ensure_ascii=False), "writer")
    writer._lite_chain = short_plan
    plan = await svc._plan_comic(sid, context)
    assert writer.calls == 1 and len(plan["cards"]) == 3
    with svc.store.connection() as db:
        story = dict(svc._story_row(db, sid))
    for position, card in enumerate(plan["cards"]):
        brief, *_ = svc._comic_brief(story, context, position,
                                    {"photo_id": photo_ids[position]}, card)
        assert len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT
    assert plan["cards"][0]["notes"][-1] == "листай →"
    assert plan["cards"][-1]["notes"] == ["История за поворотом Калининград", "Калининград, Зоологическая улица, 2"]


@pytest.mark.asyncio
async def test_failed_comic_job_exits_visual_processing_with_error(tmp_path):
    from street_story.errors import PermanentProviderError
    svc, sid, _, _ = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    svc.order_story_photos(sid, "order-failed", {"photo_ids": ["source", *ids]})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing' WHERE id=?", (sid,))
        svc._enqueue_job(db, sid, "visual", "visual:broken-card", {"content_revision": "broken"})

    async def failed_visual(_job):
        raise PermanentProviderError("visual_prompt_too_long")

    svc._run_visual = failed_visual
    assert await svc.run_once(claim_kind="visual", claim_story_id=sid)
    with svc.store.connection() as db:
        story = svc._story_row(db, sid)
        job = db.execute("SELECT state FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone()
    assert job["state"] == "failed"
    assert story["state"] == "needs_review"
    assert story["error_code"] == "visual_generation_failed"
    assert "карточки" in story["error_message"]


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
async def test_eight_photo_dispatch_uses_one_luna_task(tmp_path):
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
    await svc._run_visual(job)
    assert len(vp.operations) == 1
    assert len(vp.commands) == 1 and vp.commands[0]["candidates"] == 8
    assert len(vp.commands[0]["sources"]) == 8
    assert len(svc.story(sid)["cards"]) == 8



def setup_batch_visual(svc, sid, photo_ids, key):
    snapshot = {"comic": {"photo_ids": photo_ids,
                          "cards": [{"photo_id": photo_id} for photo_id in photo_ids],
                          "plan": {"cards": [{"notes": ["История здания"], "arrows": [""],
                                               "fact_ids": []} for _ in photo_ids]}},
                "selected_facts": [], "place_name": "Тестовый дом",
                "photo_format": {}, "content_revision": key, "stale": False}
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing',visual_context_json=? WHERE id=?",
                   (canonical(snapshot), sid))
        svc._enqueue_job(db, sid, "visual", "visual:" + key,
                         {"content_revision": key, "selected_fact_ids": []})
        return dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'",
                               (sid,)).fetchone())


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "reorder", "source_hash", "source_ref",
                                    "duplicate_asset", "duplicate_output_hash",
                                    "wrong_format", "dimensions"])
async def test_batch_rejects_any_ambiguous_candidate_before_saving_a_card(tmp_path, fault):
    from street_story.errors import PermanentProviderError
    svc, sid, _writer, vp = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    photo_ids = ["source", *ids]
    svc.order_story_photos(sid, "order-fault", {"photo_ids": photo_ids})
    job = setup_batch_visual(svc, sid, photo_ids, "batch-fault")

    def corrupt(candidates):
        if fault == "missing":
            return candidates[:-1]
        if fault == "reorder":
            return list(reversed(candidates))
        if fault == "source_hash":
            candidates[1]["source_sha256"] = "0" * 64
        elif fault == "source_ref":
            candidates[0]["source_asset_ref"] = "asset-foreign"
        elif fault == "duplicate_asset":
            candidates[2]["asset_ref"] = candidates[0]["asset_ref"]
        elif fault == "duplicate_output_hash":
            candidates[1]["sha256"] = candidates[0]["sha256"]
        elif fault == "wrong_format":
            candidates[0]["format"] = "story_9_16"
        elif fault == "dimensions":
            candidates[2]["width"] = 1
        return candidates

    vp.mutate_candidates = corrupt
    with pytest.raises(PermanentProviderError):
        await svc._run_visual(job)
    assert len(vp.operations) == 1 and len(vp.commands) == 1
    with svc.store.connection() as db:
        context = json.loads(svc._story_row(db, sid)["visual_context_json"])
    assert context["comic"]["batch_operation_id"] == "visual-1"
    assert not any(card.get("asset_ref") for card in context["comic"]["cards"])


@pytest.mark.asyncio
async def test_batch_pending_status_retries_same_operation_without_second_tune(tmp_path):
    from street_story.errors import RetryableProviderError
    svc, sid, _writer, vp = make_runtime(tmp_path)
    ids = upload_three(svc, sid)
    photo_ids = ["source", *ids]
    svc.order_story_photos(sid, "order-pending", {"photo_ids": photo_ids})
    job = setup_batch_visual(svc, sid, photo_ids, "batch-pending")
    vp.pending_once = True
    with pytest.raises(RetryableProviderError):
        await svc._run_visual(job)
    assert len(vp.operations) == 1
    await svc._run_visual(job)
    assert len(vp.operations) == 1 and len(vp.commands) == 1
    assert len(svc.story(sid)["cards"]) == 3

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
