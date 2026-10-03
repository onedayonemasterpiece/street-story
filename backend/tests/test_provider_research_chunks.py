from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from street_story.config import Settings
from street_story.db import Store
from street_story.providers import GeminiClient, GroundedResearch
from street_story.research_runs import begin_research_run, manifest_complete, run_manifest


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


class PageHTTP:
    def __init__(self,url,html):
        self.url=url
        self.html=html
        self.calls=0
    async def get(self,url,**kwargs):
        self.calls+=1
        request=httpx.Request("GET",url)
        if url==self.url:
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
    async def generate(key,timeout,contents,config=None,*,operation="grounded_research",model=None,quota=None):
        assert operation=="grounded_research"
        prompt=str(contents[0])
        call_log.append(prompt)
        if prompt.startswith("Ты проверяешь полноту"):
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "coverage_satisfied":True,
                        "summary":"Оба аспекта покрыты.",
                        "missing_aspects":[],
                    },
                    ensure_ascii=False,
                ),
                candidates=[],
            )
        if "Передан один chunk документа" in prompt:
            chunk_id=prompt.split("Chunk id: ",1)[1].split("\n",1)[0]
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
                }
            else:
                payload={"facts":[],"needs_context":False,"context_reason":""}
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
    assert len(call_log)==result.payload["page_chunk_count"]+2
