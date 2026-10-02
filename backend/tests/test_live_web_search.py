from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story.db import Store
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient
from test_backend import config


class FailingSearchExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "web_search"
        self.calls += 1
        raise GeminiUnavailable(123.0)


class PassingSearchExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "web_search"
        self.calls += 1
        return await call("key-a", 5.0)


@pytest.mark.asyncio
async def test_web_search_uses_supported_grounding_models_in_order(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
        gemini_model="gemini-3.1-flash-lite",
        gemini_fallback_model="gemini-3.5-flash-lite",
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    primary = FailingSearchExecutor()
    fallback = PassingSearchExecutor()
    first = client.web_search_routes[0]
    second = client.web_search_routes[1]
    client.web_search_routes = [
        (first[0], first[1], first[2], primary),
        (second[0], second[1], second[2], fallback),
    ]
    models = []

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        assert operation == "web_search"
        models.append(model)
        payload = {
            "summary": "Search summary",
            "official_source_urls": [],
            "facts": [{
                "claim_key": "test-fact",
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
    result = await client.search_web("test query", {"place_name": "Test place"})

    assert primary.calls == 1
    assert fallback.calls == 1
    assert models == ["gemini-3.8-flash"]
    assert result.payload["summary"] == "Search summary"
    assert result.grounding_sources == [{
        "type": "web",
        "title": "Source",
        "url": "https://example.com/source",
    }]


class FakeSearchHTTP:
    def __init__(self, html: str):
        self.html = html
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        request = httpx.Request("GET", url)
        return httpx.Response(200, text=self.html, request=request)


@pytest.mark.asyncio
async def test_web_search_falls_back_to_independent_result_snippets(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
        gemini_model="gemini-3.1-flash-lite",
        gemini_fallback_model="gemini-3.5-flash-lite",
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    failures = [FailingSearchExecutor(), FailingSearchExecutor()]
    client.web_search_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.web_search_routes, failures, strict=True)
    ]
    client.search_http = FakeSearchHTTP(
        """
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fofficial&amp;rut=x">
            Official source
          </a>
          <a class="result__snippet">The gate was rebuilt in 1843.</a>
        </div>
        <div class="result">
          <a class="result__a" href="https://example.org/archive">Archive</a>
          <a class="result__snippet">Historical archive entry.</a>
        </div>
        """
    )

    result = await client.search_web("Brandenburg Gate Kaliningrad", {"place_name": "Kaliningrad"})

    assert [executor.calls for executor in failures] == [1, 1]
    assert result.payload["search_provider"] == "duckduckgo_html_fallback"
    assert result.payload["facts"] == []
    assert result.grounding_sources == [
        {
            "type": "web_search",
            "title": "Official source",
            "url": "https://example.com/official",
            "supports": [{
                "kind": "search_snippet",
                "source_url": "https://example.com/official",
                "text": "The gate was rebuilt in 1843.",
            }],
        },
        {
            "type": "web_search",
            "title": "Archive",
            "url": "https://example.org/archive",
            "supports": [{
                "kind": "search_snippet",
                "source_url": "https://example.org/archive",
                "text": "Historical archive entry.",
            }],
        },
    ]
    assert len(client.search_http.calls) == 1


@pytest.mark.asyncio
async def test_web_search_marks_only_grounded_non_aggregator_official_source(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    route = client.web_search_routes[0]
    client.web_search_routes = [(route[0], route[1], route[2], PassingSearchExecutor())]

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        official = "https://museum.example.org/object"
        wiki = "https://ru.wikipedia.org/wiki/Object"
        payload = {
            "summary": "Sources found",
            "official_source_urls": [official, wiki],
            "facts": [{"claim_key": "opened-2000", "text": "Открыт в 2000 году.", "confidence": 0.95, "source_urls": [official]}],
        }
        chunks = [
            SimpleNamespace(web=SimpleNamespace(uri=official, title="Museum")),
            SimpleNamespace(web=SimpleNamespace(uri=wiki, title="Wikipedia")),
        ]
        support = SimpleNamespace(
            segment=SimpleNamespace(text="Открыт в 2000 году."),
            grounding_chunk_indices=[0],
        )
        return SimpleNamespace(
            text=json.dumps(payload, ensure_ascii=False),
            candidates=[SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=chunks,
                    grounding_supports=[support],
                )
            )],
        )

    client._generate = generate
    result = await client.search_web("object official site", {"place_name": "Object"})

    assert result.payload["official_source_urls"] == ["https://museum.example.org/object"]
    assert result.grounding_sources[0]["type"] == "official"
    assert result.grounding_sources[0]["supports"][0]["text"] == "Открыт в 2000 году."
    assert result.grounding_sources[1]["type"] == "web"
