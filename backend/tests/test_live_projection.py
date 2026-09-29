from __future__ import annotations

import copy
import json
import logging

from street_story.live import StreetStoryLiveAdapter


def test_live_model_reply_omits_duplicate_research_without_mutating_durable_result(caplog):
    identity = {
        "status": "owner_confirmed",
        "candidate_id": "wiki:19",
        "candidate_name": "Author confirmed gate",
        "confidence": None,
        "observations": ["Author explicitly confirmed the object"],
        "candidates": [
            {"candidate_id": f"wiki:{i}", "name": f"Gate {i}", "type": "wikipedia",
             "url": f"https://example.com/gate/{i}", "extract": "large source text " * 5000}
            for i in range(20)
        ],
    }
    full = {
        "visual_identity": identity,
        "story": {"id": "story-1", "state": "draft", "revision": 3,
                  "place_name": "Author confirmed gate", "source_count": 0,
                  "draft_text": "The author wording remains intact",
                  "visual_identity": identity, "research": "full evidence " * 20000},
    }
    original = copy.deepcopy(full)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        projected = StreetStoryLiveAdapter._model_result("confirm_place", full)
    assert full == original
    assert len(json.dumps(projected)) < 4000
    assert projected["visual_identity"]["candidate_id"] == "wiki:19"
    assert projected["visual_identity"]["candidates"][0]["candidate_id"] == "wiki:19"
    assert projected["visual_identity"]["candidate_count"] == 20
    assert projected["story"]["draft_text"] == full["story"]["draft_text"]
    assert "research" not in projected["story"]
    assert "large source text" not in caplog.text
    assert "model_chars=" in caplog.text


def test_confirmation_and_literal_text_are_never_summarized_by_projection():
    card = {"confirmation_id": "confirm-1", "text": "Exact author text — do not rewrite.",
            "destinations": ["street_story_e2e_tg"], "scheduled_for": "2026-10-01T12:00:00Z",
            "timezone": "UTC", "visual_revision": "v1", "state": "prepared"}
    full = {"confirmation": card, "text_revision": 8,
            "literal_spans": [{"text": "Exact author text", "start": 0}],
            "story": {"id": "s1", "draft_text": card["text"], "research": "bulk" * 10000}}
    projected = StreetStoryLiveAdapter._model_result("prepare_publication", full)
    assert projected["confirmation"] == card
    assert projected["text_revision"] == 8
    assert projected["literal_spans"] == full["literal_spans"]


def test_compact_context_keeps_confirmed_candidate_outside_first_page():
    candidates = [{"candidate_id": f"osm:{i}", "name": f"Object {i}"} for i in range(30)]
    identity = {"status": "owner_confirmed", "candidate_id": "osm:29",
                "candidate_name": "Object 29", "candidates": candidates}
    result = StreetStoryLiveAdapter._compact_context({
        "story": {"id": "s1", "visual_identity": identity, "facts": []},
        "editor": {"text_revision": 4},
    })
    assert result["visual_identity"]["status"] == "owner_confirmed"
    assert result["visual_identity"]["candidates"][0]["candidate_id"] == "osm:29"
    assert result["text_revision"] == 4
    assert candidates[0]["candidate_id"] == "osm:0"
