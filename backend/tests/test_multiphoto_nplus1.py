"""N photo plots remain distinct from a separately rendered final card (#325)."""
import json

import pytest

from street_story.live import StreetStoryLiveAdapter, ensure_live_schema
from street_story.service import canonical
from test_multiphoto import make_runtime, upload_three, shot


@pytest.mark.asyncio
async def test_two_photo_plots_plus_independent_final_with_edited_cover_title(tmp_path):
    svc, sid, writer, vp = make_runtime(tmp_path)
    extra_ids = upload_three(svc, sid)
    photos = ["source", extra_ids[0]]
    svc.order_story_photos(sid, "two-selected", {"photo_ids": photos})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET place_name='Вилла Зее-Адлер' WHERE id=?", (sid,))
    ensure_live_schema(svc)
    editor = StreetStoryLiveAdapter(svc, lambda *_: None, lambda *_: None)
    editor._edit_text(sid, "default-cover-text", {
        "expected_text_revision": 0, "new_text": "Черновик про историю виллы",
        "change_summary": "Надписи обложки по умолчанию",
        "card_notes": [
            {"position": 0, "notes": ["В 1910 году здесь построили дом", "В 1920 году дом обновили",
                                      "листай →"],
             "arrows": ["", "", ""]},
            {"position": 1, "notes": ["В 1930 году в доме открыли пансион"], "arrows": [""]},
        ]})
    default_preview = svc.story(sid)
    assert default_preview["card_notes"][0]["notes"] == [
        "Вилла Зее-Адлер", "В 1910 году здесь построили дом", "В 1920 году дом обновили", "Листай →"]
    assert default_preview["card_notes"][1]["notes"] == ["В 1930 году в доме открыли пансион", "Листай →"]
    from street_story.live import FUNCTIONS
    schema = next(tool for tool in FUNCTIONS if tool["name"] == "edit_text")
    help_text = schema["parameters"]["properties"]["card_notes"]["description"]
    assert "including the last selected photo" in help_text
    assert "separate N+1" in help_text
    editor._set_concept(sid, "owner-cover-concept", {
        "concept": "Обложка называется «Морская вилла»; сохранить исходные сюжеты."})
    editor._edit_text(sid, "owner-cover-text", {
        "expected_text_revision": 1, "new_text": "История приморского дома",
        "change_summary": "Изменено название обложки", "cover_title": "Морская вилла",
        "card_notes": [
            {"position": 0, "notes": ["Морская вилла", "В 1910 году здесь построили дом", "листай →"],
             "arrows": ["", "", ""]},
            {"position": 1, "notes": ["В 1920 году здание обновили"], "arrows": [""]},
        ]})
    preview = svc.story(sid)
    assert len(preview["card_notes"]) == 2
    assert preview["card_notes"][0]["notes"] == [
        "Морская вилла", "В 1910 году здесь построили дом", "Листай →"]
    assert preview["card_notes"][1]["notes"] == ["В 1920 году здание обновили", "Листай →"]
    assert preview["cards"] == []

    # Keep the actual per-card VibePublish worker, operation selection and asset readback.
    plan = [{k: item[k] for k in ("notes", "arrows")} for item in preview["card_notes"]]
    context = {
        "comic": {"photo_ids": photos,
                  "cards": [{"photo_id": photo_id} for photo_id in [*photos, photos[-1]]],
                  "plan": {"cards": [*plan, {"notes": [], "arrows": []}]}},
        "selected_facts": [{"fact_id": "fact-1", "text": "Дом построен в 1910 году."}],
        "place_name": "Вилла Зее-Адлер",
        "place_selection": {"city": "Пионерский", "address": "Пионерский, улица Портовая, 3"},
        "publication_concept": "Обложка называется «Морская вилла»",
        "photo_format": {}, "content_revision": "n2-final-1", "stale": False}
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='visual_processing',visual_context_json=? WHERE id=?",
                   (canonical(context), sid))
        svc._enqueue_job(db, sid, "visual", "visual:n2-final",
                         {"content_revision": "n2-final-1", "selected_fact_ids": []})
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    await svc._run_visual(job)
    result = svc.story(sid)
    assert [card["position"] for card in result["cards"]] == [0, 1, 2]
    assert result["card_notes"][1]["notes"] == ["В 1920 году здание обновили", "Листай →"]
    assert len(vp.operations) == 3
    assert len({str(op["asset"]) for op in vp.operations.values()}) == 3
    with svc.store.connection() as db:
        saved = json.loads(svc._story_row(db, sid)["visual_context_json"])["comic"]["cards"]
    assert [card["photo_id"] for card in saved] == [*photos, photos[-1]]
    briefs = [cmd["brief"] for cmd in vp.commands if cmd["kind"] == "tune"]
    assert len(briefs) == 3
    assert "Морская вилла" in briefs[0] and "Листай →" in briefs[0]
    assert "Вилла Зее-Адлер" not in briefs[0]
    assert "В 1920 году здание обновили" in briefs[1]
    assert "Это ФИНАЛ" not in briefs[1]
    assert "Это ФИНАЛ" in briefs[2] and "Пионерский, улица Портовая, 3" in briefs[2]
    for position in range(3):
        data, mime = await svc.comic_card(sid, position)
        assert mime == "image/jpeg" and data == shot("blue")
    assert writer.calls == 0  # Captions were edited, not regenerated by imagegen.



@pytest.mark.parametrize("cover_change", [
    {
        "request": "Поменяй ЗАЧИН на «В 1910 году здесь построили дом», название оставь прежним",
        "summary": "Поменяй зачин на «В 1910 году здесь построили дом», название оставь прежним",
        "notes": ["В 1910 году здесь построили дом", "листай →"],
        "expected": ["Вилла Зее-Адлер", "В 1910 году здесь построили дом", "Листай →"],
    },
    {
        "request": "Назови обложку «Морская вилла», сохрани зачин",
        "summary": "Изменено название обложки", "cover_title": "Морская вилла",
        "notes": ["Морская вилла", "В 1910 году здесь построили дом", "листай →"],
        "expected": ["Морская вилла", "В 1910 году здесь построили дом", "Листай →"],
    },
])
def test_editor_distinguishes_cover_hook_from_direct_title_edit(tmp_path, cover_change):
    svc, sid, _writer, _vp = make_runtime(tmp_path)
    photos = ["source", upload_three(svc, sid)[0]]
    svc.order_story_photos(sid, "editor-choose-two", {"photo_ids": photos})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET place_name='Вилла Зее-Адлер' WHERE id=?", (sid,))
    ensure_live_schema(svc)
    editor = StreetStoryLiveAdapter(svc, lambda *_: None, lambda *_: None)
    editor._edit_text(sid, "direct-cover-edit", {
        "expected_text_revision": 0, "new_text": "История здания",
        "writing_request": cover_change["request"],
        "change_summary": cover_change["summary"],
        **({"cover_title": cover_change["cover_title"]} if "cover_title" in cover_change else {}),
        "card_notes": [
            {"position": 0, "notes": cover_change["notes"],
             "arrows": [""] * len(cover_change["notes"])},
            {"position": 1, "notes": ["В 1930 году вилла стала пансионом"], "arrows": [""]},
        ],
    })
    preview = svc.story(sid)
    assert preview["card_notes"][0]["notes"] == cover_change["expected"]
    assert preview["card_notes"][1]["notes"] == ["В 1930 году вилла стала пансионом", "Листай →"]
    assert preview["cards"] == []
    with svc.store.connection() as db:
        story = dict(svc._story_row(db, sid))
    context = {
        "comic": {"photo_ids": photos},
        "place_name": "Вилла Зее-Адлер",
        "publication_concept": "",
        "selected_facts": [],
        "place_selection": {"city": "Пионерский", "address": "Пионерский, улица Тестовая, 3"},
    }
    cover, *_ = svc._comic_brief(story, context, 0, {"photo_id": photos[0]},
                                 preview["card_notes"][0])
    last_photo, *_ = svc._comic_brief(story, context, 1, {"photo_id": photos[1]},
                                      preview["card_notes"][1])
    final, *_ = svc._comic_brief(story, context, 2, {"photo_id": photos[1]},
                                 {"notes": [], "arrows": []})
    title = cover_change["expected"][0]
    assert "главное название «" + title + "» — КРУПНО" in cover
    assert "В 1910 году здесь построили дом" in cover and "Листай →" in cover
    if title != "Вилла Зее-Адлер":
        assert "Вилла Зее-Адлер" not in cover
    assert "В 1930 году вилла стала пансионом" in last_photo
    assert "Это ФИНАЛ" not in last_photo
    assert "Это ФИНАЛ" in final and "История за поворотом" in final
    editor._edit_text(sid, "hook-after-cover-edit", {
        "expected_text_revision": 1, "new_text": "Новый зачин",
        "change_summary": "Изменён только зачин",
        "card_notes": [
            {"position": 0, "notes": ["В 1920 году обновили виллу", "листай →"],
             "arrows": ["", ""]},
            {"position": 1, "notes": ["В 1930 году вилла стала пансионом"], "arrows": [""]},
        ],
    })
    updated = svc.story(sid)
    assert updated["card_notes"][0]["notes"] == [
        title, "В 1920 году обновили виллу", "Листай →"]
    updated_cover, *_ = svc._comic_brief(story, context, 0, {"photo_id": photos[0]},
                                        updated["card_notes"][0])
    assert "главное название «" + title + "» — КРУПНО" in updated_cover
    assert "В 1920 году обновили виллу" in updated_cover


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_count", [2, 8])
async def test_real_visual_request_appends_one_card_without_extra_writer_notes(tmp_path, monkeypatch, photo_count):
    from street_story import fact_ledger
    import uuid

    svc, sid, writer, _vp = make_runtime(tmp_path)
    extras = upload_three(svc, sid)
    extras.extend(str(uuid.uuid4()) for _ in range(photo_count - 3 if photo_count > 2 else 0))
    for i, photo_id in enumerate(extras[2:]):
        svc.add_story_photo(sid, "additional-" + str(i), photo_id, shot("yellow"), "image/jpeg")
    photo_ids = ["source", *extras[:photo_count - 1]]
    svc.order_story_photos(sid, "choose-" + str(photo_count), {"photo_ids": photo_ids})
    note_cards = [{"notes": ["История на выбранном фото"], "arrows": [""]}
                  for _ in photo_ids]
    note_cards[0] = {"notes": ["Настоящее имя POI", "Начало истории", "листай →"],
                     "arrows": ["", "", ""]}
    monkeypatch.setattr(fact_ledger, "eligibility_issues_for_ids", lambda *_: [])
    monkeypatch.setattr(fact_ledger, "fact_revision_bundle", lambda *_: {"fact-1": "revision-one"})
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET place_name='Настоящее имя POI',research_json=? WHERE id=?",
                   (canonical({"caption_image_notes": {
                       "photo_ids": photo_ids, "fact_ids": ["fact-1"], "cards": note_cards}}), sid))
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                   "VALUES(?, 'fact-1', 'Подтверждённое наблюдение', 0.9, 1, 1, '[]')", (sid,))
    await svc.request_visual(sid, "visual-" + str(photo_count),
                             {"selected_fact_ids": ["fact-1"]})
    with svc.store.connection() as db:
        context = json.loads(svc._story_row(db, sid)["visual_context_json"])
    assert len(context["comic"]["cards"]) == photo_count + 1
    assert len(context["comic"]["plan"]["cards"]) == photo_count + 1
    assert context["comic"]["photo_ids"] == photo_ids
    assert [card["photo_id"] for card in context["comic"]["cards"]] == [
        *photo_ids, photo_ids[-1]]
    assert context["comic"]["plan"]["cards"][:photo_count] == note_cards
    assert context["comic"]["plan"]["cards"][-1] == {"notes": [], "arrows": []}
    assert writer.calls == 0
