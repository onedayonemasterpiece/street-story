"""Mira conducts; a lite text model writes the caption, and any answering model in the chain will do."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient
from street_story.service import canonical

from test_live_editor import make_service, mark_identity_ready


def prepared(tmp_path):
    from test_map_links import tower
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, story_id)["research_json"] or "{}")
        research["place_selection"] = tower()["place_selection"]
        db.execute("UPDATE stories SET research_json=?,draft_text=? WHERE id=?",
                   (canonical(research), "Старый текст.\n\nКалининград, улица 1\n#Калининград", story_id))
        db.execute(
            "INSERT OR IGNORE INTO live_editor_state(story_id,text_revision,literal_json,history_json,updated_at) VALUES(?,0,'[]','[]',?)",
            (story_id, svc.store.now()),
        )
    return svc, adapter, session


@pytest.mark.asyncio
async def test_writing_request_is_composed_by_the_writer_and_gets_the_server_footer(tmp_path):
    svc, adapter, session = prepared(tmp_path)
    calls = []

    async def write_caption(**kwargs):
        calls.append(kwargs)
        return {"text": "Новая подпись о доме.\n\nУлица 1\n#Калининград", "model": "gemini-3.5-flash-lite"}

    svc.providers.gemini.write_caption = write_caption
    edited = await adapter.execute_tool(session, {"name": "edit_text", "id": "write-1", "args": {
        "expected_text_revision": 0, "change_summary": "Переписано", "writing_request": "Перепиши подпись."}})
    assert edited["draft_text"].startswith("Новая подпись о доме.\n\nУлица 1\n#Калининград\n\n[ЯндексКарты](")
    request = calls[0]
    assert request["owner_request"] == "Перепиши подпись."
    assert request["current_text"].startswith("Старый текст.")
    assert request["photo_jpeg"]  # the writer sees the photo to tell visible from unseen parts
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, session.resource_id)["research_json"])
    assert research["draft_composed_by"] == "writer:gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_unavailable_writer_leaves_the_text_and_tells_mira_to_write_it_herself(tmp_path):
    svc, adapter, session = prepared(tmp_path)

    async def write_caption(**kwargs):
        raise GeminiUnavailable(0, "all_caption_writer_models_unavailable")

    svc.providers.gemini.write_caption = write_caption
    with pytest.raises(Exception) as failure:
        await adapter.execute_tool(session, {"name": "edit_text", "id": "write-2", "args": {
            "expected_text_revision": 0, "change_summary": "Переписано", "writing_request": "Перепиши подпись."}})
    assert getattr(failure.value, "code", None) == "caption_writer_unavailable"
    with svc.store.connection() as db:
        assert svc._story_row(db, session.resource_id)["draft_text"].startswith("Старый текст.")
    exact = await adapter.execute_tool(session, {"name": "edit_text", "id": "write-3", "args": {
        "expected_text_revision": 0, "change_summary": "Сама", "new_text": "Текст Миры.\n\nУлица 1\n#Калининград"}})
    assert exact["draft_text"].startswith("Текст Миры.")


class Route:
    def __init__(self, outcome):
        self.outcome = outcome

    async def execute(self, _operation, call):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return await call("key", 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["unavailable", "unsupported", "malformed"])
async def test_writer_uses_the_next_lite_model_when_one_does_not_answer(first):
    from street_story.errors import PermanentProviderError
    answers = {"gemini-3.5-flash-lite": json.dumps({"text": "Подпись.\n\nУлица 1\n#Калининград"}),
               "gemini-3.1-flash-lite": "not json" if first == "malformed" else "{}"}

    async def generate(_key, _timeout, contents, _config, *, operation, model, quota):
        assert contents[0].startswith(GeminiClient.WRITER_RULES)
        return SimpleNamespace(text=answers[model])

    failing = {"unavailable": GeminiUnavailable(0, "busy"),
               "unsupported": PermanentProviderError("gemini:unsupported_model"),
               "malformed": None}[first]
    client = SimpleNamespace(_generate=generate, WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, writer_routes=[
        ("gemini-3.1-flash-lite", None, None, Route(failing) if failing else Route(None)),
        ("gemini-3.5-flash-lite", None, None, Route(None)),
    ])
    result = await GeminiClient.write_caption(client, owner_request="Перепиши", current_text="", concept="",
        facts=[{"text": "Дом построен в 1905 году."}], place={"city": "Калининград"},
        literal_fragments=[], photo_jpeg=None)
    assert result == {"text": "Подпись.\n\nУлица 1\n#Калининград", "model": "gemini-3.5-flash-lite"}


@pytest.mark.asyncio
async def test_writer_reports_unavailable_when_no_model_answers():
    async def generate(*_args, **_kwargs):
        raise AssertionError("no route admits a call")

    client = SimpleNamespace(_generate=generate, WRITER_RULES=GeminiClient.WRITER_RULES,
                             WRITER_SCHEMA=GeminiClient.WRITER_SCHEMA, writer_routes=[
        ("gemini-3.1-flash-lite", None, None, Route(GeminiUnavailable(123.0, "busy"))),
        ("gemini-3.6-flash-lite", None, None, Route(GeminiUnavailable(99.0, "busy"))),
    ])
    with pytest.raises(GeminiUnavailable):
        await GeminiClient.write_caption(client, owner_request="Перепиши", current_text="", concept="",
            facts=[], place={}, literal_fragments=[], photo_jpeg=None)
