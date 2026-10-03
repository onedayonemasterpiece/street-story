from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story.db import Store
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_runs import begin_research_run, manifest_complete, run_manifest
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
async def test_native_search_phase_timeout_falls_back_without_serial_key_stall(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    client.NATIVE_WEB_SEARCH_PHASE_SECONDS = 0.02

    class HangingExecutor:
        def __init__(self):
            self.calls = 0
            self.started_at = None
            self.cancelled_at = None

        async def execute(self, operation, call):
            assert operation == "web_search"
            self.calls += 1
            self.started_at = asyncio.get_running_loop().time()
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.cancelled_at = asyncio.get_running_loop().time()
                raise
            raise AssertionError("native phase deadline should cancel this executor")

    first = HangingExecutor()
    second = HangingExecutor()
    routes = client.web_search_routes[:2]
    client.web_search_routes = [
        (routes[0][0], routes[0][1], routes[0][2], first),
        (routes[1][0], routes[1][1], routes[1][2], second),
    ]

    public_calls = 0
    async def public_search(query, excluded_urls=None):
        nonlocal public_calls
        public_calls += 1
        return GroundedResearch(
            payload={
                "summary": "Public evidence.",
                "official_source_urls": [],
                "facts": [],
                "search_provider": "duckduckgo_html_fallback",
            },
            grounding_sources=[{
                "type": "web_search",
                "title": "Public source",
                "url": "https://public.example/source",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": "https://public.example/source",
                    "text": "Exact public evidence passage.",
                }],
            }],
        )

    async def no_semantic_completion(query, topic_context, discovery, *, timeout_seconds):
        return None

    client._public_web_search = public_search
    client._semantic_complete_discovery_best_effort = no_semantic_completion

    started = asyncio.get_running_loop().time()
    result = await client.search_web("Royal Gates facts", {})
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.5
    assert first.calls == 1
    assert first.started_at is not None and first.cancelled_at is not None
    assert first.cancelled_at - first.started_at < .25
    assert second.calls == 0
    assert public_calls == 1
    assert result.payload["native_search_status"] == "timeout"
    assert result.payload["semantic_status"] == "live_model_required"
    assert result.payload["coverage_satisfied"] is False
    assert result.payload["missing_aspects"] == ["semantic_model_temporarily_unavailable"]
    assert result.grounding_sources[0]["supports"][0]["text"] == "Exact public evidence passage."


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
        if operation == "grounded_research":
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": False,
                    "summary": "Search summary",
                    "missing_aspects": ["test query"],
                    "coverage_items": [{
                        "requirement": "Ответить на test query.",
                        "satisfied": False,
                        "fact_indices": [],
                        "evidence_refs": [],
                        "rationale": "В grounding нет конкретного supporting passage.",
                    }],
                    "read_source_urls": [],
                }),
                candidates=[],
            )
        assert operation == "web_search"
        models.append(model)
        payload = {
            "summary": "Search summary",
            "coverage_satisfied": True,
            "missing_aspects": [],
            "continuation_needed": False,
            "continuation_reason": "",
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
    assert [(source["title"], source["url"]) for source in result.grounding_sources] == [
        ("Official source", "https://example.com/official"),
        ("Archive", "https://example.org/archive"),
    ]
    assert [source["supports"][0]["text"] for source in result.grounding_sources] == [
        "The gate was rebuilt in 1843.",
        "Historical archive entry.",
    ]
    assert all(source["supports"][0]["evidence_ref"].startswith("evref_") for source in result.grounding_sources)
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
        ref_one = client._support_evidence_ref(
            "https://one.example/gate",
            {
                "kind": "search_snippet",
                "source_url": "https://one.example/gate",
                "text": "The current gate was built from 1843 to 1850.",
            },
        )
        ref_two = client._support_evidence_ref(
            "https://two.example/archive",
            {
                "kind": "search_snippet",
                "source_url": "https://two.example/archive",
                "text": "Construction of the current gate lasted from 1843 until 1850.",
            },
        )
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
                "evidence_refs": [ref_one, ref_two],
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
    refs = {
        support["evidence_ref"]
        for source in result.grounding_sources
        for support in source.get("supports") or []
    }
    assert set(fact["evidence_refs"]) <= refs


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


@pytest.mark.asyncio
async def test_page_fetch_persists_full_chunk_manifest_beyond_old_9k_limit(tmp_path):
    settings = config(tmp_path)
    store = Store(tmp_path / "street-story.sqlite3")
    story_id = "story-long-page"
    now = store.now()
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,visual_context_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'{}','{}',?,?)",
            (
                story_id,
                "client-long-page",
                "a" * 64,
                "image/jpeg",
                "/tmp/no-photo.jpg",
                "voice-chunks-v2",
                "identity_ready",
                now,
                now,
            ),
        )
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="Найти факт в хвосте длинной статьи",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-long-page",
            now=now,
        )

    url = "https://history.example/long"
    head = "<p>" + ("Исторический контекст. " * 900) + "</p>"
    tail = "<p>ХВОСТОВОЙ ФАКТ: справа изображён герцог Альбрехт I.</p>"
    client = GeminiClient(settings, store)
    client.search_http = RoutingSearchHTTP("", {url: f"<html><body><main>{head}{tail}</main></body></html>"})
    docs = await client._fetch_page_documents(
        [url],
        {
            "research_run_id": run_id,
            "research_sources": [{"url": url, "title": "Long history"}],
        },
    )

    assert url in docs
    doc = docs[url]
    assert doc["char_count"] > 9_000
    assert len(doc["chunks"]) > 1
    assert "ХВОСТОВОЙ ФАКТ" in doc["normalized_text"]
    assert any(
        "ХВОСТОВОЙ ФАКТ" in chunk["text"]
        for chunk in doc["chunks"]
    )
    with store.connection() as db:
        manifest = run_manifest(db, run_id)
    assert manifest["counts"]["chunks_planned"] == len(doc["chunks"])
    assert manifest["sources"][0]["source_version_id"] == doc["source_version_id"]


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
        <a class="result__snippet">Слева изображён Оттокар II.</a></div>"""
    )

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        valid_ref = client._support_evidence_ref(
            source_url,
            {
                "kind": "search_snippet",
                "source_url": source_url,
                "text": "Слева изображён Оттокар II.",
            },
        )
        payload = {
            "summary": "metadata repair",
            "official_source_urls": [],
            "coverage_satisfied": True,
            "read_source_urls": [],
            "facts": [
                {
                    "claim_key": "",
                    "text": "Слева изображён Оттокар II.",
                    "confidence": "bad",
                    "source_urls": [source_url],
                    "evidence_refs": [valid_ref],
                },
                {
                    "claim_key": "invalid-source",
                    "text": "Этот факт не имеет найденного evidence.",
                    "confidence": .9,
                    "source_urls": ["https://invented.example/nope"],
                    "evidence_refs": ["evref_invented"],
                },
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
    assert audit["rejected"]["no_bound_evidence"] == 1


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
    last_chunk_id = None
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls, last_chunk_id
        calls += 1
        if calls == 1:
            snippet_ref = client._support_evidence_ref(
                page_url,
                {
                    "kind": "search_snippet",
                    "source_url": page_url,
                    "text": "На фасаде находятся три исторические скульптуры; подробнее на странице.",
                },
            )
            payload = {
                "summary": "Сниппет не называет персонажей.",
                "official_source_urls": [],
                "facts": [{
                    "claim_key": "royal-gate-facade-has-three-figures",
                    "existing_fact_id": "",
                    "text": "На фасаде находятся три исторические фигуры.",
                    "confidence": .9,
                    "source_urls": [page_url],
                    "evidence_refs": [snippet_ref],
                }],
                "coverage_satisfied": False,
                "read_source_urls": [page_url, "https://invented.example/blocked"],
            }
        else:
            prompt = str(contents[0])
            if "Ты проверяешь полноту уже извлечённых facts" in prompt:
                if "Оттокар II" not in prompt:
                    payload = {
                        "coverage_satisfied": False,
                        "summary": "Сниппет не называет персонажей.",
                        "missing_aspects": ["Кто изображён на трёх фигурах."],
                        "coverage_items": [{
                            "requirement": "Установить, кто изображён на фигурах.",
                            "satisfied": False,
                            "fact_indices": [0],
                            "evidence_refs": [
                                client._support_evidence_ref(
                                    page_url,
                                    {
                                        "kind": "search_snippet",
                                        "source_url": page_url,
                                        "text": "На фасаде находятся три исторические скульптуры; подробнее на странице.",
                                    },
                                )
                            ],
                            "rationale": "Факт подтверждает наличие фигур, но не называет людей.",
                        }],
                        "read_source_urls": [page_url],
                    }
                else:
                    assert last_chunk_id
                    payload = {
                        "coverage_satisfied": True,
                        "summary": "Цель закрыта тремя отдельными фигурами.",
                        "missing_aspects": [],
                        "coverage_items": [
                            {
                                "requirement": "Кто изображён слева.",
                                "satisfied": True,
                                "fact_indices": [1],
                                "evidence_refs": [last_chunk_id],
                                "rationale": "Отдельный позиционный факт.",
                            },
                            {
                                "requirement": "Кто изображён в центре.",
                                "satisfied": True,
                                "fact_indices": [2],
                                "evidence_refs": [last_chunk_id],
                                "rationale": "Отдельный позиционный факт.",
                            },
                            {
                                "requirement": "Кто изображён справа.",
                                "satisfied": True,
                                "fact_indices": [3],
                                "evidence_refs": [last_chunk_id],
                                "rationale": "Отдельный позиционный факт.",
                            },
                        ],
                        "read_source_urls": [],
                    }
            else:
                assert "Оттокар II" in prompt and "Фридрих I" in prompt and "Альбрехт I" in prompt
                assert "Передан один chunk документа" in prompt
                chunk_id = prompt.split("Chunk id: ", 1)[1].splitlines()[0].strip()
                last_chunk_id = chunk_id
                quote = (
                    "Слева изображён чешский король Оттокар II, в центре — прусский король Фридрих I, "
                    "справа — герцог Пруссии Альбрехт I."
                )
                payload = {
                    "facts": [
                        {
                            "claim_key": "royal-gate-sculpture-left",
                            "existing_fact_id": "",
                            "text": "Слева на фасаде изображён Оттокар II.",
                            "confidence": .98,
                            "source_urls": [page_url],
                            "evidence_spans": [{"source_url": page_url, "chunk_id": chunk_id, "quote": quote}],
                        },
                        {
                            "claim_key": "royal-gate-sculpture-center",
                            "existing_fact_id": "",
                            "text": "В центре на фасаде изображён Фридрих I.",
                            "confidence": .98,
                            "source_urls": [page_url],
                            "evidence_spans": [{"source_url": page_url, "chunk_id": chunk_id, "quote": quote}],
                        },
                        {
                            "claim_key": "royal-gate-sculpture-right",
                            "existing_fact_id": "",
                            "text": "Справа на фасаде изображён Альбрехт I.",
                            "confidence": .98,
                            "source_urls": [page_url],
                            "evidence_spans": [{"source_url": page_url, "chunk_id": chunk_id, "quote": quote}],
                        },
                    ],
                    "needs_context": False,
                    "context_reason": "",
                    "continuation_needed": False,
                    "continuation_reason": "",
                }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "скульптуры на Королевских воротах Калининград описание",
        {"place_name": "Королевские ворота"},
    )

    assert calls == 4
    assert result.payload["semantic_completion"] == "gemini_research_page_chunks"
    assert result.payload["coverage_satisfied"] is True
    assert result.payload["page_chunk_count"] == 1
    assert result.payload["page_chunk_failures"] == 0
    assert {fact["text"] for fact in result.payload["facts"]} == {
        "На фасаде находятся три исторические фигуры.",
        "Слева на фасаде изображён Оттокар II.",
        "В центре на фасаде изображён Фридрих I.",
        "Справа на фасаде изображён Альбрехт I.",
    }
    page_facts = [
        fact for fact in result.payload["facts"]
        if str(fact["text"]).startswith(("Слева", "В центре", "Справа"))
    ]
    assert all(len(fact.get("evidence_refs") or []) == 1 for fact in page_facts)
    source = next(item for item in result.grounding_sources if item["url"] == page_url)
    assert any(
        item.get("kind") == "verified_page_span"
        and item.get("source_version_id")
        and item.get("evidence_ref")
        and "Оттокар II" in item.get("text", "")
        for item in source["supports"]
    )
    assert all(call[0] != "https://invented.example/blocked" for call in client.search_http.calls)


@pytest.mark.asyncio
async def test_long_page_tail_fact_is_extracted_and_all_chunks_are_accounted_for(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
        gemini_model="gemini-3.1-flash-lite",
        gemini_fallback_model="gemini-3.5-flash-lite",
    )
    store = Store(tmp_path / "street-story.sqlite3")
    now = store.now()
    story_id = "story_long_page"
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,visual_context_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'{}','{}',?,?)",
            (
                story_id,
                "client-long-page",
                "a" * 64,
                "image/jpeg",
                "/tmp/no-photo.jpg",
                "voice-chunks-v2",
                "identity_ready",
                now,
                now,
            ),
        )
        begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="Найти автора горельефов",
            expected_story_revision=1,
            identity_generation=0,
            run_id="run-long-page",
            now=now,
        )

    client = GeminiClient(settings, store)
    failures = [FailingSearchExecutor(), FailingSearchExecutor()]
    client.web_search_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.web_search_routes, failures, strict=True)
    ]
    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]

    page_url = "https://history.example/very-long-royal-gate"
    head = "".join(
        f"<p>Нейтральный абзац {index}. История городской среды без ответа на вопрос.</p>"
        for index in range(700)
    )
    tail = (
        "<p>ХВОСТОВОЙ ФАКТ: автором горельефов Королевских ворот "
        "был скульптор Вильгельм Людвиг Штюрмер.</p>"
    )
    client.search_http = RoutingSearchHTTP(
        f"""
        <div class="result">
          <a class="result__a" href="{page_url}">Long Royal Gate page</a>
          <a class="result__snippet">Большая историческая статья; автор горельефов указан внутри.</a>
        </div>
        """,
        {page_url: f"<html><body><main>{head}{tail}</main></body></html>"},
    )

    calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls
        calls += 1
        prompt = str(contents[0])
        if calls == 1:
            payload = {
                "summary": "Сниппет недостаточен.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [page_url],
            }
        elif "Ты проверяешь полноту уже извлечённых facts" in prompt:
            payload = {
                "coverage_satisfied": "Вильгельм Людвиг Штюрмер" in prompt,
                "summary": "Coverage review.",
                "missing_aspects": [] if "Вильгельм Людвиг Штюрмер" in prompt else ["автор горельефов"],
            }
        elif "ХВОСТОВОЙ ФАКТ" in prompt and "[core]" in prompt:
            chunk_id = prompt.split("Chunk id: ", 1)[1].splitlines()[0].strip()
            quote = (
                "ХВОСТОВОЙ ФАКТ: автором горельефов Королевских ворот "
                "был скульптор Вильгельм Людвиг Штюрмер."
            )
            payload = {
                "facts": [{
                    "claim_key": "royal-gate-reliefs-author",
                    "existing_fact_id": "",
                    "text": "Автором горельефов Королевских ворот был Вильгельм Людвиг Штюрмер.",
                    "confidence": .99,
                    "source_urls": [page_url],
                    "evidence_spans": [{"source_url": page_url, "chunk_id": chunk_id, "quote": quote}],
                }],
                "needs_context": False,
                "context_reason": "",
                "continuation_needed": False,
                "continuation_reason": "",
            }
        else:
            payload = {
                "facts": [],
                "needs_context": False,
                "context_reason": "",
                "continuation_needed": False,
                "continuation_reason": "",
            }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "кто автор горельефов Королевских ворот",
        {
            "place_name": "Королевские ворота",
            "coverage_goal": "Кто автор горельефов Королевских ворот?",
            "research_run_id": "run-long-page",
        },
    )

    assert calls > 3
    assert result.payload["page_chunk_count"] > 2
    assert result.payload["page_chunk_failures"] == 0
    fact = next(
        item for item in result.payload["facts"]
        if "Вильгельм Людвиг Штюрмер" in item["text"]
    )
    assert len(fact["evidence_refs"]) == 1
    source = next(item for item in result.grounding_sources if item["url"] == page_url)
    tail_support = next(
        support for support in source["supports"]
        if "ХВОСТОВОЙ ФАКТ" in support.get("text", "")
    )
    assert tail_support["evidence_ref"] == fact["evidence_refs"][0]
    assert tail_support["source_version_id"]

    with store.connection() as db:
        manifest = run_manifest(db, "run-long-page")
    assert manifest["counts"]["sources_fetched"] == 1
    assert manifest["counts"]["chunks_planned"] == result.payload["page_chunk_count"]
    assert manifest["counts"]["chunks_completed"] == result.payload["page_chunk_count"]
    assert manifest_complete(manifest) is True


@pytest.mark.asyncio
async def test_chunk_fact_with_non_verbatim_quote_is_rejected_fail_closed(tmp_path):
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

    page_url = "https://history.example/architect"
    client.search_http = RoutingSearchHTTP(
        f"""
        <div class="result">
          <a class="result__a" href="{page_url}">Architect page</a>
          <a class="result__snippet">Архитектор указан на странице.</a>
        </div>
        """,
        {
            page_url: (
                "<html><body><p>Архитектором проекта был Фридрих Штюлер. "
                "Документ также подробно описывает историю строительства и реставрации здания.</p></body></html>"
            )
        },
    )

    calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls
        calls += 1
        prompt = str(contents[0])
        if calls == 1:
            payload = {
                "summary": "Нужно прочитать страницу.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [page_url],
            }
        elif "Ты проверяешь полноту уже извлечённых facts" in prompt:
            payload = {
                "coverage_satisfied": False,
                "summary": "Valid evidence is still missing.",
                "missing_aspects": ["архитектор"],
            }
        else:
            chunk_id = prompt.split("Chunk id: ", 1)[1].splitlines()[0].strip()
            payload = {
                "facts": [{
                    "claim_key": "architect",
                    "existing_fact_id": "",
                    "text": "Архитектором проекта был Пётр Петров.",
                    "confidence": .99,
                    "source_urls": [page_url],
                    "evidence_spans": [{
                        "source_url": page_url,
                        "chunk_id": chunk_id,
                        "quote": "Архитектором проекта был Пётр Петров.",
                    }],
                }],
                "needs_context": False,
                "context_reason": "",
                "continuation_needed": False,
                "continuation_reason": "",
            }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "кто архитектор",
        {"coverage_goal": "Кто архитектор проекта?"},
    )
    assert result.payload["facts"] == []
    assert result.payload["page_chunk_failures"] == 0
    assert result.payload["extraction_audit"]["rejected"]["no_verified_span"] == 1
    source = next(item for item in result.grounding_sources if item["url"] == page_url)
    assert not any(item.get("kind") == "verified_page_span" for item in source.get("supports") or [])


@pytest.mark.asyncio
async def test_fallback_keeps_processed_urls_available_for_new_coverage_gaps(tmp_path):
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
        {
            "previously_processed_sources": [{
                "url": "https://old.example/page",
                "title": "Old",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": "https://old.example/page",
                    "text": "Cached old evidence.",
                }],
            }],
        },
    )
    assert {item["url"] for item in result.grounding_sources} == {
        "https://old.example/page",
        "https://fresh.example/page",
    }


@pytest.mark.asyncio
async def test_coverage_reviewer_does_not_treat_unordered_names_as_positional_mapping(tmp_path):
    client = GeminiClient(config(tmp_path), Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://example.org/facade"
    support = {
        "kind": "page_excerpt",
        "source_url": source_url,
        "text": "На фасаде изображены персонажи A, B и C.",
    }
    evidence_ref = client._support_evidence_ref(source_url, support)
    facts = [{
        "claim_key": "three-figures",
        "text": "На фасаде изображены персонажи A, B и C.",
        "confidence": .95,
        "source_urls": [source_url],
        "evidence_refs": [evidence_ref],
    }]
    sources = [{
        "type": "web",
        "title": "Facade",
        "url": source_url,
        "supports": [{**support, "evidence_ref": evidence_ref}],
    }]

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        prompt = str(contents[0])
        assert "неупорядоченный список людей" in prompt
        assert "слева" in prompt and "в центре" in prompt and "справа" in prompt
        return SimpleNamespace(
            text=json.dumps({
                "coverage_satisfied": False,
                "summary": "Имена известны, позиции не доказаны.",
                "missing_aspects": [
                    "Кто находится слева.",
                    "Кто находится в центре.",
                    "Кто находится справа.",
                ],
                "coverage_items": [
                    {
                        "requirement": "Установить, кто находится слева.",
                        "satisfied": False,
                        "fact_indices": [0],
                        "evidence_refs": [evidence_ref],
                        "rationale": "Общий список не задаёт позицию слева.",
                    },
                    {
                        "requirement": "Установить, кто находится в центре.",
                        "satisfied": False,
                        "fact_indices": [0],
                        "evidence_refs": [evidence_ref],
                        "rationale": "Общий список не задаёт центральную позицию.",
                    },
                    {
                        "requirement": "Установить, кто находится справа.",
                        "satisfied": False,
                        "fact_indices": [0],
                        "evidence_refs": [evidence_ref],
                        "rationale": "Общий список не задаёт позицию справа.",
                    },
                ],
                "read_source_urls": [source_url],
            }, ensure_ascii=False),
            candidates=[],
        )

    client._generate = generate
    result = await client._review_coverage_contract(
        "key-a",
        5.0,
        coverage_goal="Установить, кто находится слева, в центре и справа.",
        facts=facts,
        sources=sources,
        model="gemini-test",
        quota=None,
        allow_page_reads=True,
    )
    assert result["coverage_satisfied"] is False
    assert len(result["coverage_items"]) == 3
    assert all(item["satisfied"] is False for item in result["coverage_items"])
    assert result["read_source_urls"] == [source_url]


@pytest.mark.asyncio
async def test_slow_cached_semantic_preflight_falls_through_to_discovery(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    client.SEMANTIC_CACHE_PREFLIGHT_SECONDS = .01
    client.SEMANTIC_DISCOVERY_COMPLETION_SECONDS = .01

    failures = [FailingSearchExecutor() for _ in client.web_search_routes]
    client.web_search_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.web_search_routes, failures, strict=True)
    ]
    client.search_http = FakeSearchHTTP(
        """
        <div class="result">
          <a class="result__a" href="https://fresh.example/royal-gate">Fresh source</a>
          <a class="result__snippet">Слева изображён Отакар II.</a>
        </div>
        """
    )

    semantic_calls = 0

    async def slow_semantic(query, topic_context, discovery):
        nonlocal semantic_calls
        semantic_calls += 1
        await asyncio.sleep(.2)
        raise AssertionError("bounded best-effort semantic call should have timed out")

    client._semantic_complete_discovery = slow_semantic
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {
            "coverage_goal": "Кто изображён слева?",
            "previously_processed_sources": [{
                "url": "https://cached.example/royal-gate",
                "title": "Cached",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": "https://cached.example/royal-gate",
                    "text": "На фасаде есть три фигуры.",
                }],
            }],
        },
    )

    assert semantic_calls == 2
    assert [executor.calls for executor in failures] == [1 for _ in failures]
    assert client.search_http.calls
    assert result.payload["search_provider"] == "duckduckgo_html_fallback"
    assert result.payload["semantic_status"] == "live_model_required"
    assert result.payload["coverage_satisfied"] is False
    assert result.payload["facts"] == []
    assert any(
        source["url"] == "https://fresh.example/royal-gate"
        for source in result.grounding_sources
    )


@pytest.mark.asyncio
async def test_cached_poi_evidence_can_answer_new_coverage_without_reopening_page(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://old.example/royal-gate"
    failures = [FailingSearchExecutor() for _ in client.web_search_routes]
    client.web_search_routes = [
        (route[0], route[1], route[2], executor)
        for route, executor in zip(client.web_search_routes, failures, strict=True)
    ]
    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]
    client.search_http = FakeSearchHTTP(
        f"""<div class="result"><a class="result__a" href="{source_url}">Old</a>
        <a class="result__snippet">Фасад Королевских ворот.</a></div>"""
    )

    calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls
        calls += 1
        prompt = str(contents[0])
        assert "Отакар II" in prompt and "Фридрих I" in prompt and "Альбрехт I" in prompt
        cached_ref = client._support_evidence_ref(
            source_url,
            {
                "kind": "page_excerpt",
                "source_url": source_url,
                "text": "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
            },
        )
        if "Ты проверяешь полноту уже извлечённых facts" in prompt:
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": True,
                    "summary": "Три позиции доказаны.",
                    "missing_aspects": [],
                    "coverage_items": [
                        {
                            "requirement": "Кто изображён слева.",
                            "satisfied": True,
                            "fact_indices": [0],
                            "evidence_refs": [cached_ref],
                            "rationale": "Позиция слева указана явно.",
                        },
                        {
                            "requirement": "Кто изображён в центре.",
                            "satisfied": True,
                            "fact_indices": [1],
                            "evidence_refs": [cached_ref],
                            "rationale": "Позиция в центре указана явно.",
                        },
                        {
                            "requirement": "Кто изображён справа.",
                            "satisfied": True,
                            "fact_indices": [2],
                            "evidence_refs": [cached_ref],
                            "rationale": "Позиция справа указана явно.",
                        },
                    ],
                    "read_source_urls": [],
                }, ensure_ascii=False),
                candidates=[],
            )
        payload = {
            "summary": "Cached evidence answers who is depicted.",
            "official_source_urls": [],
            "facts": [
                {
                    "claim_key": "left", "existing_fact_id": "", "text": "Слева изображён Отакар II.",
                    "confidence": .95, "source_urls": [source_url], "evidence_refs": [cached_ref],
                },
                {
                    "claim_key": "center", "existing_fact_id": "", "text": "В центре изображён Фридрих I.",
                    "confidence": .95, "source_urls": [source_url], "evidence_refs": [cached_ref],
                },
                {
                    "claim_key": "right", "existing_fact_id": "", "text": "Справа изображён Альбрехт I.",
                    "confidence": .95, "source_urls": [source_url], "evidence_refs": [cached_ref],
                },
            ],
            "coverage_satisfied": True,
            "read_source_urls": [],
        }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {
            "coverage_goal": "Кто изображён слева, в центре и справа?",
            "previously_processed_sources": [{
                "url": source_url,
                "title": "Saved evidence",
                "supports": [{
                    "kind": "page_excerpt",
                    "source_url": source_url,
                    "text": "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
                }],
            }],
        },
    )
    assert calls == 2
    assert [executor.calls for executor in failures] == [0 for _ in failures]
    assert client.search_http.calls == []
    assert result.payload["search_provider"] == "poi_cache"
    assert result.payload["cache_only"] is True
    assert result.payload["coverage_satisfied"] is True
    assert len(result.payload["facts"]) == 3
    assert all(fact["source_urls"] == [source_url] for fact in result.payload["facts"])
    assert any(source.get("cached") for source in result.grounding_sources if source["url"] == source_url)


@pytest.mark.asyncio
async def test_incomplete_cached_evidence_falls_through_to_web_discovery(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://cached.example/royal-gate"
    web_url = "https://fresh.example/royal-gate"

    class OneShotWeb:
        def __init__(self):
            self.calls = 0
        async def execute(self, operation, call):
            assert operation == "web_search"
            self.calls += 1
            return await call("key-a", 10)

    web = OneShotWeb()
    client.web_search_routes = [("gemini-test", "test", None, web)]

    semantic = PassingResearchExecutor()
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], semantic)]

    generate_calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal generate_calls
        generate_calls += 1
        prompt = str(contents[0])
        if "LLM-арбитр evidence Street Story" in prompt:
            bound_ref = client._support_evidence_ref(
                web_url,
                {
                    "kind": "google_grounding",
                    "source_url": web_url,
                    "text": "Слева изображён Отакар II.",
                },
            )
            return SimpleNamespace(
                text=json.dumps({
                    "bindings": [{"fact_index": 0, "evidence_refs": [bound_ref]}],
                }, ensure_ascii=False),
                candidates=[],
            )
        if operation == "grounded_research":
            cached_ref = client._support_evidence_ref(
                source_url,
                {
                    "kind": "search_snippet",
                    "source_url": source_url,
                    "text": "Отакар II, Фридрих I и Альбрехт I.",
                },
            )
            payload = {
                "summary": "Cached evidence gives names but not positions.",
                "official_source_urls": [],
                "facts": [{
                    "claim_key": "three-names",
                    "existing_fact_id": "",
                    "text": "На фасаде изображены Отакар II, Фридрих I и Альбрехт I.",
                    "confidence": .9,
                    "source_urls": [source_url],
                    "evidence_refs": [cached_ref],
                }],
                "coverage_satisfied": False,
                "read_source_urls": [],
            }
            return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])
        payload = {
            "summary": "Fresh grounded result.",
            "coverage_satisfied": True,
            "missing_aspects": [],
            "continuation_needed": False,
            "continuation_reason": "",
            "official_source_urls": [],
            "facts": [{
                "claim_key": "left",
                "existing_fact_id": "",
                "text": "Слева изображён Отакар II.",
                "confidence": .95,
                "source_urls": [web_url],
            }],
        }
        chunk = SimpleNamespace(web=SimpleNamespace(uri=web_url, title="Fresh"))
        support = SimpleNamespace(
            segment=SimpleNamespace(text="Слева изображён Отакар II."),
            grounding_chunk_indices=[0],
        )
        candidate = SimpleNamespace(
            grounding_metadata=SimpleNamespace(
                grounding_chunks=[chunk],
                grounding_supports=[support],
            )
        )
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[candidate])

    client._generate = generate
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {
            "coverage_goal": "Кто изображён слева, в центре и справа?",
            "previously_processed_sources": [{
                "url": source_url,
                "title": "Cached",
                "supports": [{
                    "kind": "search_snippet",
                    "source_url": source_url,
                    "text": "Отакар II, Фридрих I и Альбрехт I.",
                }],
            }],
        },
    )
    assert web.calls == 1
    assert generate_calls >= 2
    assert result.payload["facts"][0]["source_urls"] == [web_url]


def test_merge_evidence_sources_preserves_all_passages_for_same_url(tmp_path):
    client = GeminiClient(config(tmp_path), Store(tmp_path / "street-story.sqlite3"))
    url = "https://example.org/many-passages"
    primary = [{
        "type": "web",
        "title": "Many",
        "url": url,
        "supports": [
            {"kind": "google_grounding", "source_url": url, "text": f"passage-{index}"}
            for index in range(8)
        ],
    }]
    merged = client._merge_evidence_sources(primary, [])
    assert len(merged) == 1
    assert [item["text"] for item in merged[0]["supports"]] == [
        f"passage-{index}" for index in range(8)
    ]


@pytest.mark.asyncio
async def test_fact_identity_reconciliation_scans_complete_inventory_pages(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], PassingResearchExecutor())]

    existing = [
        {
            "fact_id": f"fact-{index}",
            "claim_key": f"old-{index}",
            "text": (
                "Королевские ворота были открыты для посетителей после реставрации."
                if index == 84
                else f"Другой проверяемый факт номер {index}."
            ),
        }
        for index in range(85)
    ]
    incoming = [{
        "claim_key": "reopened-after-restoration",
        "text": "После реставрации Королевские ворота снова открыли для посетителей.",
    }]

    calls = 0
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        nonlocal calls
        calls += 1
        prompt = str(contents[0])
        if calls < 3:
            payload = {
                "matches": [{
                    "incoming_index": 0,
                    "equivalent": False,
                    "existing_fact_id": "",
                    "rationale": "На этой странице эквивалентного тезиса нет.",
                }],
            }
        else:
            assert "fact-84" in prompt
            payload = {
                "matches": [{
                    "incoming_index": 0,
                    "equivalent": True,
                    "existing_fact_id": "fact-84",
                    "rationale": "Это один и тот же тезис об открытии после реставрации.",
                }],
            }
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[])

    client._generate = generate
    result = await client.reconcile_fact_identities(incoming, existing, page_size=40)

    assert calls == 3
    assert result["pages_reviewed"] == 3
    assert result["existing_fact_count"] == 85
    assert result["matches"] == {0: "fact-84"}
    assert result["unmatched_count"] == 0
    assert result["decisions"] == [{
        "incoming_index": 0,
        "relation": "equivalent",
        "existing_fact_id": "fact-84",
        "rationale": "Это один и тот же тезис об открытии после реставрации.",
        "model_name": route[0],
        "prompt_version": "fact-identity-reconciliation-v1",
    }]


@pytest.mark.asyncio
async def test_grounded_search_is_fail_soft_per_fact_and_reports_rejections(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://museum.example/royal-gate"

    class OneShotExecutor:
        async def execute(self, operation, call):
            assert operation == "web_search"
            return await call("key-a", 10)

    client.web_search_routes = [
        ("gemini-test", "test", None, OneShotExecutor()),
    ]

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        if operation == "grounded_research":
            bound_ref = client._support_evidence_ref(
                source_url,
                {
                    "kind": "google_grounding",
                    "source_url": source_url,
                    "text": "Слева изображён Оттокар II.",
                },
            )
            return SimpleNamespace(
                text=json.dumps({
                    "bindings": [
                        {"fact_index": 0, "evidence_refs": [bound_ref]},
                        {"fact_index": 1, "evidence_refs": []},
                    ],
                }, ensure_ascii=False),
                candidates=[],
            )
        payload = {
            "summary": "Grounded facts",
            "coverage_satisfied": True,
            "missing_aspects": [],
            "continuation_needed": False,
            "continuation_reason": "",
            "official_source_urls": [source_url],
            "facts": [
                {
                    "claim_key": "",
                    "text": "Слева изображён Оттокар II.",
                    "confidence": "not-a-number",
                    "source_urls": [source_url],
                },
                {
                    "claim_key": "unsupported",
                    "text": "Этот факт ссылается на URL, которого не было в grounding.",
                    "confidence": .9,
                    "source_urls": ["https://invented.example/nope"],
                },
            ],
        }
        web = SimpleNamespace(uri=source_url, title="Museum")
        chunk = SimpleNamespace(web=web)
        support = SimpleNamespace(
            segment=SimpleNamespace(text="Слева изображён Оттокар II."),
            grounding_chunk_indices=[0],
        )
        metadata = SimpleNamespace(grounding_chunks=[chunk], grounding_supports=[support])
        candidate = SimpleNamespace(grounding_metadata=metadata)
        return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[candidate])

    client._generate = generate
    result = await client.search_web("скульптуры Королевских ворот", {"coverage_goal": "Кто изображён слева?"})
    assert [fact["text"] for fact in result.payload["facts"]] == ["Слева изображён Оттокар II."]
    assert result.payload["facts"][0]["claim_key"].startswith("exact-text:")
    audit = result.payload["extraction_audit"]
    assert audit == {
        "raw_fact_count": 2,
        "accepted_fact_count": 1,
        "claim_key_fallback_count": 1,
        "confidence_defaulted_count": 1,
        "rejected": {"no_bound_evidence": 1},
        "evidence_binding_status": "bound",
        "native_continuation_batches": 0,
    }


@pytest.mark.asyncio
async def test_native_search_continues_fact_extraction_without_second_google_search(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://museum.example/royal-gate"

    executor = PassingSearchExecutor()
    client.web_search_routes = [("gemini-test", "test", None, executor)]

    first_support = {
        "kind": "google_grounding",
        "source_url": source_url,
        "text": "Слева изображён Оттокар II.",
    }
    second_support = {
        "kind": "google_grounding",
        "source_url": source_url,
        "text": "В центре изображён Фридрих I.",
    }
    first_ref = client._support_evidence_ref(source_url, first_support)
    second_ref = client._support_evidence_ref(source_url, second_support)
    operations = []

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        del key, timeout, model, quota
        operations.append(operation)
        schema = getattr(config, "response_json_schema", None)
        if operation == "web_search":
            payload = {
                "summary": "First batch",
            "coverage_satisfied": True,
            "missing_aspects": [],
                "official_source_urls": [],
                "facts": [{
                    "claim_key": "left",
                    "text": "Слева изображён Оттокар II.",
                    "confidence": .96,
                    "source_urls": [source_url],
                }],
                "continuation_needed": True,
                "continuation_reason": "В grounding остались отдельные факты.",
            }
            web = SimpleNamespace(uri=source_url, title="Museum")
            chunk = SimpleNamespace(web=web)
            supports = [
                SimpleNamespace(
                    segment=SimpleNamespace(text=first_support["text"]),
                    grounding_chunk_indices=[0],
                ),
                SimpleNamespace(
                    segment=SimpleNamespace(text=second_support["text"]),
                    grounding_chunk_indices=[0],
                ),
            ]
            return SimpleNamespace(
                text=json.dumps(payload, ensure_ascii=False),
                candidates=[SimpleNamespace(
                    grounding_metadata=SimpleNamespace(
                        grounding_chunks=[chunk],
                        grounding_supports=supports,
                    )
                )],
            )
        if schema == client.EVIDENCE_BINDING_SCHEMA:
            return SimpleNamespace(
                text=json.dumps({
                    "bindings": [{
                        "fact_index": 0,
                        "evidence_refs": [first_ref],
                    }],
                }, ensure_ascii=False),
                candidates=[],
            )
        if schema == client.COVERAGE_REVIEW_SCHEMA:
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": True,
                    "summary": "Левая и центральная фигуры закрыты.",
                    "missing_aspects": [],
                    "coverage_items": [
                        {
                            "requirement": "Кто изображён слева.",
                            "satisfied": True,
                            "fact_indices": [0],
                            "evidence_refs": [first_ref],
                            "rationale": "Левый факт доказан.",
                        },
                        {
                            "requirement": "Кто изображён в центре.",
                            "satisfied": True,
                            "fact_indices": [1],
                            "evidence_refs": [second_ref],
                            "rationale": "Центральный факт доказан.",
                        },
                    ],
                    "read_source_urls": [],
                }, ensure_ascii=False),
                candidates=[],
            )
        assert schema == client.NATIVE_CONTINUATION_SCHEMA
        prompt = str(contents[0])
        assert first_ref in prompt and second_ref in prompt
        assert "Слева изображён Оттокар II." in prompt
        return SimpleNamespace(
            text=json.dumps({
                "facts": [{
                    "claim_key": "center",
                    "text": "В центре изображён Фридрих I.",
                    "confidence": .95,
                    "source_urls": [source_url],
                    "evidence_refs": [second_ref],
                }],
                "continuation_needed": False,
                "continuation_reason": "",
            }, ensure_ascii=False),
            candidates=[],
        )

    client._generate = generate
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {"coverage_goal": "Кто изображён слева и в центре?"},
    )

    assert executor.calls == 1
    assert operations == ["web_search", "grounded_research", "grounded_research", "grounded_research"]
    assert [fact["text"] for fact in result.payload["facts"]] == [
        "Слева изображён Оттокар II.",
        "В центре изображён Фридрих I.",
    ]
    assert result.payload["extraction_complete"] is True
    assert result.payload["continuation_reason"] == ""
    audit = result.payload["extraction_audit"]
    assert audit["native_continuation_batches"] == 1
    assert audit["accepted_fact_count"] == 2


@pytest.mark.asyncio
async def test_native_search_marks_stalled_continuation_incomplete(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://museum.example/royal-gate"
    executor = PassingSearchExecutor()
    client.web_search_routes = [("gemini-test", "test", None, executor)]

    support = {
        "kind": "google_grounding",
        "source_url": source_url,
        "text": "Слева изображён Оттокар II.",
    }
    evidence_ref = client._support_evidence_ref(source_url, support)

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        del key, timeout, contents, model, quota
        schema = getattr(config, "response_json_schema", None)
        if operation == "web_search":
            payload = {
                "summary": "Needs continuation",
            "coverage_satisfied": True,
            "missing_aspects": [],
                "official_source_urls": [],
                "facts": [{
                    "claim_key": "left",
                    "text": "Слева изображён Оттокар II.",
                    "confidence": .96,
                    "source_urls": [source_url],
                }],
                "continuation_needed": True,
                "continuation_reason": "Есть ещё факты.",
            }
            web = SimpleNamespace(uri=source_url, title="Museum")
            chunk = SimpleNamespace(web=web)
            ground = SimpleNamespace(
                segment=SimpleNamespace(text=support["text"]),
                grounding_chunk_indices=[0],
            )
            return SimpleNamespace(
                text=json.dumps(payload, ensure_ascii=False),
                candidates=[SimpleNamespace(
                    grounding_metadata=SimpleNamespace(
                        grounding_chunks=[chunk],
                        grounding_supports=[ground],
                    )
                )],
            )
        if schema == client.EVIDENCE_BINDING_SCHEMA:
            return SimpleNamespace(
                text=json.dumps({
                    "bindings": [{
                        "fact_index": 0,
                        "evidence_refs": [evidence_ref],
                    }],
                }, ensure_ascii=False),
                candidates=[],
            )
        if schema == client.COVERAGE_REVIEW_SCHEMA:
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": False,
                    "summary": "Один факт не закрывает весь фасад.",
                    "missing_aspects": ["Остальные факты о фасаде."],
                    "coverage_items": [{
                        "requirement": "Собрать все запрошенные факты о фасаде.",
                        "satisfied": False,
                        "fact_indices": [0],
                        "evidence_refs": [evidence_ref],
                        "rationale": "Есть только один факт при незавершённой extraction.",
                    }],
                    "read_source_urls": [],
                }, ensure_ascii=False),
                candidates=[],
            )
        assert schema == client.NATIVE_CONTINUATION_SCHEMA
        return SimpleNamespace(
            text=json.dumps({
                "facts": [],
                "continuation_needed": True,
                "continuation_reason": "Модель считает, что факты остались, но не вернула новый batch.",
            }, ensure_ascii=False),
            candidates=[],
        )

    client._generate = generate
    result = await client.search_web(
        "скульптуры Королевских ворот",
        {"coverage_goal": "Собери все факты о фасаде."},
    )

    assert executor.calls == 1
    assert result.payload["extraction_complete"] is False
    assert result.payload["extraction_audit"]["native_continuation_batches"] == 1
    assert result.payload["extraction_audit"]["rejected"]["native_continuation_stalled"] == 1
    assert len(result.payload["facts"]) == 1


@pytest.mark.asyncio
async def test_native_grounded_fact_binds_only_specific_support_on_same_url(tmp_path):
    settings = replace(
        config(tmp_path),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
    )
    client = GeminiClient(settings, Store(tmp_path / "street-story.sqlite3"))
    source_url = "https://museum.example/royal-gate"

    class OneShotExecutor:
        async def execute(self, operation, call):
            assert operation == "web_search"
            return await call("key-a", 10)

    client.web_search_routes = [("gemini-test", "test", None, OneShotExecutor())]

    relevant = {
        "kind": "google_grounding",
        "source_url": source_url,
        "text": "Слева изображён Оттокар II.",
    }
    irrelevant = {
        "kind": "google_grounding",
        "source_url": source_url,
        "text": "Музей работает со среды по воскресенье.",
    }
    relevant_ref = client._support_evidence_ref(source_url, relevant)
    irrelevant_ref = client._support_evidence_ref(source_url, irrelevant)

    async def generate(key, timeout, contents, config=None, *, operation="web_search", model=None, quota=None):
        if operation == "grounded_research":
            prompt = str(contents[0])
            assert relevant_ref in prompt and irrelevant_ref in prompt
            return SimpleNamespace(
                text=json.dumps({
                    "bindings": [{
                        "fact_index": 0,
                        "evidence_refs": [relevant_ref],
                    }],
                }, ensure_ascii=False),
                candidates=[],
            )

        payload = {
            "summary": "Grounded fact.",
            "coverage_satisfied": True,
            "missing_aspects": [],
            "continuation_needed": False,
            "continuation_reason": "",
            "official_source_urls": [],
            "facts": [{
                "claim_key": "left-sculpture",
                "text": "Слева изображён Оттокар II.",
                "confidence": .97,
                "source_urls": [source_url],
            }],
        }
        web = SimpleNamespace(uri=source_url, title="Museum")
        chunks = [SimpleNamespace(web=web)]
        supports = [
            SimpleNamespace(
                segment=SimpleNamespace(text=relevant["text"]),
                grounding_chunk_indices=[0],
            ),
            SimpleNamespace(
                segment=SimpleNamespace(text=irrelevant["text"]),
                grounding_chunk_indices=[0],
            ),
        ]
        return SimpleNamespace(
            text=json.dumps(payload, ensure_ascii=False),
            candidates=[SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=chunks,
                    grounding_supports=supports,
                )
            )],
        )

    client._generate = generate
    result = await client.search_web(
        "кто изображён слева на Королевских воротах",
        {"coverage_goal": "Кто изображён слева?"},
    )

    assert len(result.payload["facts"]) == 1
    fact = result.payload["facts"][0]
    assert fact["evidence_refs"] == [relevant_ref]
    source = next(item for item in result.grounding_sources if item["url"] == source_url)
    assert {item["evidence_ref"] for item in source["supports"]} == {
        relevant_ref,
        irrelevant_ref,
    }
    # Durable provider output retains all passages; the fact binds only the one
    # the semantic model selected.
    assert irrelevant_ref not in fact["evidence_refs"]


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
            "coverage_satisfied": True,
            "missing_aspects": [],
            "continuation_needed": False,
            "continuation_reason": "",
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
