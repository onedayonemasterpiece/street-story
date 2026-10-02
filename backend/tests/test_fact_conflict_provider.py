import json
from types import SimpleNamespace

import pytest

from street_story.fact_conflicts import conflict_candidate_pairs
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
    pairs = conflict_candidate_pairs([
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
    ])
    assert len(pairs) == 1
    client = object.__new__(GeminiClient)
    models = []

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        models.append(model)
        return SimpleNamespace(text=json.dumps({
            "conflicts": [{
                "pair_id": pairs[0]["pair_id"],
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

    result = await client.detect_fact_conflicts(pairs, {"place_name": "Test"})
    assert models == ["gemini-fallback"]
    assert result[0]["relation"] == "source_disagreement"
    assert result[0]["suggested_resolution"] == "unresolved"
