from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.mvp_location import MvpLocationStreetStoryService
from street_story.service import ProviderBundle


PHOTO = b"no-gps-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
WIKI_URL = "https://ru.wikipedia.org/wiki/Бранденбургские_ворота_(Калининград)"


class FakeOSM:
    user_agent = "Street Story no-GPS test"
    http = None

    async def geocode(self, query):
        assert "Бранденбург" in query
        return {
            "lat": 54.697111,
            "lon": 20.494111,
            "display_name": "Бранденбургские ворота, Калининград",
            "osm_type": "way",
            "osm_id": 10,
        }

    async def lookup(self, lat, lon):
        assert abs(lat - 54.697111) < 1e-6
        assert abs(lon - 20.494111) < 1e-6
        return {
            "reverse": {},
            "nearby": [
                {
                    "type": "way",
                    "id": 10,
                    "tags": {"name": "Бранденбургские ворота", "historic": "city_gate"},
                }
            ],
        }


class FakeWikipedia:
    async def nearby(self, lat, lon):
        return [
            {
                "pageid": 1,
                "title": "Бранденбургские ворота (Калининград)",
                "url": WIKI_URL,
                "extract": "Бранденбургские ворота находятся в Калининграде и являются городскими воротами.",
            }
        ]


class FakeGemini:
    async def transcribe(self, path: Path, mime_type: str):
        return "Я снимаю Бранденбургские ворота в Калининграде."

    async def extract_place_query(self, transcript: str):
        assert "Бранденбургские ворота" in transcript
        return "Бранденбургские ворота, Калининград"

    async def identify_photo(self, photo_path, photo_mime, transcript, candidates):
        assert any(item["candidate_id"] == "wiki:1" for item in candidates)
        return {
            "status": "match",
            "candidate_id": "wiki:1",
            "confidence": 0.94,
            "observations": ["Архитектурный облик совпадает с кандидатом."],
            "alternative_candidate_ids": [],
        }

    async def research_v2(self, photo_path, photo_mime, transcript, identity, previous):
        return {
            "payload": {
                "summary": "Проверено",
                "author_note": "",
                "facts": [
                    {
                        "claim_key": "brandenburg-gate-kaliningrad-location",
                        "text": "Бранденбургские ворота находятся в Калининграде.",
                        "confidence": 0.95,
                        "source_urls": [WIKI_URL],
                    }
                ],
            },
            "grounding_sources": [],
            "grounding_supports": [],
        }


class NoopVP:
    async def bootstrap(self):
        return {"routing_revision": 1, "destinations": [], "capabilities": []}


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story no-GPS test",
        worker_poll_seconds=0.01,
    )


def add_voice(service, story_id: str):
    session_id = "voice-no-gps"
    service.open_voice(
        story_id,
        "open-no-gps",
        {
            "session_id": session_id,
            "kind": "initial",
            "started_at": "2026-09-13T10:00:00+02:00",
            "timezone": "Europe/Kaliningrad",
            "device_label": "test",
            "capture_policy": "voice_activity_auto_pause_v1",
            "audio": {
                "container": "mp4",
                "codec": "aac_lc",
                "mime_type": "audio/mp4",
                "sample_rate_hz": 16000,
                "channels": 1,
                "target_bitrate_bps": 32000,
            },
            "vad": {
                "engine": "webrtc_vad",
                "engine_version": "2.0.10-cf.4",
                "config_version": "vad-auto-pause-efficient-v1",
                "frame_ms": 30,
                "mode": 1,
            },
        },
    )
    data = b"prepared-audio"
    sha = hashlib.sha256(data).hexdigest()
    service.put_chunk(
        story_id,
        session_id,
        0,
        "chunk-no-gps",
        sha,
        data,
        {"start_ms": 0, "end_ms": 1000, "wall_start_ms": 0, "wall_end_ms": 1000},
        "audio/mp4",
    )
    service.complete_voice(
        story_id,
        session_id,
        "complete-no-gps",
        {
            "session_id": session_id,
            "kind": "initial",
            "ended_at": "2026-09-13T10:00:02+02:00",
            "duration_ms": 1000,
            "wall_elapsed_ms": 1000,
            "manual_pause_ms": 0,
            "auto_silence_skipped_ms": 0,
            "chunk_count": 1,
            "chunks": [
                {
                    "index": 0,
                    "sha256": sha,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "wall_start_ms": 0,
                    "wall_end_ms": 1000,
                    "mime_type": "audio/mp4",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_no_gps_uses_explicit_owner_voice_place_not_device_location(tmp_path):
    service = MvpLocationStreetStoryService(
        settings(tmp_path),
        ProviderBundle(FakeOSM(), FakeWikipedia(), FakeGemini(), NoopVP()),
    )
    story = service.create_story(
        key="create-no-gps",
        client_story_id="story-no-gps",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=None,
        lon=None,
    )
    add_voice(service, story["id"])
    service.mutate_facts(story["id"], "research-no-gps", {"action": "research"})
    assert await service.run_once() is True

    result = service.story(story["id"])
    assert result["state"] == "review"
    provenance = result["location_provenance"]
    assert provenance["kind"] == "owner_voice_place_query"
    assert provenance["not_device_current_location"] is True
    assert provenance["query"] == "Бранденбургские ворота, Калининград"
    assert result["visual_identity"]["status"] == "match"
    assert result["source_count"] == 1
    with service.store.connection() as db:
        row = db.execute("SELECT latitude,longitude FROM stories WHERE id=?", (story["id"],)).fetchone()
        assert abs(row["latitude"] - 54.697111) < 1e-6
        assert abs(row["longitude"] - 20.494111) < 1e-6
