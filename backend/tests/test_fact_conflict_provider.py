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
    assert result["coverage_complete"] is True
    assert result["batch_count"] == 1
    assert result["records"][0]["relation"] == "source_disagreement"
    assert result["records"][0]["suggested_resolution"] == "unresolved"


@pytest.mark.asyncio
async def test_conflict_detector_checks_cross_block_pairs_beyond_eighty_facts():
    items = [
        {
            "fact_id": f"fact-{index}",
            "text": (
                "Дата открытия — 1843 год."
                if index == 0
                else "Дата открытия — 1850 год."
                if index == 84
                else f"Совместимый факт номер {index}."
            ),
            "sources": [{
                "type": "web",
                "url": f"https://source{index}.example/fact",
            }],
        }
        for index in range(85)
    ]
    client = object.__new__(GeminiClient)
    calls = []

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        prompt = str(contents[0])
        calls.append(prompt)
        if "fact-0" in prompt and "fact-84" in prompt:
            payload = {
                "conflicts": [{
                    "left_fact_id": "fact-0",
                    "right_fact_id": "fact-84",
                    "relation": "contradiction",
                    "suggested_resolution": "unresolved",
                    "confidence": .91,
                    "rationale": "Для одной даты открытия указаны разные годы.",
                }]
            }
        else:
            payload = {"conflicts": []}
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))

    client._generate = generate
    client.research_routes = [
        ("gemini-test", object(), object(), PassingExecutor()),
    ]

    result = await client.detect_fact_conflicts(items, {"place_name": "Test"})
    assert result["coverage_complete"] is True
    assert result["fact_count"] == 85
    assert result["batch_count"] == 10
    assert len(calls) == 10
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert {record["left_fact_id"], record["right_fact_id"]} == {"fact-0", "fact-84"}


@pytest.mark.asyncio
async def test_publication_composition_receives_all_selected_facts():
    client = object.__new__(GeminiClient)
    captured = {}

    class ComposeExecutor:
        async def execute(self, operation, call):
            assert operation == "grounded_research"
            return await call("test-key", 2)

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        captured["prompt"] = str(contents[0])
        return SimpleNamespace(text=json.dumps({
            "concept": "Все выбранные факты учтены.",
            "draft_text": "Короткий итоговый текст.",
        }, ensure_ascii=False))

    client._generate = generate
    client.research_routes = [
        ("gemini-test", object(), object(), ComposeExecutor()),
    ]
    facts = [
        {
            "fact_id": f"fact-{index}",
            "text": f"Выбранный факт номер {index}.",
            "sources": [{"url": f"https://source{index}.example"}],
        }
        for index in range(25)
    ]
    result = await client.compose_publication(
        place_name="Королевские ворота",
        concept="",
        author_note="",
        facts=facts,
    )
    assert result["draft_text"] == "Короткий итоговый текст."
    assert "Выбранный факт номер 0." in captured["prompt"]
    assert "Выбранный факт номер 24." in captured["prompt"]
