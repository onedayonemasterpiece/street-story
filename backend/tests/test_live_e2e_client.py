from __future__ import annotations

import json

import pytest

from tools.live_e2e import (
    Diagnostics,
    LiveE2EError,
    WIKIMEDIA_USER_AGENT,
    complete_body,
    supported_facts,
    validate_telegram_destinations,
    validate_voice_messages,
)


def make_fixture_photo() -> bytes:
    # Tiny transport/component fixture only. Golden visual identity uses the
    # pinned real Commons photograph in tools/live_e2e.py.
    return bytes.fromhex(
        "89504e470d0a1a0a"
        "0000000d4948445200000001000000010802000000907753de"
        "0000000c49444154789c6360f8cf000000040001f6173855"
        "0000000049454e44ae426082"
    )


def test_wikimedia_fixture_user_agent_has_contact_identity():
    assert "bot" in WIKIMEDIA_USER_AGENT.lower()
    assert "https://github.com/onedayonemasterpiece/street-story" in WIKIMEDIA_USER_AGENT
    assert "public golden fixture" in WIKIMEDIA_USER_AGENT


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


def test_research_invariants_require_sources_claim_support_and_reject_selected_unsupported():
    good = {
        "source_count": 1,
        "sources": [{"title": "Example", "url": "https://example.test/source"}],
        "facts": [
            {
                "fact_id": "fact_supported",
                "text": "Supported",
                "evidence_supported": True,
                "selected": True,
                "sources": [{
                    "url": "https://example.test/source",
                    "supports": [{
                        "kind": "google_grounding",
                        "text": "Supported",
                        "source_url": "https://example.test/source",
                    }],
                }],
            },
            {"fact_id": "fact_unsupported", "text": "Unsupported", "evidence_supported": False, "selected": False, "sources": []},
        ],
    }
    assert [row["fact_id"] for row in supported_facts(good)] == ["fact_supported"]
    bad = {
        "source_count": 1,
        "sources": good["sources"],
        "facts": [{"fact_id": "bad", "evidence_supported": False, "selected": True, "sources": []}],
    }
    with pytest.raises(LiveE2EError, match="Unsupported fact"):
        supported_facts(bad)


def test_voice_projection_requires_order_raw_and_cleaned_display():
    story = {
        "voice_messages": [
            {"session_id": "voice-a", "raw_transcript": "э-э, я я хочу", "display_text": "Я хочу рассказать"},
            {"session_id": "voice-b", "raw_transcript": "второе", "display_text": "Второе сообщение"},
        ]
    }
    validate_voice_messages(story, ["voice-a", "voice-b"])
    with pytest.raises(LiveE2EError, match="filler"):
        validate_voice_messages(
            {"voice_messages": [{"session_id": "voice-a", "raw_transcript": "raw", "display_text": "я я хочу оставить повтор"}]},
            ["voice-a"],
        )


def test_mvp_destination_projection_is_telegram_only():
    capabilities = {
        "destinations": [
            {"alias": "love-tg", "label": "Полюбить Калининград", "provider": "telegram", "status": "supported"},
        ]
    }
    rows = validate_telegram_destinations(capabilities)
    assert [row["alias"] for row in rows] == ["love-tg"]


def test_diagnostics_redact_all_configured_secrets(tmp_path):
    path = tmp_path / "diagnostic.json"
    diag = Diagnostics(path, ("https://secret.invalid", "token-value", "safe-alias"), "smoke")
    diag.add("probe", "failed", message="https://secret.invalid token-value safe-alias")
    text = path.read_text()
    assert "secret.invalid" not in text and "token-value" not in text and "safe-alias" not in text
    assert json.loads(text)["steps"][0]["message"] == "<redacted> <redacted> <redacted>"
