from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from street_story.app import create_app
from street_story.config import Settings
from street_story.providers import GroundedResearch, OSMClient, RetryableProviderError, WikipediaClient, project_destinations
from street_story.service import ConflictError, ProviderBundle, StreetStoryService, stable_fact_id

PHOTO = b"street-story-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class FakeOSM:
    async def lookup(self, lat, lon):
        return {
            "reverse": {"display_name": "Дом Советов, Калининград", "osm_type": "way", "osm_id": 123, "namedetails": {"name": "Дом Советов"}},
            "nearby": [{"type": "node", "id": 456, "tags": {"name": "Площадь"}}],
        }


class FakeWikipedia:
    async def nearby(self, lat, lon):
        return [{"title": "Дом Советов", "extract": "Проверенный контекст", "url": "https://ru.wikipedia.org/wiki/Test"}]


class FakeGemini:
    def __init__(self, temporary_fail=False):
        self.temporary_fail = temporary_fail
        self.transcribe_order = []
        self.transcribe_calls = 0

    async def transcribe(self, path: Path, mime_type: str):
        self.transcribe_calls += 1
        token = path.read_bytes().decode()
        self.transcribe_order.append(token)
        return f"транскрипт-{token}"

    async def research(self, photo_path, photo_mime, transcript, place_context, wikipedia, previous_facts):
        if self.temporary_fail:
            raise RetryableProviderError("temporary Gemini outage")
        return GroundedResearch(
            payload={
                "place_name": "Дом Советов",
                "summary": "Короткое резюме",
                "draft_text": "Черновик публикации",
                "facts": [
                    {"text": "Дом Советов связан с центральной площадью", "confidence": 0.91, "source_urls": ["https://ru.wikipedia.org/wiki/Test"]},
                    {"text": "Неподтверждённая легенда", "confidence": 0.4, "source_urls": ["https://example.invalid/not-grounded"]},
                ],
            },
            grounding_sources=[{"type": "web", "title": "Grounded", "url": "https://grounded.example/source"}],
        )


class FakeVibePublish:
    def __init__(self, available=True, lose_first=False):
        self.available = available
        self.lose_first = lose_first
        self.publish_calls = []
        self.effects = {}

    async def bootstrap(self):
        if not self.available:
            raise RetryableProviderError("runtime unavailable")
        return {
            "routing_revision": 7,
            "destinations": [
                {"alias": "polyubit-kaliningrad-telegram", "kind": "destination", "label": "Полюбить Калининград — Telegram", "revision": 1},
                {"alias": "polyubit-kaliningrad-vk", "kind": "destination", "label": "Полюбить Калининград — VK", "revision": 1},
                {"alias": "uhty-kaliningrad-max", "kind": "destination", "label": "Ух ты, Калининград — MAX", "revision": 1},
                {"alias": "ambiguous", "kind": "destination", "label": "Неясный канал", "revision": 1},
            ],
            "capabilities": [
                {"destination": "polyubit-kaliningrad-telegram", "operation": "publish", "surface": "post", "status": "supported"},
                {"destination": "polyubit-kaliningrad-vk", "operation": "publish", "surface": "post", "status": "needs_review"},
                {"destination": "uhty-kaliningrad-max", "operation": "publish", "surface": "post", "status": "needs_auth"},
                {"destination": "ambiguous", "operation": "publish", "surface": "post", "status": "supported"},
            ],
        }

    def source_image_ingress_supported(self):
        return False

    async def publish(self, payload, request_key):
        self.publish_calls.append((request_key, payload))
        if request_key not in self.effects:
            self.effects[request_key] = "vp_op_1"
            if self.lose_first:
                self.lose_first = False
                raise RetryableProviderError("lost response after acceptance")
        return {"operation_id": self.effects[request_key], "state": "scheduled"}

    async def status(self, operation_id):
        return {"receipts": [{"operation_id": operation_id, "state": "scheduled"}]}


def config(tmp_path):
    return Settings(
        data_dir=tmp_path,
        device_token="device-secret",
        gemini_api_key="server-only-gemini-secret",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="http://vibepublish.test",
        vibepublish_bearer_token="server-only-vp-secret",
        osm_user_agent="StreetStory tests",
        worker_poll_seconds=0.01,
    )


def service(tmp_path, gemini=None, vp=None):
    gemini = gemini or FakeGemini()
    vp = vp or FakeVibePublish()
    return StreetStoryService(config(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), gemini, vp)), gemini, vp


def create(svc, key="create-key", client="story-client-1", photo=PHOTO, sha=PHOTO_SHA):
    return svc.create_story(
        key=key,
        client_story_id=client,
        photo_sha256=sha,
        photo_mime_type="image/jpeg",
        photo_bytes=photo,
        voice_protocol="voice-chunks-v2",
        lat=54.7104,
        lon=20.4522,
    )


def open_voice(svc, story_id, session="voice-1", kind="initial"):
    return svc.open_voice(story_id, f"open-{session}", {
        "session_id": session,
        "kind": kind,
        "started_at": "2026-09-08T12:00:00+02:00",
        "timezone": "Europe/Kaliningrad",
        "device_label": "test",
        "capture_policy": "voice_activity_auto_pause_v1",
        "audio": {"container": "mp4", "codec": "aac_lc", "mime_type": "audio/mp4", "sample_rate_hz": 16000, "channels": 1, "target_bitrate_bps": 32000},
        "vad": {"engine": "webrtc_vad", "engine_version": "2.0.10-cf.4", "config_version": "vad-auto-pause-efficient-v1", "frame_ms": 30, "mode": 1},
    })


def add_chunk(svc, story_id, session, index, text):
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    receipt = svc.put_chunk(
        story_id,
        session,
        index,
        f"chunk-{session}-{index}-{sha[:8]}",
        sha,
        data,
        {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "wall_start_ms": index * 1100, "wall_end_ms": (index + 1) * 1100},
        "audio/mp4",
    )
    return receipt, sha


def finish(svc, story_id, session, shas, kind="initial"):
    body = {
        "session_id": session,
        "kind": kind,
        "ended_at": "2026-09-08T12:05:00+02:00",
        "duration_ms": len(shas) * 1000,
        "wall_elapsed_ms": len(shas) * 1100,
        "manual_pause_ms": 0,
        "auto_silence_skipped_ms": len(shas) * 100,
        "chunk_count": len(shas),
        "chunks": [{"index": i, "sha256": sha, "start_ms": i * 1000, "end_ms": (i + 1) * 1000, "wall_start_ms": i * 1100, "wall_end_ms": (i + 1) * 1100, "mime_type": "audio/mp4"} for i, sha in enumerate(shas)],
    }
    return svc.complete_voice(story_id, session, f"complete-{session}", body)


def seed_ready_visual(svc, story_id):
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='ready_to_publish',vibepublish_asset_ref='asset_verified_1',draft_text='draft' WHERE id=?", (story_id,))


def test_idempotent_create(tmp_path):
    svc, _, _ = service(tmp_path)
    assert create(svc)["id"] == create(svc)["id"]
    assert len(svc.stories()) == 1


def test_conflicting_create(tmp_path):
    svc, _, _ = service(tmp_path)
    create(svc)
    other = b"different-photo"
    with pytest.raises(ConflictError) as exc:
        create(svc, key="create-other", photo=other, sha=hashlib.sha256(other).hexdigest())
    assert exc.value.code == "client_story_photo_conflict"


def test_exact_chunk_digest_validation(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    with pytest.raises(ConflictError) as exc:
        svc.put_chunk(story["id"], "voice-1", 0, "chunk-bad", "0" * 64, b"abc", {"start_ms": 0, "end_ms": 1, "wall_start_ms": 0, "wall_end_ms": 1}, "audio/mp4")
    assert exc.value.code == "chunk_digest_mismatch"


def test_lost_chunk_put_response_reconciles_manifest(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 0, "zero")
    assert open_voice(svc, story["id"])["received"] == [{"index": 0, "sha256": sha}]
    assert add_chunk(svc, story["id"], "voice-1", 0, "zero")[0]["received"] == [{"index": 0, "sha256": sha}]


def test_exact_manifest_complete_rejects_gap(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 1, "one")
    with pytest.raises(ConflictError) as exc:
        finish(svc, story["id"], "voice-1", [sha])
    assert exc.value.code == "voice_manifest_mismatch"


@pytest.mark.asyncio
async def test_long_multichunk_transcription_order_and_reuse(tmp_path):
    svc, gemini, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    shas = [add_chunk(svc, story["id"], "voice-1", i, value)[1] for i, value in [(0, "zero"), (1, "one"), (2, "two")]]
    finish(svc, story["id"], "voice-1", shas)
    await svc.run_once()
    assert gemini.transcribe_order == ["zero", "one", "two"]
    with svc.store.tx() as db:
        svc._enqueue_job(db, story["id"], "research", "manual-research-retry", {"voice_session_id": "voice-1"})
    await svc.run_once()
    assert gemini.transcribe_calls == 3


@pytest.mark.asyncio
async def test_worker_restart_during_research_resumes(tmp_path):
    svc, _, vp = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 0, "restart")
    finish(svc, story["id"], "voice-1", [sha])
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',lease_until=0 WHERE kind='research'")
    restarted = StreetStoryService(config(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), FakeGemini(), vp))
    assert restarted.recover_jobs() == 1
    with restarted.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0")
    await restarted.run_once()
    assert restarted.story(story["id"])["state"] == "review"


@pytest.mark.asyncio
async def test_osm_cache(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"display_name": "Cached", "osm_type": "way", "osm_id": 1} if request.method == "GET" else {"elements": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc, _, _ = service(tmp_path)
    osm = OSMClient(svc.store, "StreetStory tests", client)
    await osm.lookup(54.7, 20.45)
    await osm.lookup(54.7, 20.45)
    await client.aclose()
    assert calls == ["GET", "POST"]


@pytest.mark.asyncio
async def test_wikipedia_cache(tmp_path):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        params = dict(request.url.params)
        if params.get("list") == "geosearch":
            return httpx.Response(200, json={"query": {"geosearch": [{"pageid": 1, "title": "Test"}]}})
        return httpx.Response(200, json={"query": {"pages": [{"pageid": 1, "title": "Test", "extract": "Text", "fullurl": "https://ru.wikipedia.org/wiki/Test"}]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc, _, _ = service(tmp_path)
    wiki = WikipediaClient(svc.store, client)
    await wiki.nearby(54.7, 20.45)
    await wiki.nearby(54.7, 20.45)
    await client.aclose()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_evidence_urls_survive_and_unsupported_defaults_off(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 0, "zero")
    finish(svc, story["id"], "voice-1", [sha])
    await svc.run_once()
    facts = svc.story(story["id"])["facts"]
    supported = next(f for f in facts if f["evidence_supported"])
    unsupported = next(f for f in facts if not f["evidence_supported"])
    assert supported["sources"][0]["url"] == "https://ru.wikipedia.org/wiki/Test"
    assert supported["selected"] is True
    assert unsupported["selected"] is False


@pytest.mark.asyncio
async def test_refinement_preserves_fact_toggle(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 0, "zero")
    finish(svc, story["id"], "voice-1", [sha])
    await svc.run_once()
    fact_id = stable_fact_id("Дом Советов связан с центральной площадью")
    open_voice(svc, story["id"], "voice-refine", "refinement")
    _, rsha = add_chunk(svc, story["id"], "voice-refine", 0, "refine")
    finish(svc, story["id"], "voice-refine", [rsha], "refinement")
    svc.mutate_refinement(story["id"], "refinement-key", {"voice_session_id": "voice-refine", "selected_fact_ids": []})
    await svc.run_once()
    assert next(f for f in svc.story(story["id"])["facts"] if f["fact_id"] == fact_id)["selected"] is False


@pytest.mark.asyncio
async def test_gemini_temporary_error_is_retryable(tmp_path):
    svc, _, _ = service(tmp_path, gemini=FakeGemini(temporary_fail=True))
    story = create(svc)
    open_voice(svc, story["id"])
    _, sha = add_chunk(svc, story["id"], "voice-1", 0, "zero")
    finish(svc, story["id"], "voice-1", [sha])
    await svc.run_once()
    with svc.store.connection() as db:
        row = db.execute("SELECT state,last_error FROM jobs WHERE kind='research'").fetchone()
    assert row["state"] == "retry" and "temporary Gemini outage" in row["last_error"]


@pytest.mark.asyncio
async def test_visual_missing_ingress_is_durable_blocked_and_retry_same_story(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    svc.mutate_visual(story["id"], "visual-1", {"selected_fact_ids": []})
    await svc.run_once()
    blocked = svc.story(story["id"])
    assert blocked["state"] == "visual_blocked"
    assert blocked["error"]["code"] == "vibepublish_media_ingress_not_enabled"
    assert svc.mutate_visual(story["id"], "visual-1", {"selected_fact_ids": []})["state"] == "visual_blocked"
    assert svc.mutate_visual(story["id"], "visual-2", {"selected_fact_ids": []})["id"] == story["id"]


@pytest.mark.asyncio
async def test_visual_missing_runtime_is_durable_blocked(tmp_path):
    svc, _, _ = service(tmp_path, vp=FakeVibePublish(available=False))
    story = create(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    svc.mutate_visual(story["id"], "visual-1", {"selected_fact_ids": []})
    await svc.run_once()
    assert svc.story(story["id"])["error"]["code"] == "vibepublish_runtime_unavailable"


@pytest.mark.asyncio
async def test_destinations_only_from_real_projected_bootstrap(tmp_path):
    svc, _, _ = service(tmp_path)
    caps = await svc.capabilities()
    aliases = {d["alias"] for d in caps["destinations"]}
    assert aliases == {"polyubit-kaliningrad-telegram", "polyubit-kaliningrad-vk", "uhty-kaliningrad-max"}
    assert next(d for d in caps["destinations"] if d["provider"] == "telegram")["selected"] is True
    assert next(d for d in caps["destinations"] if d["provider"] == "vk")["selected"] is True
    assert next(d for d in caps["destinations"] if d["provider"] == "max")["selected"] is False


@pytest.mark.asyncio
async def test_publish_retry_produces_one_vibepublish_intent(tmp_path):
    vp = FakeVibePublish(lose_first=True)
    svc, _, _ = service(tmp_path, vp=vp)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    body = {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 60, "text_override": "copy"}
    svc.mutate_publish(story["id"], "publish-key", body)
    svc.mutate_publish(story["id"], "publish-key", body)
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM publish_intents").fetchone()[0] == 1
    await svc.run_once()
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='publish'")
    await svc.run_once()
    assert len(vp.effects) == 1
    assert len({key for key, _ in vp.publish_calls}) == 1
    assert svc.story(story["id"])["state"] == "scheduled"


@pytest.mark.asyncio
async def test_lost_vibepublish_response_reconciles_same_request(tmp_path):
    vp = FakeVibePublish(lose_first=True)
    svc, _, _ = service(tmp_path, vp=vp)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    svc.mutate_publish(story["id"], "publish-key", {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 60})
    await svc.run_once()
    with svc.store.connection() as db:
        key = db.execute("SELECT vibepublish_request_key FROM publish_intents").fetchone()[0]
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='publish'")
    await svc.run_once()
    assert vp.publish_calls[0][0] == key == vp.publish_calls[1][0]


@pytest.mark.asyncio
async def test_process_restart_resumes_nonterminal_publish_job(tmp_path):
    vp = FakeVibePublish()
    svc, _, _ = service(tmp_path, vp=vp)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    svc.mutate_publish(story["id"], "publish-key", {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 60})
    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',lease_until=0 WHERE kind='publish'")
    restarted = StreetStoryService(config(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), FakeGemini(), vp))
    assert restarted.recover_jobs() == 1
    with restarted.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0")
    await restarted.run_once()
    assert restarted.story(story["id"])["state"] == "scheduled"


def test_no_server_secrets_in_android_responses(tmp_path):
    svc, _, _ = service(tmp_path)
    client = TestClient(create_app(config(tmp_path), svc))
    response = client.post(
        "/v1/stories",
        headers={"Authorization": "Bearer device-secret", "Idempotency-Key": "http-create", "X-Photo-SHA256": PHOTO_SHA},
        data={"client_story_id": "http-story", "photo_sha256": PHOTO_SHA, "voice_protocol": "voice-chunks-v2", "lat": "54.7", "lon": "20.45"},
        files={"photo": ("photo.jpg", PHOTO, "image/jpeg")},
    )
    assert response.status_code == 200
    assert "server-only-gemini-secret" not in response.text
    assert "server-only-vp-secret" not in response.text


def test_health_public_story_auth_required(tmp_path):
    svc, _, _ = service(tmp_path)
    client = TestClient(create_app(config(tmp_path), svc))
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/stories").status_code == 401


def test_provider_projection_never_fabricates_ambiguous_provider():
    bootstrap = {
        "destinations": [{"alias": "channel_one", "kind": "destination", "label": "Канал", "revision": 1}],
        "capabilities": [{"destination": "channel_one", "operation": "publish", "surface": "post", "status": "supported"}],
    }
    assert project_destinations(bootstrap) == []
