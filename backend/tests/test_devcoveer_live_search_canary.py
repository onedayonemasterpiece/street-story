from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "devcoveer_live_search_canary.py"


def load_module():
    spec = importlib.util.spec_from_file_location("devcoveer_live_search_canary", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_accepts_native_grounding_fallback_and_continued_live_turn() -> None:
    module = load_module()
    events = [
        {"type": "ready"},
        {"type": "tool_result", "name": "search_web", "status": "error", "code": "quota"},
        {
            "type": "grounding",
            "metadata": {
                "groundingChunks": [
                    {"web": {"uri": "https://example.com/source", "title": "Source"}}
                ]
            },
        },
        {"type": "output_transcript", "text": "Нашёл подтверждение через встроенный поиск."},
        {"type": "turn_complete"},
    ]
    summary = module.summarize(events)
    module.validate_summary(summary)
    assert summary["app_search_ok"] is False
    assert summary["native_search_ok"] is True
    assert summary["native_grounding_url_count"] == 1
    assert module.validate_story(
        {"state": "draft", "draft_text": None, "facts": [], "source_count": 0},
        native_search_ok=True,
    )["native_search_used"] is True


def test_summary_requires_search_tool_and_continued_live_turn() -> None:
    module = load_module()
    events = [
        {"type": "ready"},
        {"type": "tool_result", "name": "search_web", "status": "ok"},
        {"type": "output_transcript", "text": "Нашёл подтверждение."},
        {"type": "turn_complete"},
    ]
    summary = module.summarize(events)
    module.validate_summary(summary)
    assert summary["search_ok"] is True
    assert summary["post_search_output"] is True


@pytest.mark.parametrize(
    ("events", "code"),
    [
        ([{"type": "ready"}, {"type": "turn_complete"}], "search_web_tool_missing_or_failed"),
        (
            [
                {"type": "ready"},
                {"type": "tool_result", "name": "search_web", "status": "error"},
                {"type": "turn_complete"},
            ],
            "search_web_tool_missing_or_failed",
        ),
        (
            [
                {"type": "ready"},
                {"type": "tool_result", "name": "search_web", "status": "ok"},
                {"type": "turn_complete"},
            ],
            "live_did_not_continue_after_search",
        ),
        (
            [
                {"type": "resource_fallback"},
                {"type": "tool_result", "name": "search_web", "status": "ok"},
                {"type": "output_transcript", "text": "x"},
                {"type": "turn_complete"},
            ],
            "central_authority_fell_back",
        ),
    ],
)
def test_summary_fails_closed(events, code) -> None:
    module = load_module()
    with pytest.raises(module.CanaryError, match=code):
        module.validate_summary(module.summarize(events))


def test_story_requires_grounded_fact_and_no_legacy_research_state() -> None:
    module = load_module()
    result = module.validate_story({
        "state": "draft",
        "draft_text": None,
        "source_count": 1,
        "facts": [{
            "fact_id": "f1",
            "text": "Fact",
            "evidence_supported": True,
            "sources": [{"url": "https://example.com/source"}],
        }],
    })
    assert result["supported_fact_count"] == 1


def test_story_rejects_draft_rewrite_and_researching_state() -> None:
    module = load_module()
    base = {
        "state": "draft",
        "draft_text": None,
        "source_count": 1,
        "facts": [{
            "fact_id": "f1",
            "text": "Fact",
            "evidence_supported": True,
            "sources": [{"url": "https://example.com/source"}],
        }],
    }
    with pytest.raises(module.CanaryError, match="search_tool_rewrote_draft"):
        module.validate_story({**base, "draft_text": "unexpected"})
    with pytest.raises(module.CanaryError, match="legacy_research_job_used"):
        module.validate_story({**base, "state": "researching"})
