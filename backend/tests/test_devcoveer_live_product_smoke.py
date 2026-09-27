from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "devcoveer_live_product_smoke.py"


def load_module():
    spec = importlib.util.spec_from_file_location("devcoveer_live_product_smoke", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EventClient:
    def __init__(self, events):
        self.events = events

    def get(self, path, params=None):
        request = httpx.Request("GET", "https://street-story.example" + path)
        return httpx.Response(
            200,
            json={"events": self.events, "cursor": len(self.events)},
            request=request,
        )


def test_supported_fact_ids_require_https_evidence() -> None:
    module = load_module()
    ids = module.supported_fact_ids({
        "facts": [
            {
                "fact_id": "good",
                "evidence_supported": True,
                "sources": [{"url": "https://example.com/source"}],
            },
            {
                "fact_id": "http",
                "evidence_supported": True,
                "sources": [{"url": "http://example.com/source"}],
            },
            {
                "fact_id": "unsupported",
                "evidence_supported": False,
                "sources": [{"url": "https://example.com/other"}],
            },
        ]
    })
    assert ids == ["good"]


def test_poll_events_accepts_tool_result_and_same_turn_continuation() -> None:
    module = load_module()
    cursor, result = module.poll_events(
        EventClient([
            {"type": "tool_result", "name": "edit_text", "status": "ok"},
            {"type": "output_transcript", "text": "Готово."},
            {"type": "turn_complete"},
        ]),
        "story",
        "session",
        0,
        expected_tool="edit_text",
        timeout_seconds=1,
    )
    assert cursor == 3
    assert result["tool_ok"] is True
    assert result["post_tool_output"] is True
    assert result["turn_complete"] is True


def test_poll_events_recovers_revision_conflict_inside_same_turn() -> None:
    module = load_module()
    cursor, result = module.poll_events(
        EventClient([
            {
                "type": "tool_result",
                "name": "edit_text",
                "status": "error",
                "code": "live_text_revision_conflict",
            },
            {"type": "tool_result", "name": "read_topic", "status": "ok"},
            {"type": "tool_result", "name": "edit_text", "status": "ok"},
            {"type": "output_transcript", "text": "Исправил по актуальной версии."},
            {"type": "turn_complete"},
        ]),
        "story",
        "session",
        0,
        expected_tool="edit_text",
        timeout_seconds=1,
    )
    assert cursor == 5
    assert result["tool_ok"] is True
    assert result["recoverable_tool_errors"] == ["live_text_revision_conflict"]


def test_poll_events_fails_when_revision_conflict_is_not_recovered() -> None:
    module = load_module()
    with pytest.raises(module.ProductSmokeError, match="edit_text_recoverable_conflict_unresolved"):
        module.poll_events(
            EventClient([
                {
                    "type": "tool_result",
                    "name": "edit_text",
                    "status": "error",
                    "code": "live_text_revision_conflict",
                },
                {"type": "turn_complete"},
            ]),
            "story",
            "session",
            0,
            expected_tool="edit_text",
            timeout_seconds=1,
        )


def test_poll_events_requires_publication_confirmation() -> None:
    module = load_module()
    cursor, result = module.poll_events(
        EventClient([
            {
                "type": "publication_confirmation",
                "confirmation_id": "confirm_1",
                "destinations": ["lovekenig_tg"],
                "state": "prepared",
            },
            {"type": "tool_result", "name": "prepare_publication", "status": "ok"},
            {"type": "turn_complete"},
        ]),
        "story",
        "session",
        0,
        expected_tool="prepare_publication",
        timeout_seconds=1,
        require_confirmation=True,
    )
    assert cursor == 3
    assert result["confirmation"]["confirmation_id"] == "confirm_1"


@pytest.mark.parametrize(
    ("event", "code"),
    [
        ({"type": "resource_fallback"}, "central_authority_fell_back"),
        ({"type": "error", "code": "LIVE_PROVIDER_ERROR"}, "LIVE_PROVIDER_ERROR"),
    ],
)
def test_event_error_fails_closed(event, code) -> None:
    module = load_module()
    assert module.event_error(event) == code


def test_cached_fixture_reads_only_matching_story_path(tmp_path) -> None:
    module = load_module()
    data_root = tmp_path / "data"
    data_root.mkdir()
    photo = b"x" * 12000
    source = data_root / "stories" / "story_fixture" / "source.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(photo)
    import hashlib
    meta = {
        "source_sha1": hashlib.sha1(photo).hexdigest(),
        "source_sha256": hashlib.sha256(photo).hexdigest(),
    }
    import sqlite3
    with sqlite3.connect(data_root / "street-story.sqlite3") as db:
        db.execute(
            "CREATE TABLE stories(photo_sha256 TEXT, photo_path TEXT, created_at REAL)"
        )
        db.execute(
            "INSERT INTO stories VALUES(?,?,?)",
            (meta["source_sha256"], str(source), 1.0),
        )
    assert module.cached_fixture(meta, data_root) == photo
