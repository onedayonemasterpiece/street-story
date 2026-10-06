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


class CapabilitiesClient:
    def __init__(self, destinations):
        self.destinations = destinations

    def get(self, path, params=None):
        assert path == "/v1/capabilities"
        request = httpx.Request("GET", "https://street-story.example" + path)
        return httpx.Response(
            200,
            json={"destinations": self.destinations},
            request=request,
        )


def test_telegram_destination_keeps_reviewable_configured_alias() -> None:
    module = load_module()
    client = CapabilitiesClient([
        {
            "alias": "lovekenig_tg",
            "provider": "telegram",
            "status": "needs_review",
        }
    ])
    assert module.telegram_destination(client) == "lovekenig_tg"


def test_execute_publication_rejects_main_channel_alias() -> None:
    module = load_module()
    client = CapabilitiesClient([
        {
            "alias": "lovekenig_tg",
            "label": "lovekenig Telegram",
            "provider": "telegram",
            "status": "supported",
        }
    ])
    with pytest.raises(module.ProductSmokeError, match="publication_destination_not_test_safe"):
        module.telegram_destination(
            client,
            requested="lovekenig_tg",
            require_test=True,
        )


def test_execute_publication_requires_explicit_test_alias() -> None:
    module = load_module()
    client = CapabilitiesClient([
        {
            "alias": "street_story_test_tg",
            "label": "Street Story Test Group",
            "provider": "telegram",
            "status": "needs_review",
        }
    ])
    with pytest.raises(module.ProductSmokeError, match="publication_destination_required"):
        module.telegram_destination(client, require_test=True)
    assert (
        module.telegram_destination(
            client,
            requested="street_story_test_tg",
            require_test=True,
        )
        == "street_story_test_tg"
    )


def test_execute_publication_accepts_hidden_internal_e2e_alias() -> None:
    module = load_module()
    client = CapabilitiesClient([])
    assert (
        module.telegram_destination(
            client,
            requested="street_story_e2e_20260928_tg",
            require_test=True,
        )
        == "street_story_e2e_20260928_tg"
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


def test_canary_schedule_is_minute_aligned_and_keep_mode_is_bounded() -> None:
    module = load_module()
    now = module.datetime(
        2026, 9, 27, 19, 16, 59, 123456, tzinfo=module.timezone.utc
    )
    scheduled = module.publication_schedule(
        keep_publication=False,
        delay_minutes=5,
        now=now,
    )
    assert scheduled.second == 0
    assert scheduled.microsecond == 0
    assert scheduled - now > module.timedelta(hours=24)
    visible = module.publication_schedule(
        keep_publication=True,
        delay_minutes=5,
        now=now,
    )
    assert module.timedelta(minutes=4) <= visible - now <= module.timedelta(minutes=5)
    with pytest.raises(module.ProductSmokeError, match="publication_delay_invalid"):
        module.publication_schedule(keep_publication=True, delay_minutes=1, now=now)


def test_heartbeat_events_advances_cursor_without_new_turn() -> None:
    module = load_module()
    cursor = module.heartbeat_events(
        EventClient([
            {"type": "output_transcript", "text": "Готово."},
            {"type": "turn_complete"},
        ]),
        "story",
        "session",
        0,
    )
    assert cursor == 2


def test_heartbeat_events_fails_if_provider_session_closed() -> None:
    module = load_module()
    with pytest.raises(module.ProductSmokeError, match="live_session_closed"):
        module.heartbeat_events(
            EventClient([{"type": "closed"}]),
            "story",
            "session",
            0,
        )


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


def test_fixture_download_uses_raw_ram_without_image_hash_or_database_cache(monkeypatch, tmp_path) -> None:
    module = load_module()
    photo = b"updated-approved-source" * 1200
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, content=photo, request=httpx.Request("GET", url))

    monkeypatch.setattr(module.httpx, "get", fetch)
    meta = {"download_url": "https://example.com/source.jpg", "source_sha256": "obsolete"}
    loaded, provenance = module.load_fixture(meta, tmp_path / "unused-data",
                                             metadata_path=tmp_path / "fixture.json")
    assert loaded == photo
    assert calls == [meta["download_url"]]
    assert provenance == "wikimedia"
    assert not list(tmp_path.iterdir())


def test_repository_fixture_is_read_to_ram_without_new_image_file(monkeypatch, tmp_path) -> None:
    module = load_module()
    photo = b"owner-fixture" * 1200
    candidate = (tmp_path / "source.png").resolve()
    # Emulate an already committed fixture without writing an image test artifact.
    monkeypatch.setattr(Path, "is_file", lambda self: self == candidate)
    monkeypatch.setattr(Path, "read_bytes", lambda self: photo if self == candidate else b"")
    loaded, provenance = module.load_fixture(
        {"source_file": "source.png"}, tmp_path / "unused-data",
        metadata_path=tmp_path / "fixture.json",
    )
    assert loaded == photo
    assert provenance == "repository_fixture"
    assert not list(tmp_path.iterdir())


def test_visual_review_receipt_uses_lineage_and_never_downloads_image() -> None:
    module = load_module()

    class MetadataOnlyClient:
        def get(self, *args, **kwargs):
            raise AssertionError("Image bytes must not be fetched for a metadata receipt")

    visual = {"source_asset_ref": "source-1", "operation_id": "operation-1",
              "selected_asset_ref": "asset-1", "prompt_version": "owner",
              "prompt_sha256": module.OWNER_PROMPT_SHA256, "content_revision": 7}
    current = {"draft_text": "approved", "visual": visual,
               "visual_identity": {"identity_generation": 3},
               "processed_image_url": "https://images.example/asset-1.png"}
    receipt = module.validate_visual(MetadataOnlyClient(), current, "approved")
    assert receipt["selected_asset_ref"] == "asset-1"
    assert receipt["operation_id"] == "operation-1"
    assert receipt["identity_generation"] == 3
    assert receipt["image_url"] == current["processed_image_url"]
    assert "selected_sha256" not in receipt
    with pytest.raises(module.ProductSmokeError, match="visual_changed_text"):
        module.validate_visual(MetadataOnlyClient(), current, "stale draft")
    current["visual"].pop("selected_asset_ref")
    with pytest.raises(module.ProductSmokeError, match="visual_receipt_incomplete"):
        module.validate_visual(MetadataOnlyClient(), current, "approved")
