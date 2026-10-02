from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from street_story.config import Settings
from street_story.live import FUNCTIONS, StreetStoryLiveAdapter, ensure_live_schema
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.providers import GroundedResearch
from street_story.service import ConflictError, ProviderBundle


def test_live_interaction_is_pinned_to_manual_activity_release() -> None:
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    text = requirements.read_text(encoding="utf-8")
    assert (
        "live-interaction @ "
        "https://github.com/onedayonemasterpiece/live-interaction/archive/"
        "refs/tags/v0.3.7-rc.1.tar.gz#sha256=f817bea35bb8d7d1e9778ba6dac9fb88a42e50c3f827b25aa025c6d25d8e7660"
    ) in text


def test_live_initialization_declares_application_search_function(tmp_path) -> None:
    svc, adapter, session, _events = make_service(tmp_path)
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model="gemini-3.8-live")
    configuration = initialized["configuration"]
    assert configuration["search_enabled"] is True
    assert configuration["manual_activity_detection"] is True
    assert configuration["application_search_function"] == "search_web"
    assert any(item["name"] == "search_web" for item in configuration["functions"])


def test_live_functions_expose_place_and_search_tools_not_async_research_job() -> None:
    names = [item["name"] for item in FUNCTIONS]
    assert "resolve_place" in names
    assert "confirm_place" in names
    assert "search_web" in names
    assert "save_research_facts" in names
    assert "record_fact_conflicts" in names
    assert "resolve_fact_conflict" in names
    assert "start_research" not in names


PHOTO = b"live-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class FakeOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {"display_name": "Калининград"}, "nearby": []}


class FakeWiki:
    async def nearby(self, lat, lon):
        return [
            {
                "pageid": 77,
                "title": "Бранденбургские ворота",
                "url": "https://ru.wikipedia.org/wiki/Бранденбургские_ворота_(Калининград)",
                "extract": "Бранденбургские ворота — исторические городские ворота Калининграда.",
            }
        ]


class FakeGemini:
    def __init__(self):
        self.searches = []

    async def identify_photo(self, photo_path, photo_mime, transcript, candidates):
        assert any(item.get("candidate_id") == "wiki:77" for item in candidates)
        return {
            "status": "uncertain",
            "candidate_id": "wiki:77",
            "confidence": 0.71,
            "observations": ["Арка и фасад похожи, но требуется подтверждение автора."],
            "alternative_candidate_ids": [],
        }

    async def search_web(self, query, topic_context):
        self.searches.append((query, topic_context))
        return GroundedResearch(
            payload={
                "summary": "Найдено два проверяемых факта.",
                "facts": [
                    {
                        "claim_key": "brandenburg-gate-location-kaliningrad",
                        "existing_fact_id": "legacy-location",
                        "text": "Бранденбургские ворота находятся в Калининграде.",
                        "confidence": 0.98,
                        "source_urls": ["https://example.com/brandenburg"],
                    },
                    {
                        "claim_key": "unsupported-demo-fact",
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
                {"alias": "street_story_e2e_test", "kind": "destination", "label": "Test Telegram", "provider": "telegram"}
            ],
            "capabilities": [
                {
                    "destination": "street_story_e2e_test",
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
        publication_test_alias="street_story_e2e_test",
        osm_user_agent="Street Story Live tests",
    )


def mark_identity_ready(svc, story_id: str, *, name: str = "Бранденбургские ворота") -> None:
    with svc.store.tx() as db:
        row = svc._story_row(db, story_id)
        research = json.loads(row["research_json"] or "{}")
        research["visual_identity"] = {
            "status": "owner_confirmed",
            "candidate_id": "wiki:77",
            "candidate_name": name,
            "confidence": None,
            "observations": ["Подтверждено для теста."],
            "candidates": [
                {
                    "candidate_id": "wiki:77",
                    "name": name,
                    "type": "wikipedia",
                    "url": "https://ru.wikipedia.org/wiki/Test",
                }
            ],
        }
        db.execute(
            "UPDATE stories SET state='identity_ready',place_name=?,research_json=?,updated_at=? WHERE id=?",
            (name, json.dumps(research, ensure_ascii=False), svc.store.now(), story_id),
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
    adapter = StreetStoryLiveAdapter(
        svc,
        lambda _session, event: events.append(event),
        lambda _session, _message: None,
    )
    ensure_live_schema(svc)
    session = SimpleNamespace(
        id="live_1234567890abcdef",
        resource_id=story["id"],
        state={"recent_user": __import__("collections").deque(maxlen=24), "recent_model": __import__("collections").deque(maxlen=16), "literal": None},
    )
    return svc, adapter, session, events


@pytest.mark.asyncio
async def test_live_place_resolution_and_owner_confirmation_use_osm_wikipedia_context(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    adapter.on_event(
        session,
        {"type": "input_transcript", "text": "Это Бранденбургские ворота в Калининграде."},
    )

    resolved = await adapter.execute_tool(
        session,
        {
            "name": "resolve_place",
            "id": "place-1",
            "args": {"owner_hint": "Бранденбургские ворота, Калининград"},
        },
    )
    identity = resolved["visual_identity"]
    assert identity["status"] == "uncertain"
    assert identity["candidate_id"] == "wiki:77"
    assert identity["candidate_name"] == "Бранденбургские ворота"
    assert resolved["wikipedia"][0]["url"].startswith("https://ru.wikipedia.org/")

    confirmed = await adapter.execute_tool(
        session,
        {
            "name": "confirm_place",
            "id": "place-2",
            "args": {"candidate_name": "Бранденбургские ворота"},
        },
    )
    assert confirmed["visual_identity"]["status"] == "owner_confirmed"
    assert confirmed["visual_identity"]["candidate_id"] == "wiki:77"
    story = svc.story(session.resource_id)
    assert story["place_name"] == "Бранденбургские ворота"
    assert story["visual_identity"]["status"] == "owner_confirmed"
    compact = adapter._compact_context(adapter._topic_state(session.resource_id))
    assert compact["visual_identity"]["candidates"][0]["candidate_id"] == "wiki:77"
    assert any(event.get("type") == "product_state" for event in events)


@pytest.mark.asyncio
async def test_live_search_and_publication_are_blocked_until_identity_is_ready(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)

    with pytest.raises(Exception) as search_error:
        await adapter.execute_tool(
            session,
            {
                "name": "search_web",
                "id": "search-before-identity",
                "args": {"query": "история объекта"},
            },
        )
    assert getattr(search_error.value, "code", None) == "identity_required"

    with pytest.raises(Exception) as visual_error:
        await adapter.execute_tool(
            session,
            {
                "name": "generate_visual",
                "id": "visual-before-identity",
                "args": {"visual_instruction": "сделай постер"},
            },
        )
    assert getattr(visual_error.value, "code", None) == "identity_required"

    future = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).astimezone(timezone(timedelta(hours=2))).isoformat(timespec="seconds")
    with pytest.raises(Exception) as publish_error:
        await adapter.execute_tool(
            session,
            {
                "name": "prepare_publication",
                "id": "publish-before-identity",
                "args": {
                    "destinations": ["street_story_e2e_test"],
                    "scheduled_for": future,
                    "timezone": "Europe/Kaliningrad",
                },
            },
        )
    assert getattr(publish_error.value, "code", None) == "identity_required"


@pytest.mark.asyncio
async def test_live_discovery_fallback_is_semantically_completed_by_mira(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)

    async def fallback_search(query, topic_context):
        return GroundedResearch(
            payload={
                "summary": "Discovery only",
                "facts": [],
                "search_provider": "duckduckgo_html_fallback",
            },
            grounding_sources=[{
                "type": "web_search",
                "title": "Archive",
                "url": "https://archive.example/gate",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": "https://archive.example/gate",
                    "text": "В 1843 году началось строительство нынешних ворот.",
                }],
            }],
        )

    svc.providers.gemini.search_web = fallback_search
    search_result = await adapter.execute_tool(
        session,
        {
            "name": "search_web",
            "id": "search-discovery",
            "args": {"query": "история ворот"},
        },
    )
    assert search_result["discovery_only"] is True
    assert search_result["facts"] == []
    source_ref = search_result["sources"][0]["source_ref"]
    assert source_ref.startswith("websrc_")

    saved = await adapter.execute_tool(
        session,
        {
            "name": "save_research_facts",
            "id": "save-discovery-facts",
            "args": {
                "facts": [{
                    "claim_key": "current-gate-construction-start-1843",
                    "text": "Строительство нынешних ворот началось в 1843 году.",
                    "confidence": 0.82,
                    "selected": True,
                    "source_refs": [source_ref],
                }],
            },
        },
    )
    assert len(saved["facts"]) == 1
    current = svc.story(story_id)
    assert len(current["facts"]) == 1
    assert current["facts"][0]["evidence_supported"] is True
    assert current["facts"][0]["selected"] is True
    assert current["facts"][0]["sources"][0]["supports"][0]["kind"] == "search_snippet"
    with svc.store.connection() as db:
        research = json.loads(
            db.execute("SELECT research_json FROM stories WHERE id=?", (story_id,)).fetchone()[0]
        )
    assert research["live_web_searches"][-1]["semantic_completion"] == "mira_live"
    assert research["draft_needs_refresh"] is True


@pytest.mark.asyncio
async def test_live_discovery_merges_multiple_sources_for_same_model_fact_identity(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)

    async def fallback_search(query, topic_context):
        return GroundedResearch(
            payload={"summary": "Discovery only", "facts": [], "search_provider": "duckduckgo_html_fallback"},
            grounding_sources=[
                {
                    "type": "web_search",
                    "title": "Source A",
                    "url": "https://a.example/gate",
                    "supports": [{"kind": "search_snippet", "source_url": "https://a.example/gate", "text": "Ворота строились в 1843–1850 годах."}],
                },
                {
                    "type": "web_search",
                    "title": "Source B",
                    "url": "https://b.example/gate",
                    "supports": [{"kind": "search_snippet", "source_url": "https://b.example/gate", "text": "Строительство ворот продолжалось с 1843 по 1850 год."}],
                },
            ],
        )

    svc.providers.gemini.search_web = fallback_search
    search_result = await adapter.execute_tool(
        session,
        {"name": "search_web", "id": "search-corroboration", "args": {"query": "годы строительства ворот"}},
    )
    refs = [item["source_ref"] for item in search_result["sources"]]
    saved = await adapter.execute_tool(
        session,
        {
            "name": "save_research_facts",
            "id": "save-corroboration",
            "args": {
                "facts": [
                    {
                        "claim_key": "gate-construction-period",
                        "text": "Королевские ворота строились в 1843–1850 годах.",
                        "confidence": 0.9,
                        "selected": True,
                        "source_refs": [refs[0]],
                    },
                    {
                        "claim_key": "gate-construction-period",
                        "text": "Королевские ворота строились в 1843–1850 годах.",
                        "confidence": 0.95,
                        "selected": True,
                        "source_refs": [refs[1]],
                    },
                ],
            },
        },
    )
    assert len(saved["facts"]) == 1
    assert len(saved["facts"][0]["sources"]) == 2
    current = svc.story(story_id)
    assert len(current["facts"]) == 1
    assert len(current["facts"][0]["sources"]) == 2


@pytest.mark.asyncio
async def test_live_discovery_fallback_rejects_unseen_source_url(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)

    async def fallback_search(query, topic_context):
        return GroundedResearch(
            payload={"summary": "Discovery only", "facts": [], "search_provider": "duckduckgo_html_fallback"},
            grounding_sources=[{
                "type": "web_search",
                "title": "Archive",
                "url": "https://archive.example/gate",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": "https://archive.example/gate",
                    "text": "В 1843 году началось строительство нынешних ворот.",
                }],
            }],
        )

    svc.providers.gemini.search_web = fallback_search
    search_result = await adapter.execute_tool(
        session,
        {"name": "search_web", "id": "search-discovery-unknown", "args": {"query": "история ворот"}},
    )
    assert search_result["sources"][0]["source_ref"].startswith("websrc_")
    with pytest.raises(ConflictError) as exc:
        await adapter.execute_tool(
            session,
            {
                "name": "save_research_facts",
                "id": "save-bad-source",
                "args": {
                    "facts": [{
                        "claim_key": "invented",
                        "text": "Новый факт.",
                        "confidence": 0.9,
                        "selected": True,
                        "source_refs": ["websrc_00000000000000000000"],
                    }],
                },
            },
        )
    assert exc.value.code == "live_research_fact_source_unknown"


@pytest.mark.asyncio
async def test_mira_can_record_conflict_without_server_pair_heuristics(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
    with svc.store.tx() as db:
        for fact_id, fact_text, url in (
            ("fact-a", "Строительство ворот началось в 1843 году.", "https://a.example/source"),
            ("fact-b", "Строительство ворот началось в 1845 году.", "https://b.example/source"),
        ):
            db.execute(
                "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    story_id,
                    fact_id,
                    fact_text,
                    0.8,
                    1,
                    1,
                    json.dumps([{
                        "type": "web",
                        "title": "Source",
                        "url": url,
                        "supports": [{"kind": "model_evidence", "source_url": url, "text": fact_text}],
                    }], ensure_ascii=False),
                ),
            )

    result = await adapter.execute_tool(
        session,
        {
            "name": "record_fact_conflicts",
            "id": "record-conflict",
            "args": {
                "conflicts": [{
                    "left_fact_id": "fact-a",
                    "right_fact_id": "fact-b",
                    "relation": "contradiction",
                    "suggested_resolution": "unresolved",
                    "confidence": 0.88,
                    "rationale": "Один и тот же этап строительства указан с разными датами.",
                }],
            },
        },
    )
    assert result["recorded_count"] == 1
    conflicts = adapter._topic_state(story_id)["fact_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["relation"] == "contradiction"
    assert conflicts[0]["arbitrated_by"] is None


@pytest.mark.asyncio
async def test_live_web_search_stays_in_session_and_does_not_rewrite_draft(tmp_path):
    svc, adapter, session, events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
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
    assert len(result["facts"]) == 2
    supported = [fact for fact in result["facts"] if fact["evidence_supported"]]
    unsupported = [fact for fact in result["facts"] if not fact["evidence_supported"]]
    assert len(supported) == 1
    assert len(unsupported) == 1
    assert svc.providers.gemini.searches[0][0] == "Бранденбургские ворота Калининград история"
    assert "Я снимаю Бранденбургские ворота" in svc.providers.gemini.searches[0][1]["recent_author_context"]

    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE story_id=? AND kind='research'", (story_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM voice_sessions").fetchone()[0] == 0
        rows = list(db.execute("SELECT text,evidence_supported,sources_json FROM facts WHERE story_id=? ORDER BY rowid", (story_id,)))
        story = db.execute("SELECT state,draft_text,research_json FROM stories WHERE id=?", (story_id,)).fetchone()
    assert len(rows) == 2
    assert rows[0]["evidence_supported"] == 1
    assert rows[1]["evidence_supported"] == 0
    assert "example.com/brandenburg" in rows[0]["sources_json"]
    assert story["draft_text"] == "Авторский текст"
    assert story["state"] != "researching"
    research = __import__("json").loads(story["research_json"])
    assert research["live_web_searches"][-1]["query"] == "Бранденбургские ворота Калининград история"
    assert any(event.get("type") == "product_state" for event in events)



@pytest.mark.asyncio
async def test_live_web_search_does_not_reselect_a_previously_rejected_fact(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
    with svc.store.tx() as db:
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                story_id,
                "legacy-location",
                "Бранденбургские ворота находятся в Калининграде.",
                0.98,
                1,
                0,
                '[{"type":"web","title":"Brandenburg source","url":"https://example.com/brandenburg"}]',
            ),
        )

    await adapter.execute_tool(
        session,
        {
            "name": "search_web",
            "id": "search-rejected",
            "args": {"query": "Бранденбургские ворота Калининград история"},
        },
    )

    with svc.store.connection() as db:
        rows = list(db.execute(
            "SELECT fact_id,text,evidence_supported,selected FROM facts WHERE story_id=? ORDER BY rowid",
            (story_id,),
        ))
    location = next(row for row in rows if row["fact_id"] == "legacy-location")
    assert location["text"] == "Бранденбургские ворота находятся в Калининграде."
    assert location["evidence_supported"] == 1
    assert location["selected"] == 0
    assert any(row["evidence_supported"] == 0 for row in rows if row["fact_id"] != "legacy-location")


@pytest.mark.asyncio
async def test_mira_arbitration_is_persisted_without_changing_fact_selection(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    story_id = session.resource_id
    mark_identity_ready(svc, story_id)
    now = svc.store.now()
    with svc.store.tx() as db:
        db.execute(
            """
            INSERT INTO fact_conflicts(
              story_id,conflict_id,poi_key,left_fact_id,right_fact_id,left_text,right_text,
              relation,detector_confidence,suggested_resolution,suggested_fact_id,
              detector_rationale,final_resolution,final_fact_id,arbitration_reason,
              arbitration_confidence,arbitrated_by,evidence_json,times_seen,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,1,?,?)
            """,
            (
                story_id,
                "conflict_test",
                "wiki:77",
                "fact_left",
                "fact_right",
                "Построены в 1843 году.",
                "Построены в 1845 году.",
                "contradiction",
                .91,
                "unresolved",
                None,
                "Источники расходятся по дате.",
                json.dumps({"left": {"source_count": 3}, "right": {"source_count": 1}}),
                now,
                now,
            ),
        )
        for fact_id, text in (
            ("fact_left", "Построены в 1843 году."),
            ("fact_right", "Построены в 1845 году."),
        ):
            db.execute(
                "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (story_id, fact_id, text, .9, 1, 1, "[]"),
            )

    result = await adapter.execute_tool(
        session,
        {
            "name": "resolve_fact_conflict",
            "id": "conflict-resolution-1",
            "args": {
                "conflict_id": "conflict_test",
                "resolution": "prefer_left",
                "reason": "Первичный источник датирует завершение 1843 годом.",
                "confidence": .84,
            },
        },
    )

    assert result["resolution"] == "prefer_left"
    assert result["preferred_fact_id"] == "fact_left"
    with svc.store.connection() as db:
        conflict = db.execute(
            "SELECT final_resolution,final_fact_id,arbitration_confidence,arbitrated_by,evidence_json "
            "FROM fact_conflicts WHERE story_id=? AND conflict_id=?",
            (story_id, "conflict_test"),
        ).fetchone()
        selected = list(db.execute(
            "SELECT fact_id,selected FROM facts WHERE story_id=? ORDER BY fact_id",
            (story_id,),
        ))
        telemetry = db.execute(
            "SELECT COUNT(*) FROM live_diagnostics WHERE story_id=? AND source='fact_conflict' "
            "AND event_type='fact_conflict_arbitrated'",
            (story_id,),
        ).fetchone()[0]
    assert conflict["final_resolution"] == "prefer_left"
    assert conflict["final_fact_id"] == "fact_left"
    assert conflict["arbitrated_by"] == "mira"
    assert conflict["arbitration_confidence"] == .84
    assert [bool(row["selected"]) for row in selected] == [True, True]
    assert telemetry == 1

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
    mark_identity_ready(svc, story_id)
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
                "destinations": ["street_story_e2e_test"],
                "scheduled_for": scheduled_for,
                "timezone": "Europe/Kaliningrad",
            },
        },
    )
    card = prepared["confirmation"]
    assert card["text"] == "Готовый текст"
    assert card["visual_revision"] == "visual-r1"
    assert card["destinations"] == ["street_story_e2e_test"]

    with pytest.raises(ConflictError) as invalid_destination:
        await adapter.execute_tool(
            session,
            {
                "name": "prepare_publication",
                "id": "prepare-invalid-destination",
                "args": {
                    "destinations": ["alias street_story_e2e_test"],
                    "scheduled_for": scheduled_for,
                    "timezone": "Europe/Kaliningrad",
                },
            },
        )
    assert invalid_destination.value.code == "publish_destination_invalid"

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

def test_live_transcripts_are_retained_as_bounded_diagnostics(tmp_path):
    svc, adapter, session, _events = make_service(tmp_path)
    adapter.on_event(session, {"type": "input_transcript", "text": "Покажи, что ты видишь на фотографии."})
    adapter.on_event(session, {"type": "output_transcript", "text": "Вижу кирпичную арку и башни."})
    with svc.store.connection() as db:
        rows = db.execute(
            "SELECT source,event_type,payload_json FROM live_diagnostics WHERE story_id=? ORDER BY id",
            (session.resource_id,),
        ).fetchall()
    assert [row["event_type"] for row in rows] == ["input_transcript", "output_transcript"]
    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert payloads[0]["role"] == "user"
    assert payloads[0]["text"] == "Покажи, что ты видишь на фотографии."
    assert payloads[1]["role"] == "assistant"
    assert payloads[1]["text"] == "Вижу кирпичную арку и башни."


def test_live_start_queues_orientation_correct_source_photo_snapshot(tmp_path):
    from PIL import Image

    svc, _adapter, session, events = make_service(tmp_path)
    with svc.store.connection() as db:
        row = db.execute("SELECT photo_path FROM stories WHERE id=?", (session.resource_id,)).fetchone()
        photo_path = Path(row["photo_path"])
    image = Image.new("RGB", (80, 40), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(photo_path, "JPEG", exif=exif)

    writes = []
    adapter = StreetStoryLiveAdapter(
        svc,
        lambda _session, event: events.append(event),
        lambda _session, message: writes.append(message),
    )
    adapter.on_started(session)

    assert len(writes) == 1
    snapshot = writes[0]
    assert snapshot["type"] == "snapshot"
    assert snapshot["mime_type"] == "image/jpeg"
    assert snapshot["optional"] is False
    data = base64.b64decode(snapshot["data"])
    assert len(data) <= 480 * 1024
    with Image.open(io.BytesIO(data)) as normalized:
        assert normalized.size == (40, 80)
    visual = [event for event in events if event.get("type") == "visual_context"][-1]
    assert visual["status"] == "ready"
    assert (visual["width"], visual["height"]) == (40, 80)
