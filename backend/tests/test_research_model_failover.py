from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from street_story.db import Store
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient
from test_backend import config


class FailingExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "grounded_research"
        self.calls += 1
        raise GeminiUnavailable(123.0)


class PassingExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "grounded_research"
        self.calls += 1
        return await call("key-a", 5.0)


@pytest.mark.asyncio
async def test_grounded_research_fails_over_to_second_lite_model(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_model="gemini-3.1-flash-lite",
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    primary = FailingExecutor()
    fallback = PassingExecutor()
    first = client.research_routes[0]
    second = client.research_routes[1]
    client.research_routes = [
        (first[0], first[1], first[2], primary),
        (second[0], second[1], second[2], fallback),
    ]
    models = []

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        assert operation == "grounded_research"
        models.append(model)
        payload = {
            "place_name": "Test place",
            "summary": "Summary",
            "draft_text": "Draft",
            "facts": [{
                "text": "Fact",
                "confidence": 0.9,
                "source_urls": ["https://example.com/source"],
            }],
        }
        web = SimpleNamespace(uri="https://example.com/source", title="Source")
        chunk = SimpleNamespace(web=web)
        metadata = SimpleNamespace(grounding_chunks=[chunk])
        candidate = SimpleNamespace(grounding_metadata=metadata)
        return SimpleNamespace(text=json.dumps(payload), candidates=[candidate])

    client._generate = generate
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"photo")

    result = await client.research(photo, "image/jpeg", "voice", {"label": "place"}, [], [])

    assert primary.calls == 1
    assert fallback.calls == 1
    assert models == ["gemini-3.1-flash-lite"]
    assert result.payload["place_name"] == "Test place"
    assert result.grounding_sources == [{
        "type": "web",
        "title": "Source",
        "url": "https://example.com/source",
    }]
