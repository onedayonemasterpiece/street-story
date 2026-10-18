from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story.config import Settings
from street_story.db import Store
from street_story.gemini import GeminiUnavailable
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_runs import begin_research_run, manifest_complete, run_manifest


@pytest.fixture(autouse=True)
def public_article_network(monkeypatch):
    """Keep public-reader validation while isolating reserved test-domain DNS."""
    from street_story import article_media
    cached_page = article_media.cached_public_page

    async def resolve_fixture(host):
        assert host == 'history.example'
        return '93.184.216.34'

    async def fixture_page(store, client, raw, **kwargs):
        kwargs.setdefault('resolver', resolve_fixture)
        return await cached_page(store, client, raw, **kwargs)

    monkeypatch.setattr(article_media, 'cached_public_page', fixture_page)


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        device_token=SecretStr("device"),
        gemini_api_key=SecretStr("key-a"),
        gemini_api_keys=(SecretStr("key-a"),),
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url=None,
        vibepublish_bearer_token=None,
        osm_user_agent="StreetStory provider research tests",
        worker_poll_seconds=.01,
    )


def create_story(store: Store) -> str:
    story_id="story-provider-chunks"
    now=store.now()
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,visual_context_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'{}','{}',?,?)",
            (
                story_id,
                "client-provider-chunks",
                "a"*64,
                "image/jpeg",
                "/tmp/no-photo.jpg",
                "voice-chunks-v2",
                "identity_ready",
                now,
                now,
            ),
        )
    return story_id


class ResearchExecutor:
    async def execute(self, operation, call):
        assert operation=="grounded_research"
        return await call("key-a",20)


class PageHTTP(httpx.AsyncClient):
    def __init__(self,url,html):
        self.url=url
        self.html=html
        self.calls=0
        super().__init__(transport=httpx.MockTransport(self.page_response), follow_redirects=False)

    async def page_response(self,request):
        self.calls+=1
        # fetch_public must retain its DNS pin, logical host and TLS identity.
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'history.example'
        assert request.extensions['sni_hostname'] == 'history.example'
        logical_url = str(request.url.copy_with(host=request.headers['host']))
        if logical_url==self.url:
            return httpx.Response(
                200,
                text=self.html,
                headers={"content-type":"text/html; charset=utf-8"},
                request=request,
            )
        return httpx.Response(404,text="no",headers={"content-type":"text/plain"},request=request)


@pytest.mark.asyncio
async def test_chunked_page_extraction_preserves_pass_one_and_tail_fact_with_exact_span(tmp_path):
    store=Store(tmp_path/"db.sqlite3")
    story_id=create_story(store)
    now=store.now()
    with store.tx() as db:
        run_id=begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="Найти вводный факт и хвостовой факт",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-provider-chunks",
            now=now,
        )

    url="https://history.example/long-gate"
    filler="<p>"+("Длинный исторический контекст без целевого факта. "*500)+"</p>"
    tail_text="ХВОСТОВОЙ ФАКТ: справа изображён герцог Альбрехт I."
    html=f"<html><body><main><p>ВВОДНЫЙ ФАКТ: ворота являются историческим памятником.</p>{filler}<p>{tail_text}</p></main></body></html>"

    client=GeminiClient(settings(tmp_path),store)
    client.search_http=PageHTTP(url,html)
    route=client.research_routes[0]
    client.research_routes=[(route[0],route[1],route[2],ResearchExecutor())]

    call_log=[]
    last_chunk_id=None
    async def generate(key,timeout,contents,config=None,*,operation="grounded_research",model=None,quota=None):
        nonlocal last_chunk_id
        assert operation=="grounded_research"
        prompt=str(contents[0])
        call_log.append(prompt)
        if prompt.startswith("Ты проверяешь полноту"):
            snippet_ref=client._support_evidence_ref(
                url,
                {
                    "kind":"search_snippet",
                    "source_url":url,
                    "text":"Королевские ворота — исторический памятник; подробности на странице.",
                },
            )
            if tail_text not in prompt:
                payload={
                    "coverage_satisfied":False,
                    "summary":"Вводный факт закрыт, правая фигура ещё не установлена.",
                    "missing_aspects":["Кто изображён справа."],
                    "coverage_items":[
                        {
                            "requirement":"Подтвердить вводный факт.",
                            "satisfied":True,
                            "fact_indices":[0],
                            "evidence_refs":[snippet_ref],
                            "rationale":"Snippet прямо поддерживает вводный факт.",
                        },
                        {
                            "requirement":"Установить, кто изображён справа.",
                            "satisfied":False,
                            "fact_indices":[],
                            "evidence_refs":[],
                            "rationale":"В snippet нет имени правой фигуры.",
                        },
                    ],
                    "read_source_urls":[url],
                }
            else:
                assert last_chunk_id
                payload={
                    "coverage_satisfied":True,
                    "summary":"Оба аспекта покрыты.",
                    "missing_aspects":[],
                    "coverage_items":[
                        {
                            "requirement":"Подтвердить вводный факт.",
                            "satisfied":True,
                            "fact_indices":[0],
                            "evidence_refs":[snippet_ref],
                            "rationale":"Snippet прямо поддерживает вводный факт.",
                        },
                        {
                            "requirement":"Установить, кто изображён справа.",
                            "satisfied":True,
                            "fact_indices":[1],
                            "evidence_refs":[last_chunk_id],
                            "rationale":"Tail chunk прямо называет правую фигуру.",
                        },
                    ],
                    "read_source_urls":[],
                }
            return SimpleNamespace(text=json.dumps(payload,ensure_ascii=False),candidates=[])
        if "Передан один chunk документа" in prompt:
            chunk_id=prompt.split("Chunk id: ",1)[1].split("\n",1)[0]
            last_chunk_id=chunk_id
            source_url=prompt.split("Source URL: ",1)[1].split("\n",1)[0]
            if tail_text in prompt:
                payload={
                    "facts":[{
                        "claim_key":"right-sculpture",
                        "existing_fact_id":"",
                        "text":"Справа на фасаде изображён герцог Альбрехт I.",
                        "confidence":.97,
                        "source_urls":[source_url],
                        "evidence_spans":[{
                            "source_url":source_url,
                            "chunk_id":chunk_id,
                            "quote":tail_text,
                        }],
                    }],
                    "needs_context":False,
                    "context_reason":"",
                    "continuation_needed":False,
                    "continuation_reason":"",
                }
            else:
                payload={"facts":[],"needs_context":False,"context_reason":"","continuation_needed":False,"continuation_reason":""}
            return SimpleNamespace(text=json.dumps(payload,ensure_ascii=False),candidates=[])

        snippet_ref=client._support_evidence_ref(
            url,
            {
                "kind":"search_snippet",
                "source_url":url,
                "text":"Королевские ворота — исторический памятник; подробности на странице.",
            },
        )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "summary":"Snippet gives one preliminary fact; page is needed for the tail.",
                    "official_source_urls":[],
                    "facts":[{
                        "claim_key":"heritage",
                        "existing_fact_id":"",
                        "text":"Королевские ворота являются историческим памятником.",
                        "confidence":.8,
                        "source_urls":[url],
                        "evidence_refs":[snippet_ref],
                    }],
                    "coverage_satisfied":False,
                    "read_source_urls":[url],
                },
                ensure_ascii=False,
            ),
            candidates=[],
        )

    client._generate=generate
    discovery=GroundedResearch(
        payload={
            "summary":"Discovery",
            "official_source_urls":[],
            "facts":[],
            "search_provider":"duckduckgo_html_fallback",
        },
        grounding_sources=[{
            "type":"web_search",
            "title":"Long Gate",
            "url":url,
            "supports":[{
                "kind":"search_snippet",
                "source_url":url,
                "text":"Королевские ворота — исторический памятник; подробности на странице.",
            }],
        }],
    )
    result=await client._semantic_complete_discovery(
        "Королевские ворота история и фигуры",
        {
            "research_run_id":run_id,
            "coverage_goal":"Найти вводный факт и кто изображён справа.",
            "known_facts":[],
            "previously_considered_poi_facts":[],
            "previously_processed_sources":[],
        },
        discovery,
    )

    facts=result.payload["facts"]
    assert any(f["claim_key"]=="heritage" for f in facts)
    tail=next(f for f in facts if f["claim_key"]=="right-sculpture")
    assert tail["evidence_spans"][0]["quote"]==tail_text
    assert tail["evidence_spans"][0]["span_start"]>9000
    assert result.payload["coverage_satisfied"] is True
    assert result.payload["page_chunk_count"]>1
    assert result.payload["page_chunk_failures"]==0

    exact_supports=[
        support
        for source in result.grounding_sources
        for support in source.get("supports") or []
        if support.get("kind")=="verified_page_span"
    ]
    assert len(exact_supports)==1
    assert exact_supports[0]["text"]==tail_text
    assert "Длинный исторический контекст" not in exact_supports[0]["text"]

    with store.connection() as db:
        manifest=run_manifest(db,run_id)
    assert manifest["counts"]["chunks_planned"]==result.payload["page_chunk_count"]
    assert manifest["counts"]["chunks_completed"]==result.payload["page_chunk_count"]
    assert manifest_complete(manifest) is True
    # extractor + pre-page coverage review + one call per chunk + post-page coverage review
    assert len(call_log)==result.payload["page_chunk_count"]+3


@pytest.mark.asyncio
async def test_dense_chunk_continues_until_all_facts_are_extracted(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:dense",
            goal="Извлечь все сорок фактов",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-dense-continuation",
            now=now,
        )

    url = "https://history.example/dense"
    quotes = [f"ФАКТ {index:02d}: значение {index}." for index in range(40)]
    html = "<html><body><main><p>" + " ".join(quotes) + "</p></main></body></html>"

    client = GeminiClient(settings(tmp_path), store)
    client.search_http = PageHTTP(url, html)
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], ResearchExecutor())]

    chunk_calls = []
    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        prompt = str(contents[0])
        if prompt.startswith("Ты проверяешь полноту"):
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": True,
                    "summary": "Все сорок фактов извлечены.",
                    "missing_aspects": [],
                }, ensure_ascii=False),
                candidates=[],
            )
        if "Передан один chunk документа" in prompt:
            batch_index = int(prompt.split("Continuation batch index: ", 1)[1].split("\n", 1)[0])
            chunk_id = prompt.split("Chunk id: ", 1)[1].split("\n", 1)[0]
            source_url = prompt.split("Source URL: ", 1)[1].split("\n", 1)[0]
            chunk_calls.append(batch_index)
            if batch_index == 0:
                indexes = range(32)
                continuation_needed = True
                continuation_reason = "В core остаются ещё восемь атомарных фактов."
            else:
                assert "ФАКТ 31: значение 31." in prompt
                indexes = range(32, 40)
                continuation_needed = False
                continuation_reason = ""
            facts = [{
                "claim_key": f"dense-{index}",
                "existing_fact_id": "",
                "text": quotes[index],
                "confidence": .95,
                "source_urls": [source_url],
                "evidence_spans": [{
                    "source_url": source_url,
                    "chunk_id": chunk_id,
                    "quote": quotes[index],
                }],
            } for index in indexes]
            return SimpleNamespace(
                text=json.dumps({
                    "facts": facts,
                    "needs_context": False,
                    "context_reason": "",
                    "continuation_needed": continuation_needed,
                    "continuation_reason": continuation_reason,
                }, ensure_ascii=False),
                candidates=[],
            )
        return SimpleNamespace(
            text=json.dumps({
                "summary": "Нужно прочитать страницу.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [url],
            }, ensure_ascii=False),
            candidates=[],
        )

    client._generate = generate
    discovery = GroundedResearch(
        payload={
            "summary": "Discovery",
            "official_source_urls": [],
            "facts": [],
            "search_provider": "duckduckgo_html_fallback",
        },
        grounding_sources=[{
            "type": "web_search",
            "title": "Dense",
            "url": url,
            "supports": [{
                "kind": "search_snippet",
                "source_url": url,
                "text": "Страница содержит подробный перечень фактов.",
            }],
        }],
    )

    result = await client._semantic_complete_discovery(
        "dense facts",
        {
            "research_run_id": run_id,
            "coverage_goal": "Извлечь все сорок фактов.",
            "known_facts": [],
            "previously_considered_poi_facts": [],
            "previously_processed_sources": [],
        },
        discovery,
    )

    assert chunk_calls == [0, 1]
    assert len(result.payload["facts"]) == 40
    assert result.payload["page_fact_count"] == 40
    assert result.payload["page_continuation_batches"] == 1
    assert result.payload["page_chunk_deferred"] == 0
    assert {fact["text"] for fact in result.payload["facts"]} == set(quotes)

    with store.connection() as db:
        manifest = run_manifest(db, run_id)
    assert manifest["counts"]["chunk_batches_total"] == 2
    assert manifest["counts"]["chunk_batches_continuation"] == 1
    assert manifest["counts"]["chunk_batches_failed"] == 0
    assert manifest["counts"]["chunk_batches_deferred"] == 0
    assert manifest["counts"]["chunks_completed"] == 1
    assert manifest["chunk_batches"][0]["accepted_fact_count"] == 32
    assert manifest["chunk_batches"][1]["accepted_fact_count"] == 8
    assert manifest_complete(manifest) is True


@pytest.mark.asyncio
async def test_failed_continuation_preserves_prior_batch_facts_and_marks_run_partial(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:partial",
            goal="Не потерять первый пакет",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-continuation-failure",
            now=now,
        )

    url = "https://history.example/partial-continuation"
    quote = "ФАКТ A: подтверждённый первый пакет."
    html = f"<html><body><main><p>{quote}</p><p>" + ("Дополнительный текст. " * 30) + "</p></main></body></html>"

    client = GeminiClient(settings(tmp_path), store)
    client.search_http = PageHTTP(url, html)
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], ResearchExecutor())]

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        prompt = str(contents[0])
        if prompt.startswith("Ты проверяешь полноту"):
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": False,
                    "summary": "Продолжение не завершилось.",
                    "missing_aspects": ["continuation_failed"],
                }, ensure_ascii=False),
                candidates=[],
            )
        if "Передан один chunk документа" in prompt:
            batch_index = int(prompt.split("Continuation batch index: ", 1)[1].split("\n", 1)[0])
            if batch_index == 1:
                raise GeminiUnavailable(None, "continuation batch unavailable")
            chunk_id = prompt.split("Chunk id: ", 1)[1].split("\n", 1)[0]
            source_url = prompt.split("Source URL: ", 1)[1].split("\n", 1)[0]
            return SimpleNamespace(
                text=json.dumps({
                    "facts": [{
                        "claim_key": "fact-a",
                        "existing_fact_id": "",
                        "text": quote,
                        "confidence": .95,
                        "source_urls": [source_url],
                        "evidence_spans": [{
                            "source_url": source_url,
                            "chunk_id": chunk_id,
                            "quote": quote,
                        }],
                    }],
                    "needs_context": False,
                    "context_reason": "",
                    "continuation_needed": True,
                    "continuation_reason": "Есть ещё факты.",
                }, ensure_ascii=False),
                candidates=[],
            )
        return SimpleNamespace(
            text=json.dumps({
                "summary": "Нужно прочитать страницу.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [url],
            }, ensure_ascii=False),
            candidates=[],
        )

    client._generate = generate
    discovery = GroundedResearch(
        payload={
            "summary": "Discovery",
            "official_source_urls": [],
            "facts": [],
            "search_provider": "duckduckgo_html_fallback",
        },
        grounding_sources=[{
            "type": "web_search",
            "title": "Partial",
            "url": url,
            "supports": [{
                "kind": "search_snippet",
                "source_url": url,
                "text": "Страница содержит несколько фактов.",
            }],
        }],
    )

    result = await client._semantic_complete_discovery(
        "partial continuation",
        {
            "research_run_id": run_id,
            "coverage_goal": "Извлечь все факты.",
            "known_facts": [],
            "previously_considered_poi_facts": [],
            "previously_processed_sources": [],
        },
        discovery,
    )

    assert [fact["text"] for fact in result.payload["facts"]] == [quote]
    assert result.payload["page_chunk_failures"] == 1
    assert result.payload["page_continuation_batches"] == 1

    with store.connection() as db:
        manifest = run_manifest(db, run_id)
    assert manifest["counts"]["chunk_batches_total"] == 2
    assert manifest["counts"]["chunk_batches_continuation"] == 1
    assert manifest["counts"]["chunk_batches_failed"] == 1
    assert manifest["chunks"][0]["status"] == "failed"
    assert manifest["chunks"][0]["observation_count"] == 1
    assert manifest_complete(manifest) is False


@pytest.mark.asyncio
async def test_continuation_limit_marks_chunk_deferred_instead_of_silent_success(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:limit",
            goal="Проверить continuation limit",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-continuation-limit",
            now=now,
        )

    url = "https://history.example/continuation-limit"
    quotes = [f"ЛИМИТ ФАКТ {index}." for index in range(6)]
    html = "<html><body><main><p>" + " ".join(quotes) + "</p></main></body></html>"

    client = GeminiClient(settings(tmp_path), store)
    client.search_http = PageHTTP(url, html)
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], ResearchExecutor())]

    async def generate(key, timeout, contents, config=None, *, operation="grounded_research", model=None, quota=None):
        prompt = str(contents[0])
        if prompt.startswith("Ты проверяешь полноту"):
            return SimpleNamespace(
                text=json.dumps({
                    "coverage_satisfied": False,
                    "summary": "Continuation budget exhausted.",
                    "missing_aspects": ["continuation_limit"],
                }),
                candidates=[],
            )
        if "Передан один chunk документа" in prompt:
            batch_index = int(
                prompt.partition("Continuation batch index: ")[2].splitlines()[0]
            )
            chunk_id = prompt.partition("Chunk id: ")[2].splitlines()[0]
            source_url = prompt.partition("Source URL: ")[2].splitlines()[0]
            quote = quotes[batch_index]
            return SimpleNamespace(
                text=json.dumps({
                    "facts": [{
                        "claim_key": f"limit-{batch_index}",
                        "existing_fact_id": "",
                        "text": quote,
                        "confidence": .9,
                        "source_urls": [source_url],
                        "evidence_spans": [{
                            "source_url": source_url,
                            "chunk_id": chunk_id,
                            "quote": quote,
                        }],
                    }],
                    "needs_context": False,
                    "context_reason": "",
                    "continuation_needed": True,
                    "continuation_reason": "Модель заявляет, что факты ещё остались.",
                }, ensure_ascii=False),
                candidates=[],
            )
        return SimpleNamespace(
            text=json.dumps({
                "summary": "Нужно прочитать страницу.",
                "official_source_urls": [],
                "facts": [],
                "coverage_satisfied": False,
                "read_source_urls": [url],
            }),
            candidates=[],
        )

    client._generate = generate
    discovery = GroundedResearch(
        payload={
            "summary": "Discovery",
            "official_source_urls": [],
            "facts": [],
            "search_provider": "duckduckgo_html_fallback",
        },
        grounding_sources=[{
            "type": "web_search",
            "title": "Limit",
            "url": url,
            "supports": [{
                "kind": "search_snippet",
                "source_url": url,
                "text": "Страница содержит плотный набор фактов.",
            }],
        }],
    )

    result = await client._semantic_complete_discovery(
        "continuation limit",
        {
            "research_run_id": run_id,
            "coverage_goal": "Извлечь весь плотный набор.",
            "known_facts": [],
            "previously_considered_poi_facts": [],
            "previously_processed_sources": [],
        },
        discovery,
    )

    assert len(result.payload["facts"]) == 6
    assert result.payload["page_continuation_batches"] == 5
    assert result.payload["page_chunk_deferred"] == 1

    with store.connection() as db:
        manifest = run_manifest(db, run_id)
    assert manifest["counts"]["chunk_batches_total"] == 6
    assert manifest["counts"]["chunk_batches_continuation"] == 5
    assert manifest["counts"]["chunk_batches_deferred"] == 1
    assert manifest["chunks"][0]["status"] == "deferred"
    assert manifest["chunks"][0]["error_code"] == "continuation_limit"
    assert manifest_complete(manifest) is False


@pytest.mark.asyncio
async def test_chunk_checkpoint_resumes_next_batch_and_reuses_terminal_payload(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:resume",
            goal="Сохранить первый пакет и продолжить тот же chunk.",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-checkpoint-resume",
            now=now,
        )

    url = "https://history.example/checkpoint-resume"
    quote_a = "ФАКТ A: первый durable пакет."
    quote_b = "ФАКТ B: второй пакет после resume."
    filler = "Нейтральный исторический контекст документа. " * 8
    html = (
        "<html><body><main><p>"
        + quote_a
        + " "
        + quote_b
        + " " + filler + "</p></main></body></html>"
    )
    client = GeminiClient(settings(tmp_path), store)
    client.search_http = PageHTTP(url, html)
    route = client.research_routes[0]
    client.research_routes = [(route[0], route[1], route[2], ResearchExecutor())]

    phase = {"value": 1}
    chunk_calls: list[tuple[int, int]] = []

    async def coverage_review(
        api_key,
        timeout,
        *,
        coverage_goal,
        facts,
        sources,
        model=None,
        quota=None,
        allow_page_reads,
    ):
        has_both = {fact.get("text") for fact in facts} >= {quote_a, quote_b}
        return {
            "coverage_satisfied": has_both,
            "coverage_items": [],
            "missing_aspects": [] if has_both else ["second_batch"],
            "summary": "Checkpoint resume.",
            "read_source_urls": [url] if allow_page_reads and not has_both else [],
        }

    async def generate(
        key,
        timeout,
        contents,
        config=None,
        *,
        operation="grounded_research",
        model=None,
        quota=None,
    ):
        prompt = str(contents[0])
        if "Передан один chunk документа" in prompt:
            batch_index = int(
                prompt.partition("Continuation batch index: ")[2].splitlines()[0]
            )
            chunk_calls.append((phase["value"], batch_index))
            chunk_id = prompt.partition("Chunk id: ")[2].splitlines()[0]
            source_url = prompt.partition("Source URL: ")[2].splitlines()[0]
            if phase["value"] == 1 and batch_index == 1:
                raise GeminiUnavailable(None, "simulated second batch timeout")
            if phase["value"] >= 2 and batch_index == 0:
                raise AssertionError("durable batch 0 must not be extracted again")
            quote = quote_a if batch_index == 0 else quote_b
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "facts": [{
                            "claim_key": f"checkpoint-{batch_index}",
                            "existing_fact_id": "",
                            "text": quote,
                            "confidence": .96,
                            "source_urls": [source_url],
                            "evidence_spans": [{
                                "source_url": source_url,
                                "chunk_id": chunk_id,
                                "quote": quote,
                            }],
                        }],
                        "needs_context": False,
                        "context_reason": "",
                        "continuation_needed": batch_index == 0,
                        "continuation_reason": (
                            "Есть второй пакет." if batch_index == 0 else ""
                        ),
                    },
                    ensure_ascii=False,
                ),
                candidates=[],
            )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "summary": "Нужно прочитать страницу.",
                    "official_source_urls": [],
                    "facts": [],
                    "coverage_satisfied": False,
                    "read_source_urls": [url],
                },
                ensure_ascii=False,
            ),
            candidates=[],
        )

    client._generate = generate
    client._review_coverage_contract = coverage_review
    discovery = GroundedResearch(
        payload={
            "summary": "Discovery",
            "official_source_urls": [],
            "facts": [],
            "search_provider": "duckduckgo_html_fallback",
        },
        grounding_sources=[{
            "type": "web_search",
            "title": "Checkpoint",
            "url": url,
            "supports": [{
                "kind": "search_snippet",
                "source_url": url,
                "text": "Страница содержит два факта.",
            }],
        }],
    )
    context = {
        "research_run_id": run_id,
        "coverage_goal": "Извлечь оба факта.",
        "known_facts": [],
        "previously_considered_poi_facts": [],
        "previously_processed_sources": [],
    }

    first = await client._semantic_complete_discovery(
        "checkpoint resume",
        context,
        discovery,
    )
    with store.connection() as db:
        first_manifest = run_manifest(db, run_id)
    assert first_manifest["counts"]["chunk_batches_total"] == 2
    assert first_manifest["chunk_batches"][0]["payload_saved"] == 1
    assert first_manifest["chunk_batches"][0]["accepted_fact_count"] == 1
    assert first_manifest["chunk_batches"][1]["status"] == "failed"
    assert [fact["text"] for fact in first.payload["facts"]] == [quote_a]

    phase["value"] = 2
    second = await client._semantic_complete_discovery(
        "checkpoint resume",
        context,
        discovery,
    )
    assert [fact["text"] for fact in second.payload["facts"]] == [quote_a, quote_b]
    assert chunk_calls == [(1, 0), (1, 1), (2, 1)]
    with store.connection() as db:
        second_manifest = run_manifest(db, run_id)
    assert second_manifest["counts"]["chunk_batches_total"] == 2
    assert second_manifest["counts"]["chunk_batches_failed"] == 0
    assert second_manifest["counts"]["chunk_batches_payload_missing"] == 0
    assert second_manifest["chunks"][0]["status"] == "extracted"
    assert second_manifest["chunks"][0]["observation_count"] == 2
    assert manifest_complete(second_manifest) is True

    phase["value"] = 3
    third = await client._semantic_complete_discovery(
        "checkpoint resume",
        context,
        discovery,
    )
    assert [fact["text"] for fact in third.payload["facts"]] == [quote_a, quote_b]
    assert chunk_calls == [(1, 0), (1, 1), (2, 1)]
    with store.connection() as db:
        third_manifest = run_manifest(db, run_id)
    assert third_manifest["counts"]["chunk_batches_total"] == 2
    assert third_manifest["chunks"][0]["status"] == "extracted"
