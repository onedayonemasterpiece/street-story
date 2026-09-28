from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from street_story.config import Settings
from street_story.live import FUNCTIONS, StreetStoryLiveAdapter, ensure_live_schema
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.providers import GroundedResearch
from street_story.service import ConflictError, ProviderBundle


def test_live_initialization_declares_application_search_function(tmp_path) -> None:
    svc, adapter, session, _events = make_service(tmp_path)
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model="gemini-3.8-live")
    configuration = initialized["configuration"]
    assert configuration["search_enabled"] is True
    assert configuration["manual_activity_detection"] is True
    assert configuration["application_search_function"] == "search_web"
    assert any(item["name"] == "search_web" for item in configuration["functions"])


def test_live_functions_expose_search_tool_not_async_research_job() -> None:
    names = [item["name"] for item in FUNCTIONS]
    assert "search_web" in names
    assert "start_research" not in names


PHOTO = b"live-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class FakeOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {}, "nearby": []}


class FakeWiki:
    async def nearby(self, lat, lon):
        return []


class FakeGemini:
    def __init__(self):
        self.searches = []

    async def search_web(self, query, topic_context):
        self.searches.append((query, topic_context))
        return GroundedResearch(
            payload={
                "summary": "Найдено два проверяемых факта.",
                "facts": [
                    {
                        "text": "Бранденбургские ворота находятся в Калининграде.",
                        "confidence": 0.98,
                        "source_urls": ["https://example.com/brandenburg"],
                    },
                    {
                        "text": "Неподтверждённый факт не должен стать доказанным.",
                        "confidence": 0.4,
                        "source_urls": ["https://not-grounded.example/"],
                    },
                ],
            },
            grounding_sources=[
                {
                    "type": "web",
                    "title": "Brandenburg source",
                    "url": "https://example.com/brandenburg",
                }
            ],
        )


class FakeVP:
    async def bootstrap(self):
        return {
            "routing_revision": 1,
            "destinations": [
                {"alias": "tg-safe", "kind": "destination", "label": "Test Telegram", "provider": "telegram"}
            ],
            "capabilities": [
                {
                    "destination": "tg-safe",
                    "operation": "publish",
                    "surface": "post",
                    "provider": "telegram",
                    "status": "supported",
                }
            ],
        }


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="key",
        gemini_model="gemini-3.5-flash-lite",
        vibepublish_base_url="https://vp.test",
        vibepublish_bearer_token="vp",
        osm_user_agent="Street Story Live tests",
    )


def make_service(tmp_path: Path):
    svc = MvpLocationStreetStoryService(
        settings(tmp_path), ProviderBundle(FakeOSM(), FakeWiki(), FakeGemini(), FakeVP())
    )
    story = svc.create_story(
        key="create",
        client_story_id="live-topic",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.7,
        lon=20.5,
    )
    events = []
    adapter = StreetStoryLiveAdapter(svc, lambda _session, event: events.append(event))
    ensure_live_schema(svc)
    session = SimpleNamespace(
        resource_id=story["id"],
        state={"recent_user": __import__("collections").deque(maxlen=24), "recent_model": __import__("collections").deque(maxlen=16), "literal": None},
    )
    return svc, adapter, session, events


@pytest.mark.asyncio
async def test_live_web_search_stays_in_session_and_does_not_rewrite_draft(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    story_id = session.resource_id
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='Авторский текст' WHERE id=?", (story_id,))

    adapter.on_event(
        session,
        {"type": "input_transcript", "text": "Я снимаю Бранденбургские ворота в Калининграде"},
    )
    result = await adapter.execute_tool(
        session,
        {
            "name": "search_web",
            "id": "search-1",
            "args": {"query": "Бранденбургские ворота Калининград история"},
        },
    )

    assert result["summary"] == "Найдено два проверяемых факта."
    assert len(result["sources"]) == 1
    assert result["facts"][0]["evidence_supported"] is True
    assert result["facts"][1]["evidence_supported"] is False
    assert svc.providers.gemini.searches[0][0] == "Бранденбургские ворота Калининград история"
    assert "Я снимаю Бранденбургские ворота" in svc.providers.gemini.searches[0][1]["recent_author_context"]

    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE story_id=? AND kind='research'", (story_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM voice_sessions").fetchone()[0] == 0
        rows = list(db.execute("SELECT text,evidence_supported,sources_json FROM facts WHERE story_id=? ORDER BY rowid", (story_id,)))
        story = db.execute("SELECT state,draft_text,research_json FROM stories WHERE id=?", (story_id,)).fetchone()
    assert len(rows) == 2
    assert rows[0]["evidence_supported"] == 1
    assert "example.com/brandenburg" in rows[0]["sources_json"]
    assert rows[1]["evidence_supported"] == 0
    assert story["draft_text"] == "Авторский текст"
    assert story["state"] != "researching"
    research = __import__("json").loads(story["research_json"])
    assert research["live_web_searches"][-1]["query"] == "Бранденбургские ворота Калининград история"
    assert any(event.get("type") == "product_state" for event in events)


@pytest.mark.asyncio
async def test_literal_span_is_protected_and_undo_restores_previous_text(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    story_id = session.resource_id
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='Остальной текст' WHERE id=?", (story_id,))
    await adapter.execute_tool(session, {"name": "literal_begin", "id": "begin", "args": {"position": "start"}})
    adapter.on_event(session, {"type": "input_transcript", "text": "Я люблю этот город"})
    adapter.on_event(session, {"type": "input_transcript", "text": "Завершить диктовку"})
    literal = await adapter.execute_tool(session, {"name": "literal_finish", "id": "literal-1", "args": {}})
    assert literal["draft_text"].startswith("Я люблю этот город")
    assert any(event.get("type") == "literal_mode" and not event.get("active") for event in events)

    with pytest.raises(ConflictError) as protected:
        await adapter.execute_tool(
            session,
            {
                "name": "edit_text",
                "id": "edit-bad",
                "args": {
                    "expected_text_revision": literal["text_revision"],
                    "new_text": "Переписал всё",
                    "change_summary": "Сократил",
                },
            },
        )
    assert protected.value.code == "literal_span_protected"

    edited = await adapter.execute_tool(
        session,
        {
            "name": "edit_text",
            "id": "edit-ok",
            "args": {
                "expected_text_revision": literal["text_revision"],
                "new_text": "Я люблю этот город\n\nКороткий остальной текст",
                "change_summary": "Сократил остальное",
            },
        },
    )
    assert edited["draft_text"].startswith("Я люблю этот город")
    undone = await adapter.execute_tool(session, {"name": "undo", "id": "undo-1", "args": {}})
    assert undone["draft_text"] == literal["draft_text"]
    assert undone["literal_spans"]


@pytest.mark.asyncio
async def test_live_fact_selection_does_not_overwrite_edited_draft(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    with svc.store.tx() as db:
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,?,?,?,?,?)",
            (story_id, "f1", "Проверенный факт", 0.9, 1, 1, "[]"),
        )
        db.execute("UPDATE stories SET draft_text='Авторский текст' WHERE id=?", (story_id,))
    result = await adapter.execute_tool(
        session, {"name": "select_facts", "id": "facts-1", "args": {"fact_ids": ["f1"]}}
    )
    assert result["story"]["draft_text"] == "Авторский текст"


@pytest.mark.asyncio
async def test_publication_confirmation_binds_exact_text_and_visual(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    with svc.store.tx() as db:
        db.execute(
            "UPDATE stories SET state='ready_to_publish',draft_text='Готовый текст',"
            "vibepublish_asset_ref='asset-1',processed_image_url='/v1/assets/x',"
            "visual_context_json=? WHERE id=?",
            ('{"content_revision":"visual-r1","stale":false}', story_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO live_editor_state(story_id,text_revision,literal_json,history_json,updated_at) VALUES(?,3,'[]','[]',?)",
            (story_id, svc.store.now()),
        )
    scheduled_for = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).astimezone(timezone(timedelta(hours=2))).isoformat(timespec="seconds")
    prepared = await adapter.execute_tool(
        session,
        {
            "name": "prepare_publication",
            "id": "prepare-1",
            "args": {
                "destinations": ["tg-safe"],
                "scheduled_for": scheduled_for,
                "timezone": "Europe/Kaliningrad",
            },
        },
    )
    card = prepared["confirmation"]
    assert card["text"] == "Готовый текст"
    assert card["visual_revision"] == "visual-r1"

    with svc.store.tx() as db:
        db.execute("UPDATE stories SET draft_text='Невидимая новая версия' WHERE id=?", (story_id,))
        db.execute("UPDATE live_editor_state SET text_revision=4 WHERE story_id=?", (story_id,))
    with pytest.raises(ConflictError) as stale:
        await adapter.execute_tool(
            session,
            {
                "name": "confirm_publication",
                "id": "confirm-1",
                "args": {"confirmation_id": card["confirmation_id"]},
            },
        )
    assert stale.value.code == "publication_confirmation_stale"
