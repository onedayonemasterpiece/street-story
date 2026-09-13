from __future__ import annotations

import json

import pytest

from tools.live_e2e import (
    Diagnostics,
    LiveE2EError,
    complete_body,
    validate_primary_destinations,
    validate_research_story,
    validate_voice_messages,
)


def make_fixture_photo() -> bytes:
    # Tiny transport/component fixture only. The golden visual-identity run uses
    # a pinned real Commons photograph in tools/live_e2e.py.
    return bytes.fromhex(
        "89504e470d0a1a0a"
        "0000000d4948445200000001000000010802000000907753de"
        "0000000c49444154789c6360f8cf000000040001f6173855"
        "0000000049454e44ae426082"
    )


def test_generated_photo_is_real_png_and_manifest_is_exact():
    assert make_fixture_photo().startswith(b"\x89PNG\r\n\x1a\n")
    chunks = [
        {"index": 0, "sha256": "a" * 64, "start_ms": 0, "end_ms": 1000, "wall_start_ms": 0, "wall_end_ms": 1100},
        {"index": 1, "sha256": "b" * 64, "start_ms": 1000, "end_ms": 2000, "wall_start_ms": 1000, "wall_end_ms": 2100},
    ]
    body = complete_body("voice-test", "initial", chunks)
    assert body["kind"] == "initial"
    assert body["chunk_count"] == 2
    assert [(c["index"], c["sha256"]) for c in body["chunks"]] == [(0, "a" * 64), (1, "b" * 64)]


def test_research_invariants_require_all_provenance_https_and_reject_selected_unsupported():
    good = {
        "research_provenance": {
            "osm_present": True,
            "wikipedia_page_count": 1,
            "grounded_source_count": 2,
        },
        "facts": [
            {"fact_id": "fact_supported", "text": "Supported", "evidence_supported": True, "selected": True, "sources": [{"url": "https://example.test/source", "supports": [{"kind": "google_grounding", "text": "Supported", "source_url": "https://example.test/source"}]}]},
            {"fact_id": "fact_unsupported", "text": "Unsupported", "evidence_supported": False, "selected": False, "sources": []},
        ],
    }
    assert validate_research_story(good) == ["fact_supported"]
    bad = {
        "research_provenance": good["research_provenance"],
        "facts": [{"fact_id": "bad", "evidence_supported": False, "selected": True, "sources": []}],
    }
    with pytest.raises(LiveE2EError, match="Unsupported fact"):
        validate_research_story(bad)


def test_voice_projection_requires_durable_raw_and_cleaned_display():
    story = {
        "voice_messages": [
            {
                "session_id": "voice-a",
                "raw_transcript": "э-э, я я хочу рассказать про Дом Советов",
                "display_text": "Я хочу рассказать про Дом Советов",
            }
        ]
    }
    messages = validate_voice_messages(story, {"voice-a"})
    assert messages["voice-a"]["raw_transcript"].startswith("э-э")
    with pytest.raises(LiveE2EError, match="filler/repeat"):
        validate_voice_messages(
            {"voice_messages": [{"session_id": "voice-a", "raw_transcript": "raw", "display_text": "я я хочу оставить повтор"}]},
            {"voice-a"},
        )


def test_primary_destination_projection_is_telegram_only_for_mvp():
    capabilities = {
        "destinations": [
            {"alias": "love-tg", "label": "Полюбить Калининград", "provider": "telegram", "status": "supported"},
        ]
    }
    projected = validate_primary_destinations(capabilities)
    assert projected["telegram"]["status"] == "supported"


def test_diagnostics_redact_all_configured_secrets(tmp_path):
    path = tmp_path / "diagnostic.json"
    diag = Diagnostics(path, ("https://secret.invalid", "token-value", "safe-alias"), "smoke")
    diag.add("probe", "failed", message="https://secret.invalid token-value safe-alias")
    text = path.read_text()
    assert "secret.invalid" not in text and "token-value" not in text and "safe-alias" not in text
    assert json.loads(text)["steps"][0]["message"] == "<redacted> <redacted> <redacted>"
