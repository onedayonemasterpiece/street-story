from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.product import ProductStreetStoryService, normalize_display_text
from street_story.providers import GroundedResearch, RetryableProviderError
from street_story.service import ProviderBundle

PHOTO = b"street-story-product-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
PROCESSED = b"street-story-processed-image"
PROCESSED_SHA = hashlib.sha256(PROCESSED).hexdigest()


class FakeOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {"display_name": "Дом Советов, Калининград"}, "nearby": []}


class FakeWikipedia:
    async def nearby(self, lat, lon):
        return [{"title": "Дом Советов", "extract": "Проверенный контекст", "url": "https://ru.wikipedia.org/wiki/Test"}]


class DirtyGemini:
    def __init__(self):
        self.transcribe_calls = 0

    async def transcribe(self, path: Path, mime_type: str):
        self.transcribe_calls += 1
        return "э-э, я я хочу по- потом уточнить Дом Советов и его историю"

    async def research(self, photo_path, photo_mime, transcript, place_context, wikipedia, previous_facts):
        return GroundedResearch(
            payload={
                "place_name": "Дом Советов",
                "summary": "Короткое резюме",
                "draft_text": "Черновик публикации",
                "facts": [
                    {"text": "Дом Советов связан с центральной площадью", "confidence": 0.91, "source_urls": ["https://ru.wikipedia.org/wiki/Test"]},
                    {"text": "Неподтверждённая легенда", "confidence": 0.2, "source_urls": []},
                ],
            },
            grounding_sources=[{"type": "web", "title": "Grounded", "url": "https://grounded.example/source"}],
        )


class ProductFakeVibePublish:
    def __init__(self, lose_first_ingress: bool = False):
        self.lose_first_ingress = lose_first_ingress
        self.ingress_effects: dict[str, dict] = {}
        self.visual_effects: dict[str, dict] = {}
        self.publish_effects: dict[str, dict] = {}
        self.cancel_effects: dict[str, dict] = {}

    async def bootstrap(self):
        return {
            "routing_revision": 9,
            "destinations": [
                {"alias": "love-kld-main", "kind": "destination", "label": "Полюбить Калининград", "provider": "telegram"},
                {"alias": "love-kld-vk", "kind": "destination", "label": "Полюбить Калининград", "provider": "vk"},
                {"alias": "uhty-max", "kind": "destination", "label": "Ух ты, Калининград", "provider": "max"},
            ],
            "capabilities": [
                {"destination": "love-kld-main", "operation": "publish", "surface": "post", "provider": "telegram", "status": "supported"},
                {"destination": "love-kld-vk", "operation": "publish", "surface": "post", "provider": "vk", "status": "needs_review"},
                {"destination": "uhty-max", "operation": "publish", "surface": "post", "provider": "max", "status": "needs_auth"},
            ],
        }

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str):
        receipt = self.ingress_effects.setdefault(
            request_key,
            {"asset_id": "asset_source_1", "source_sha256": hashlib.sha256(data).hexdigest(), "sha256": hashlib.sha256(data).hexdigest()},
        )
        if self.lose_first_ingress:
            self.lose_first_ingress = False
            raise RetryableProviderError("lost ingress response after durable acceptance")
        return receipt

    async def visual(self, payload: dict, request_key: str):
        kind = payload["command"]["kind"]
        if kind == "tune":
            ready = {
                "operation_id": "vp_visual_op_1",
                "state": "needs_selection",
                "visual_job_id": "visual_job_1",
                "visual_revision": 1,
                "candidates": [
                    {"id": "candidate_1", "asset_ref": "asset_candidate_1", "sha256": PROCESSED_SHA, "selection_token": "selection-token"}
                ],
            }
            self.visual_effects.setdefault(request_key, ready)
            return ready
        if kind == "select":
            selected = {
                "operation_id": "vp_visual_op_1",
                "state": "verified",
                "selected_asset_ref": "asset_processed_1",
                "selected_sha256": PROCESSED_SHA,
                "visual_job_id": "visual_job_1",
                "visual_revision": 2,
            }
            self.visual_effects.setdefault(request_key, selected)
            return selected
        raise AssertionError(kind)

    async def status(self, operation_id: str):
        if operation_id == "vp_visual_op_1":
            ready = next(value for value in self.visual_effects.values() if value.get("state") == "needs_selection")
            return {"receipts": [ready]}
        if operation_id == "vp_publish_op_1":
            return {"receipts": [next(iter(self.publish_effects.values()))]}
        if operation_id == "vp_cancel_op_1":
            return {"receipts": [next(iter(self.cancel_effects.values()))]}
        raise AssertionError(operation_id)

    async def read_asset(self, asset_id: str):
        assert asset_id == "asset_processed_1"
        return PROCESSED, "image/png"

    async def publish(self, payload: dict, request_key: str):
        alias = payload["to"][0]
        receipt = {
            "operation_id": "vp_publish_op_1",
            "resource_id": "publication_1",
            "revision": 1,
            "state": "scheduled",
            "deliveries": [{"destination": alias, "observed": "scheduled", "effective_at": payload["delivery"]["at"]}],
        }
        self.publish_effects.setdefault(request_key, receipt)
        return receipt

    async def publication_update(self, publication_id: str, expected_revision: int, change: dict, request_key: str):
        assert publication_id == "publication_1"
        assert expected_revision == 1
        assert change == {"kind": "cancel"}
        receipt = {
            "operation_id": "vp_cancel_op_1",
            "resource_id": publication_id,
            "revision": 2,
            "state": "cancelled",
            "deliveries": [{"destination": "love-kld-main", "observed": "cancelled", "effective_at": "2026-09-08T20:00:00Z"}],
        }
        self.cancel_effects.setdefault(request_key, receipt)
        return receipt


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story product tests",
        worker_poll_seconds=0.01,
    )


def product_service(tmp_path, *, vp=None, gemini=None):
    vp = vp or ProductFakeVibePublish()
    gemini = gemini or DirtyGemini()
    return ProductStreetStoryService(settings(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), gemini, vp)), gemini, vp


def create_story(service, client_id="story-product-1"):
    return service.create_story(
        key=f"create-{client_id}", client_story_id=client_id, photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg", photo_bytes=PHOTO, voice_protocol="voice-chunks-v2",
        lat=54.7104, lon=20.4522,
    )


def open_and_finish_voice(service, story_id: str, session_id="voice-product-1", kind="initial"):
    service.open_voice(story_id, f"open-{session_id}", {
        "session_id": session_id, "kind": kind, "started_at": "2026-09-08T12:00:00+02:00",
        "timezone": "Europe/Kaliningrad", "device_label": "test", "capture_policy": "voice_activity_auto_pause_v1",
        "audio": {"container": "mp4", "codec": "aac_lc", "mime_type": "audio/mp4", "sample_rate_hz": 16000, "channels": 1, "target_bitrate_bps": 32000},
        "vad": {"engine": "webrtc_vad", "engine_version": "2.0.10-cf.4", "config_version": "vad-auto-pause-efficient-v1", "frame_ms": 30, "mode": 1},
    })
    data = b"durable-audio"
    sha = hashlib.sha256(data).hexdigest()
    service.put_chunk(
        story_id, session_id, 0, f"chunk-{session_id}", sha, data,
        {"start_ms": 0, "end_ms": 6000, "wall_start_ms": 0, "wall_end_ms": 6100}, "audio/mp4",
    )
    service.complete_voice(story_id, session_id, f"complete-{session_id}", {
        "session_id": session_id, "kind": kind, "ended_at": "2026-09-08T12:00:07+02:00",
        "duration_ms": 6000, "wall_elapsed_ms": 6100, "manual_pause_ms": 0, "auto_silence_skipped_ms": 100,
        "chunk_count": 1,
        "chunks": [{"index": 0, "sha256": sha, "start_ms": 0, "end_ms": 6000, "wall_start_ms": 0, "wall_end_ms": 6100, "mime_type": "audio/mp4"}],
    })


def test_display_cleaner_preserves_meaning_without_summary():
    raw = "э-э, я я хочу по- потом уточнить Дом Советов и его историю"
    cleaned = normalize_display_text(raw)
    assert "э-э" not in cleaned.lower()
    assert "я я" not in cleaned.lower()
    assert "по-" not in cleaned.lower()
    assert "Дом Советов" in cleaned
    assert "уточнить" in cleaned and "историю" in cleaned


@pytest.mark.asyncio
async def test_raw_transcript_and_one_time_display_text_are_persisted(tmp_path):
    service, gemini, _ = product_service(tmp_path)
    story = create_story(service)
    open_and_finish_voice(service, story["id"])
    await service.run_once()
    result = service.story(story["id"])
    message = result["voice_messages"][0]
    assert message["raw_transcript"] == "э-э, я я хочу по- потом уточнить Дом Советов и его историю"
    assert message["display_text"] == "Я хочу потом уточнить Дом Советов и его историю"
    assert gemini.transcribe_calls == 1

    restarted = ProductStreetStoryService(settings(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), gemini, ProductFakeVibePublish()))
    assert restarted.story(story["id"])["voice_messages"][0]["display_text"] == message["display_text"]
    assert gemini.transcribe_calls == 1


@pytest.mark.asyncio
async def test_visual_uses_idempotent_ingress_select_and_verified_readback(tmp_path):
    vp = ProductFakeVibePublish(lose_first_ingress=True)
    service, _, _ = product_service(tmp_path, vp=vp)
    story = create_story(service)
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='review',draft_text='draft' WHERE id=?", (story["id"],))
    service.mutate_visual(story["id"], "visual-user-key", {"selected_fact_ids": []})
    await service.run_once()
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='visual'")
    await service.run_once()
    ready = service.story(story["id"])
    assert ready["state"] == "ready_to_publish"
    assert ready["visual"]["source_asset_ref"] == "asset_source_1"
    assert ready["visual"]["selected_asset_ref"] == "asset_processed_1"
    assert ready["visual"]["selected_sha256"] == PROCESSED_SHA
    assert len(vp.ingress_effects) == 1
    assert service.asset(story["id"])[0] == PROCESSED


@pytest.mark.asyncio
async def test_primary_telegram_vk_projection_and_social_cancel_receipt(tmp_path):
    service, _, vp = product_service(tmp_path)
    capabilities = await service.capabilities()
    primary = {(d["provider"], d["status"], d["selected"]) for d in capabilities["destinations"] if "Полюбить" in d["label"]}
    assert ("telegram", "supported", True) in primary
    assert ("vk", "needs_review", True) in primary

    story = create_story(service)
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='ready_to_publish',vibepublish_asset_ref='asset_processed_1',draft_text='draft' WHERE id=?", (story["id"],))
    service.mutate_publish(story["id"], "publish-user-key", {"destinations": ["love-kld-main"], "delay_minutes": 60, "text_override": "copy"})
    await service.run_once()
    scheduled = service.story(story["id"])
    assert scheduled["state"] == "scheduled"
    assert scheduled["publication"]["publication_id"] == "publication_1"
    assert scheduled["destinations"][0]["status"] == "scheduled"

    service.mutate_cancel(story["id"], "cancel-user-key", {})
    await service.run_once()
    cancelled = service.story(story["id"])
    assert cancelled["publication"]["state"] == "cancelled"
    assert cancelled["destinations"][0]["status"] == "cancelled"
    assert len(vp.publish_effects) == 1 and len(vp.cancel_effects) == 1
