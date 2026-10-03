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


class FailingResearchExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "grounded_research"
        self.calls += 1
        raise GeminiUnavailable(123.0)


class PassingResearchExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, operation, call):
        assert operation == "grounded_research"
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
    research_failures = [FailingResearchExecutor() for _ in client.research_routes]
    client.research_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.research_routes, research_failures, strict=True)
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
        <div class="result">
          <a class="result__a" href="http://legacy.example/insecure">Legacy HTTP</a>
          <a class="result__snippet">This must never be exposed as saveable evidence.</a>
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
    assert all(source["url"].startswith("https://") for source in result.grounding_sources)
    assert "Legacy HTTP" not in result.payload["summary"]
    assert len(client.search_http.calls) == 1




@pytest.mark.asyncio
async def test_web_search_semantically_completes_discovery_snippets_with_research_model(tmp_path):
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
    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]
    client.search_http = FakeSearchHTTP(
        """
        <div class="result">
          <a class="result__a" href="https://one.example/gate">One</a>
          <a class="result__snippet">The current gate was built from 1843 to 1850.</a>
        </div>
        <div class="result">
          <a class="result__a" href="https://two.example/archive">Two</a>
          <a class="result__snippet">Construction of the current gate lasted from 1843 until 1850.</a>
        </div>
        """
    )

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        assert operation == "grounded_research"
        payload = {
            "summary": "Two snippets corroborate the construction period.",
            "official_source_urls": [],
            "facts": [{
                "claim_key": "royal-gate-construction-period",
                "existing_fact_id": "fact-existing",
                "text": "Королевские ворота строились в 1843–1850 годах.",
                "confidence": 0.94,
                "source_urls": [
                    "https://one.example/gate",
                    "https://two.example/archive",
                    "https://invented.example/not-allowed",
                ],
            }],
        }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "Королевские ворота годы строительства",
        {
            "place_name": "Королевские ворота",
            "known_facts": [{"fact_id": "fact-existing", "text": "Строительство: 1843–1850."}],
        },
    )

    assert semantic.calls == 1
    assert result.payload["search_provider"] == "duckduckgo_html_fallback"
    assert result.payload["semantic_completion"] == "gemini_research"
    assert len(result.payload["facts"]) == 1
    fact = result.payload["facts"][0]
    assert fact["existing_fact_id"] == "fact-existing"
    assert fact["source_urls"] == [
        "https://one.example/gate",
        "https://two.example/archive",
    ]
    assert len(result.grounding_sources) == 2




class RoutingSearchHTTP:
    def __init__(self, search_html: str, pages: dict[str, str]):
        self.search_html = search_html
        self.pages = pages
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        request = httpx.Request("GET", url)
        if "duckduckgo.com" in url:
            return httpx.Response(200, text=self.search_html, headers={"content-type": "text/html"}, request=request)
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html; charset=utf-8"}, request=request)
        return httpx.Response(404, text="not found", headers={"content-type": "text/plain"}, request=request)


def test_page_text_is_chunked_with_bounded_overlap():
    text = "\n".join([
        "Первый абзац " + "А" * 1800,
        "Второй абзац " + "Б" * 1800,
        "Третий абзац " + "В" * 1800,
    ])
    chunks = GeminiClient._chunk_page_text(text, target=2200, overlap=180)
    assert 2 <= len(chunks) <= 4
    assert all(len(chunk) <= 2400 for chunk in chunks)
    assert "Первый абзац" in chunks[0]
    assert "Третий абзац" in chunks[-1]


@pytest.mark.asyncio
async def test_discovery_fact_metadata_is_fail_soft_but_evidence_is_fail_closed(tmp_path):
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
    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]
    source_url = "https://history.example/gate"
    client.search_http = FakeSearchHTTP(
        f"""<div class="result"><a class="result__a" href="{source_url}">History</a>
        <a class="result__snippet">Три фигуры находятся на фасаде.</a></div>"""
    )

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        payload = {
            "summary": "metadata repair",
            "official_source_urls": [],
            "coverage_satisfied": True,
            "read_source_urls": [],
            "facts": [
                {"claim_key": "", "text": "Слева изображён Оттокар II.", "confidence": "bad", "source_urls": [source_url]},
                {"claim_key": "invalid-source", "text": "Этот факт не имеет найденного evidence.", "confidence": .9, "source_urls": ["https://invented.example/nope"]},
            ],
        }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {"coverage_goal": "Кто изображён слева, в центре и справа?"},
    )
    assert len(result.payload["facts"]) == 1
    accepted = result.payload["facts"][0]
    assert accepted["text"] == "Слева изображён Оттокар II."
    assert accepted["claim_key"].startswith("exact-text:")
    audit = result.payload["extraction_audit"]
    assert audit["raw_fact_count"] == 2
    assert audit["accepted_fact_count"] == 1
    assert audit["claim_key_fallback_count"] == 1
    assert audit["confidence_defaulted_count"] == 1
    assert audit["rejected"]["no_grounded_source"] == 1


@pytest.mark.asyncio
async def test_discovery_reads_selected_page_when_snippets_do_not_answer_visual_detail_query(tmp_path):
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
    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]

    page_url = "https://history.example/royal-gate"
    client.search_http = RoutingSearchHTTP(
        f"""
        <div class="result">
          <a class="result__a" href="{page_url}">Royal Gate facade</a>
          <a class="result__snippet">На фасаде находятся три исторические скульптуры; подробнее на странице.</a>
        </div>
        """,
        {
            page_url: """
              <html><body><main>
              <h1>Скульптуры Королевских ворот</h1>
              <p>Слева изображён чешский король Оттокар II, в центре — прусский король Фридрих I,
              справа — герцог Пруссии Альбрехт I.</p>
              </main><script>ignore me</script></body></html>
            """,
        },
    )

    calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = {
                "summary": "Сниппет не называет персонажей.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [page_url, "https://invented.example/blocked"],
            }
        else:
            prompt = str(contents[0])
            assert "Оттокар II" in prompt and "Фридрих I" in prompt and "Альбрехт I" in prompt
            payload = {
                "summary": "Страница прямо называет три фигуры.",
                "official_source_urls": [],
                "facts": [
                    {"claim_key": "royal-gate-sculpture-left", "existing_fact_id": "", "text": "Слева на фасаде изображён Оттокар II.", "confidence": .98, "source_urls": [page_url]},
                    {"claim_key": "royal-gate-sculpture-center", "existing_fact_id": "", "text": "В центре на фасаде изображён Фридрих I.", "confidence": .98, "source_urls": [page_url]},
                    {"claim_key": "royal-gate-sculpture-right", "existing_fact_id": "", "text": "Справа на фасаде изображён Альбрехт I.", "confidence": .98, "source_urls": [page_url]},
                ],
            }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "скульптуры на Королевских воротах Калининград описание",
        {"place_name": "Королевские ворота"},
    )

    assert calls == 2
    assert result.payload["semantic_completion"] == "gemini_research_page_evidence"
    assert {fact["text"] for fact in result.payload["facts"]} == {
        "Слева на фасаде изображён Оттокар II.",
        "В центре на фасаде изображён Фридрих I.",
        "Справа на фасаде изображён Альбрехт I.",
    }
    source = next(item for item in result.grounding_sources if item["url"] == page_url)
    assert any(item.get("kind") == "page_excerpt" and "Оттокар II" in item.get("text", "") for item in source["supports"])
    assert all(call[0] != "https://invented.example/blocked" for call in client.search_http.calls)


@pytest.mark.asyncio
async def test_fallback_filters_already_processed_exact_urls_when_fresh_sources_exist(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    failures = [FailingSearchExecutor() for _ in client.web_search_routes]
    client.web_search_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.web_search_routes, failures, strict=True)
    ]
    client.research_routes = [
        (route[0], route[1], route[2], FailingResearchExecutor())
        for route in client.research_routes
    ]
    client.search_http = FakeSearchHTTP(
        """
        <div class="result"><a class="result__a" href="https://old.example/page">Old</a>
        <a class="result__snippet">Old evidence.</a></div>
        <div class="result"><a class="result__a" href="https://fresh.example/page">Fresh</a>
        <a class="result__snippet">Fresh evidence.</a></div>
        """
    )
    result = await client.search_web(
        "new angle",
        {"previously_processed_sources": [{"url": "https://old.example/page"}]},
    )
    assert [item["url"] for item in result.grounding_sources] == ["https://fresh.example/page"]

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
