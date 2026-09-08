from __future__ import annotations

import json

import pytest

from tools.live_e2e import Diagnostics, LiveE2EError, complete_body, make_fixture_photo, validate_research_story, validate_visual_block


def test_generated_photo_is_real_png_and_manifest_is_exact():
    assert make_fixture_photo().startswith(b"\x89PNG\r\n\x1a\n")
    chunks = [
        {"index": 0, "sha256": "a" * 64, "start_ms": 0, "end_ms": 1000, "wall_start_ms": 0, "wall_end_ms": 1100},
        {"index": 1, "sha256": "b" * 64, "start_ms": 1000, "end_ms": 2000, "wall_start_ms": 1000, "wall_end_ms": 2100},
    ]
    body = complete_body("voice-test", chunks)
    assert body["chunk_count"] == 2
    assert [(c["index"], c["sha256"]) for c in body["chunks"]] == [(0, "a" * 64), (1, "b" * 64)]


def test_research_invariants_require_https_and_reject_selected_unsupported():
    good = {
        "facts": [
            {"fact_id": "fact_supported", "text": "Supported", "evidence_supported": True, "selected": True, "sources": [{"url": "https://example.test/source"}]},
            {"fact_id": "fact_unsupported", "text": "Unsupported", "evidence_supported": False, "selected": False, "sources": []},
        ]
    }
    assert validate_research_story(good) == ["fact_supported"]
    with pytest.raises(LiveE2EError, match="Unsupported fact"):
        validate_research_story({"facts": [{"fact_id": "bad", "evidence_supported": False, "selected": True, "sources": []}]})


def test_visual_block_is_exact_not_generic():
    assert validate_visual_block({"state": "visual_blocked", "error": {"code": "vibepublish_media_ingress_not_enabled"}})
    with pytest.raises(LiveE2EError, match="Expected vibepublish_media_ingress_not_enabled"):
        validate_visual_block({"state": "visual_blocked", "error": {"code": "vibepublish_runtime_unavailable"}})


def test_diagnostics_redact_all_configured_secrets(tmp_path):
    path = tmp_path / "diagnostic.json"
    diag = Diagnostics(path, ("https://secret.invalid", "token-value", "safe-alias"), "smoke")
    diag.add("probe", "failed", message="https://secret.invalid token-value safe-alias")
    text = path.read_text()
    assert "secret.invalid" not in text and "token-value" not in text and "safe-alias" not in text
    assert json.loads(text)["steps"][0]["message"] == "<redacted> <redacted> <redacted>"
