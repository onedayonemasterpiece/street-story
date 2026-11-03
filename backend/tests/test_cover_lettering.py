"""Cover lettering through the existing writer, editor, plan and image brief contracts."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from street_story.live import StreetStoryLiveAdapter, ensure_live_schema
from street_story.providers import GeminiClient
from test_multiphoto import make_runtime, upload_three
from test_caption_writer import Route


@pytest.mark.asyncio
async def test_cover_names_hook_and_cta_survive_writer_editor_and_render(tmp_path):
    main = "Вилла бургомистра Раушена"
    alt = "Дом звездочёта"
    hook = "Дом получил башню для наблюдения за звёздами"
    facts = [{"text": name, "source_url": "https://history.example/villa"}
             for name in (main, alt, "вилла", hook)]
    # Fixture of the existing model JSON contract: the model chooses alt, excluding
    # the semantic duplicate "вилла". No Python name extraction/deduplication.
    response = {"text": "История дома.", "cover_title": main, "card_notes": [
        {"notes": [main, hook, alt], "arrows": ["", "", ""]},
        {"notes": ["Башня для наблюдений"], "arrows": ["башня"]},
        {"notes": ["История здания"], "arrows": [""]}]}
    prompts = []

    async def generate(_key, _timeout, contents, config, **kwargs):
        prompts.append(contents[0])
        assert json.loads(contents[0].rsplit("\n\n", 1)[-1])["facts"] == facts
        return SimpleNamespace(text=json.dumps(response, ensure_ascii=False))

    client = SimpleNamespace(WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, _generate=generate,
                             writer_routes=[('fixture', None, None, Route(None))])
    written = await GeminiClient.write_caption(
        client, owner_request=f"На обложке {main}, сохрани зачин", current_text="", concept="", place=main,
        place_name=alt, facts=facts, literal_fragments=[], photo_jpeg=None, photo_count=3, lead_fact=hook)
    assert written["card_notes"][0]["notes"] == [main, hook, alt, "Листай →"]
    assert "смысловые дубли" in prompts[0]
    assert "меньшим шрифтом" in prompts[0]

    svc, sid, _, _ = make_runtime(tmp_path)
    ids = ["source", *upload_three(svc, sid)]
    svc.order_story_photos(sid, "order", {"photo_ids": ids})
    ensure_live_schema(svc)
    adapter = StreetStoryLiveAdapter(svc, lambda *_: None, lambda *_: None)
    edited_hook = "Башня открыла этому дому звёздное небо"
    written["card_notes"][0]["notes"][1] = edited_hook
    adapter._edit_text(sid, "editor", {"expected_text_revision": 0,
        "new_text": "Редакторский текст поста", "change_summary": "Уточнён зачин",
        "cover_title": main}, card_notes=written["card_notes"])
    saved = svc.story(sid)
    assert saved["draft_text"].startswith("Редакторский текст поста")
    assert saved["card_notes"][0]["notes"] == [main, edited_hook, alt, "Листай →"]
    with svc.store.connection() as db:
        story = dict(svc._story_row(db, sid))
    context = {"comic": {"photo_ids": ids}, "selected_facts": facts,
               "place_name": main, "place_selection": {"city": "Светлогорск"}}
    plans = [*saved["card_notes"], {"notes": [], "arrows": []}]
    images = []
    for pos, plan in enumerate(plans):
        brief, data, mime, _ = svc._comic_brief(
            story, context, pos, {"photo_id": ids[min(pos, 2)]}, plan)
        labels = plan["notes"] if pos < 3 else ["История за поворотом", "Светлогорск", main]
        if pos < 3:
            assert labels[-1] == "Листай →"
            assert "«Листай →» — без стрелки" in brief
        else:
            assert "Листай →" not in brief
        if pos == 0:
            assert all(label in brief for label in (main, alt, edited_hook))
            assert "меньшим шрифтом" in brief
        # Actual local bitmap fixture, not a claim about paid ImageGen quality.
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image = image.resize((900, round(900 * image.height / image.width)))
        draw = ImageDraw.Draw(image)
        y = 30
        for i, label in enumerate(labels):
            size = 34 if pos == 0 and i == 0 else 20 if pos == 0 and i >= 2 else 26
            font = ImageFont.truetype("DejaVuSans.ttf", size)
            box = draw.textbbox((20, y), label, font=font)
            assert box[2] < image.width and box[3] < image.height
            draw.text((20, y), label, font=font, fill="black", stroke_width=1, stroke_fill="white")
            y += 90
        images.append(image)
        output = Path(os.environ.get("CHECK_OUTPUT", str(tmp_path)))
        image.save(output / f"cover-fixture-{pos}.png")
    assert len(images) == 4
