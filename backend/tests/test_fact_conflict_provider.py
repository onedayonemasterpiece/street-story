import json
from types import SimpleNamespace

import pytest

from street_story.fact_conflicts import conflict_scan_items
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient


class FailingExecutor:
    async def execute(self, operation, call):
        raise GeminiUnavailable(123.0)


class PassingExecutor:
    async def execute(self, operation, call):
        return await call("test-key", 2)


@pytest.mark.asyncio
async def test_conflict_detector_fails_over_and_validates_pair_ids():
    items = [
        {
            "fact_id": "left",
            "text": "Архитектор — Иван Иванов.",
            "sources": [{"type": "web", "url": "https://a.example/fact"}],
        },
        {
            "fact_id": "right",
            "text": "Архитектор — Пётр Петров.",
            "sources": [{"type": "official", "url": "https://official.example/fact"}],
        },
    ]
    model_items = conflict_scan_items(items)
    client = object.__new__(GeminiClient)
    models = []

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        models.append(model)
        return SimpleNamespace(text=json.dumps({
            "conflicts": [{
                "left_fact_id": "left",
                "right_fact_id": "right",
                "relation": "source_disagreement",
                "suggested_resolution": "unresolved",
                "confidence": .82,
                "rationale": "Источники расходятся; количества сайтов недостаточно для решения.",
            }]
        }, ensure_ascii=False))

    client._generate = generate
    client.research_routes = [
        ("gemini-primary", object(), object(), FailingExecutor()),
        ("gemini-fallback", object(), object(), PassingExecutor()),
    ]

    result = await client.detect_fact_conflicts(model_items, {"place_name": "Test"})
    assert models == ["gemini-fallback"]
    assert result[0]["relation"] == "source_disagreement"
    assert result[0]["suggested_resolution"] == "unresolved"
