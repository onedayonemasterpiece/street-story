from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.mvp import MvpProductStreetStoryService
from street_story.service import ProviderBundle


PHOTO = b"mvp-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
PROCESSED = b"mvp-processed"
PROCESSED_SHA = hashlib.sha256(PROCESSED).hexdigest()
PROMPT_SHA = "4eab6d0cfcafc84881cad86380baa9920785b7e18e9a934923966995802380a3"


class NoopOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {}, "nearby": []}


class NoopWikipedia:
    async def nearby(self, lat, lon):
        return []


class NoopGemini:
    pass


class RecoverableVisualVibePublish:
    def __init__(self):
        self.tune_calls = 0
        self.status_calls = 0

    async def bootstrap(self):
        return {"routing_revision": 1, "destinations": [], "capabilities": []}

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str):
        return {"asset_id": "source-asset", "source_sha256": hashlib.sha256(data).hexdigest()}

    async def visual(self, payload: dict, request_key: str):
        assert payload["command"]["kind"] == "tune"
        self.tune_calls += 1
        return {"operation_id": "visual-op", "state": "accepted", "visual_job_id": "job-1"}

    async def status(self, operation_id: str):
        assert operation_id == "visual-op"
        self.status_calls += 1
        if self.status_calls == 1:
            return {"receipts": [{"operation_id": operation_id, "state": "accepted"}]}
        return {
            "receipts": [
                {
                    "operation_id": operation_id,
                    "state": "verified",
                    "visual_job_id": "job-1",
                    "selected_asset_ref": "processed-asset",
                    "selected_sha256": PROCESSED_SHA,
                }
            ]
        }

    async def read_asset(self, asset_id: str):
        assert asset_id == "processed-asset"
        return PROCESSED, "image/png"


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story MVP tests",
        worker_poll_seconds=0.01,
    )


def service(tmp_path: Path, vp=None) -> MvpProductStreetStoryService:
    return MvpProductStreetStoryService(
        settings(tmp_path),
        ProviderBundle(NoopOSM(), NoopWikipedia(), NoopGemini(), vp or RecoverableVisualVibePublish()),
    )


def create_story(svc: MvpProductStreetStoryService):
    return svc.create_story(
        key="create-mvp",
        client_story_id="story-mvp",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.7104,
        lon=20.4522,
    )


def test_owner_prompt_is_exact_and_expands_russian_notes(tmp_path):
    prompt = Path(__file__).resolve().parents[1] / "prompts" / "street-story-image-v1.txt"
    data = prompt.read_bytes()
    assert len(data) == 4217
    assert hashlib.sha256(data).hexdigest() == PROMPT_SHA

    svc = service(tmp_path)
    brief = svc._visual_brief(
        {"place_name": "Калининград"},
        {
            "user_voice_intent": "Здесь чувствуется ритм старого города",
            "selected_facts": [{"fact_id": "f1", "text": "Проверенный факт", "sources": []}],
        },
    )
    assert "{{CITY_NOTE_THEMES}}" not in brief
    assert "Проверенный факт" in brief
    assert "all visible handwritten city notes and annotations" in brief
    assert "without adding text" not in brief


def test_visual_request_freezes_content_snapshot(tmp_path):
    svc = service(tmp_path)
    story = create_story(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    result = svc.mutate_visual(story["id"], "visual-key", {"selected_fact_ids": []})
    assert result["visual"]["prompt_version"] == "street-story-image-v1"
    assert result["visual"]["prompt_sha256"] == PROMPT_SHA
    assert len(result["visual"]["content_revision"]) == 64
    with svc.store.connection() as db:
        row = db.execute("SELECT visual_context_json FROM stories WHERE id=?", (story["id"],)).fetchone()
    context = json.loads(row["visual_context_json"])
    assert context["source_photo_sha256"] == PHOTO_SHA
    assert context["ordered_voice_ids"] == []
    assert context["brief"].startswith("Use the uploaded street photo as the main reference image.")


@pytest.mark.asyncio
async def test_visual_operation_is_persisted_and_reconciled_without_second_paid_submit(tmp_path):
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    story = create_story(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    svc.mutate_visual(story["id"], "visual-reconcile", {"selected_fact_ids": []})

    assert await svc.run_once() is True
    after_first = svc.story(story["id"])
    assert after_first["visual"]["operation_id"] == "visual-op"
    assert vp.tune_calls == 1

    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='visual'")
    assert await svc.run_once() is True
    ready = svc.story(story["id"])
    assert ready["state"] == "ready_to_publish"
    assert ready["visual"]["selected_asset_ref"] == "processed-asset"
    assert ready["visual"]["selected_sha256"] == PROCESSED_SHA
    assert vp.tune_calls == 1
    assert svc.asset(story["id"])[0] == PROCESSED
