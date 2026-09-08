from __future__ import annotations

import hashlib

import httpx
import pytest

from street_story.config import Settings
from street_story.runtime import ReplayCheckingVibePublishBoundary, RuntimeStreetStoryService
from street_story.service import ProviderBundle

PHOTO = b"runtime-projection-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story runtime tests",
        worker_poll_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_real_boundary_replays_same_asset_ingress_identity(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.method == "POST"
        assert request.url.path == "/v1/assets"
        assert request.headers["idempotency-key"] == "asset-replay-key"
        calls += 1
        return httpx.Response(
            201,
            json={
                "asset_id": "asset_1",
                "source_sha256": hashlib.sha256(request.content).hexdigest(),
                "sha256": hashlib.sha256(request.content).hexdigest(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        boundary = ReplayCheckingVibePublishBoundary(settings(tmp_path), client)
        receipt = await boundary.ingress_asset(PHOTO, "image/jpeg", "asset-replay-key")
    assert receipt["asset_id"] == "asset_1"
    assert calls == 2


class Dummy:
    pass


def test_runtime_story_exposes_bounded_research_provenance(tmp_path):
    providers = ProviderBundle(Dummy(), Dummy(), Dummy(), Dummy())
    service = RuntimeStreetStoryService(settings(tmp_path), providers)
    story = service.create_story(
        key="create-runtime-projection",
        client_story_id="runtime-projection",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.7064,
        lon=20.5117,
    )
    with service.store.tx() as db:
        db.execute(
            "UPDATE stories SET state='review',research_json=? WHERE id=?",
            (
                '{"osm":{"reverse":{"display_name":"Kaliningrad"}},"wikipedia":[{"title":"Fixture"}],'
                '"grounding_sources":[{"type":"web","url":"https://example.test/source"}]}',
                story["id"],
            ),
        )
    readback = service.story(story["id"])
    assert readback["research_provenance"] == {
        "osm_present": True,
        "wikipedia_page_count": 1,
        "grounded_source_count": 1,
    }
