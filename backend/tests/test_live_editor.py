from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from street_story.config import Settings
from street_story.live import StreetStoryLiveAdapter, ensure_live_schema
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.service import ConflictError, ProviderBundle


PHOTO = b"live-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class FakeOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {}, "nearby": []}


class FakeWiki:
    async def nearby(self, lat, lon):
        return []


class FakeGemini:
    pass


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
async def test_live_research_accepts_live_transcript_without_legacy_voice_session(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    adapter.on_event(session, {"type": "input_transcript", "text": "Я снимаю Бранденбургские ворота в Калининграде"})
    result = await adapter.execute_tool(
        session,
        {"name": "start_research", "id": "research-1", "args": {"owner_context": "Найди проверенные факты"}},
    )
    assert result["accepted"] is True
    with svc.store.connection() as db:
        job = db.execute("SELECT payload_json FROM jobs WHERE id=?", (result["operation_id"],)).fetchone()
    assert "live_transcript" in job["payload_json"]
    assert not svc.store.connection().execute("SELECT 1 FROM voice_sessions").fetchone()


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
    prepared = await adapter.execute_tool(
        session,
        {
            "name": "prepare_publication",
            "id": "prepare-1",
            "args": {
                "destinations": ["tg-safe"],
                "scheduled_for": "2026-09-27T10:00:00+02:00",
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
