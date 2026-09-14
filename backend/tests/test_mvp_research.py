from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.service import ConflictError, ProviderBundle


PHOTO = b"research-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
WIKI_URL = "https://ru.wikipedia.org/wiki/Test"


class FakeOSM:
    async def lookup(self, lat, lon):
        return {
            "reverse": {"display_name": "Площадь, Калининград"},
            "nearby": [
                {
                    "type": "way",
                    "id": 99,
                    "tags": {"name": "Неверный ближайший объект", "historic": "yes"},
                }
            ],
        }


class FakeWikipedia:
    async def nearby(self, lat, lon):
        return [
            {
                "pageid": 1,
                "title": "Дом Советов",
                "url": WIKI_URL,
                "extract": "Дом Советов расположен у центральной площади Калининграда. История здания связана с послевоенной перестройкой центра города.",
            }
        ]


class FakeGemini:
    def __init__(self, identity_status="match"):
        self.identity_status = identity_status
        self.transcribe_calls: list[str] = []
        self.research_calls = 0

    async def transcribe(self, path: Path, mime_type: str):
        session_id = path.parent.name
        self.transcribe_calls.append(session_id)
        return {
            "voice-1": "Первое сообщение про Дом Советов.",
            "voice-2": "Второе сообщение этой же истории.",
            "voice-3": "Третье сообщение, ищем факты.",
            "voice-refine": "Уточнение: не возвращай снятый мной факт.",
        }.get(session_id, "Голосовое сообщение")

    async def identify_photo(self, photo_path, photo_mime, transcript, candidates):
        if self.identity_status == "match":
            return {
                "status": "match",
                "candidate_id": "wiki:1",
                "confidence": 0.94,
                "observations": ["Форма и положение объекта совпадают с кандидатом."],
                "alternative_candidate_ids": [],
            }
        return {
            "status": "mismatch",
            "candidate_id": "osm:way:99",
            "confidence": 0.82,
            "observations": ["Ближайший OSM-кандидат визуально не совпадает с фото."],
            "alternative_candidate_ids": ["wiki:1"],
        }

    async def research_v2(self, photo_path, photo_mime, transcript, identity, previous):
        self.research_calls += 1
        text = (
            "Дом Советов расположен у центральной площади Калининграда."
            if self.research_calls == 1
            else "У центральной площади Калининграда расположен Дом Советов."
        )
        return {
            "payload": {
                "summary": "Проверено по источникам",
                "author_note": "Я смотрю на знакомый городской силуэт.",
                "facts": [
                    {
                        "claim_key": "dom-sovetov-location-central-square",
                        "text": text,
                        "confidence": 0.9,
                        "source_urls": [WIKI_URL],
                    },
                    {
                        "claim_key": "unsupported-legend",
                        "text": "Неподтвержденная городская легенда.",
                        "confidence": 0.4,
                        "source_urls": ["https://unknown.invalid/source"],
                    },
                ],
            },
            "grounding_sources": [],
            "grounding_supports": [],
        }


class NoopVP:
    async def bootstrap(self):
        return {
            "routing_revision": 1,
            "destinations": [
                {
                    "alias": "tg-safe",
                    "kind": "destination",
                    "label": "Полюбить Калининград",
                    "provider": "telegram",
                },
                {
                    "alias": "vk-deferred",
                    "kind": "destination",
                    "label": "Полюбить Калининград",
                    "provider": "vk",
                },
            ],
            "capabilities": [
                {
                    "destination": "tg-safe",
                    "operation": "publish",
                    "surface": "post",
                    "provider": "telegram",
                    "status": "supported",
                },
                {
                    "destination": "vk-deferred",
                    "operation": "publish",
                    "surface": "post",
                    "provider": "vk",
                    "status": "supported",
                },
            ],
        }


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story research tests",
        worker_poll_seconds=0.01,
    )


def service(tmp_path: Path, *, identity_status="match"):
    gemini = FakeGemini(identity_status)
    svc = MvpResearchStreetStoryService(
        settings(tmp_path), ProviderBundle(FakeOSM(), FakeWikipedia(), gemini, NoopVP())
    )
    story = svc.create_story(
        key="create",
        client_story_id="story-research",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.7104,
        lon=20.4522,
    )
    return svc, gemini, story


def add_voice(svc, story_id: str, session_id: str, kind="initial"):
    svc.open_voice(
        story_id,
        f"open-{session_id}",
        {
            "session_id": session_id,
            "kind": kind,
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
    data = f"audio-{session_id}".encode()
    sha = hashlib.sha256(data).hexdigest()
    svc.put_chunk(
        story_id,
        session_id,
        0,
        f"chunk-{session_id}",
        sha,
        data,
        {"start_ms": 0, "end_ms": 1000, "wall_start_ms": 0, "wall_end_ms": 1100},
        "audio/mp4",
    )
    svc.complete_voice(
        story_id,
        session_id,
        f"finish-{session_id}",
        {
            "session_id": session_id,
            "kind": kind,
            "ended_at": "2026-09-13T10:00:02+02:00",
            "duration_ms": 1000,
            "wall_elapsed_ms": 1100,
            "manual_pause_ms": 0,
            "auto_silence_skipped_ms": 100,
            "chunk_count": 1,
            "chunks": [
                {
                    "index": 0,
                    "sha256": sha,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "wall_start_ms": 0,
                    "wall_end_ms": 1100,
                    "mime_type": "audio/mp4",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_three_voice_messages_are_ordered_and_research_is_explicit(tmp_path):
    svc, gemini, story = service(tmp_path)
    for session_id in ("voice-1", "voice-2", "voice-3"):
        add_voice(svc, story["id"], session_id)

    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    before = svc.story(story["id"])
    assert before["state"] == "voice_ready"
    assert [item["session_id"] for item in before["voice_messages"]] == ["voice-1", "voice-2", "voice-3"]

    svc.mutate_facts(story["id"], "research-explicit", {"action": "research"})
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1
    assert await svc.run_once() is True

    result = svc.story(story["id"])
    assert result["state"] == "review"
    assert gemini.transcribe_calls == ["voice-1", "voice-2", "voice-3"]
    assert result["visual_identity"]["status"] == "match"
    assert result["visual_identity"]["candidate_id"] == "wiki:1"
    assert result["source_count"] == 1
    assert result["sources"] == [{"type": "wikipedia", "title": "Дом Советов", "url": WIKI_URL}]
    assert result["facts"][0]["evidence_supported"] is True
    assert result["facts"][0]["sources"][0]["supports"][0]["kind"] == "retrieved_excerpt"
    assert result["facts"][1]["evidence_supported"] is False
    assert result["facts"][1]["selected"] is False


@pytest.mark.asyncio
async def test_wrong_nearest_candidate_requires_owner_confirmation(tmp_path):
    svc, gemini, story = service(tmp_path, identity_status="mismatch")
    add_voice(svc, story["id"], "voice-1")
    svc.mutate_facts(story["id"], "research-first", {"action": "research"})
    assert await svc.run_once() is True
    blocked = svc.story(story["id"])
    assert blocked["state"] == "needs_review"
    assert blocked["visual_identity"]["status"] == "mismatch"
    assert gemini.research_calls == 0
    candidate_ids = {item["candidate_id"] for item in blocked["visual_identity"]["candidates"]}
    assert "wiki:1" in candidate_ids

    svc.mutate_facts(
        story["id"],
        "research-confirmed",
        {"action": "research", "candidate_id": "wiki:1"},
    )
    assert await svc.run_once() is True
    result = svc.story(story["id"])
    assert result["state"] == "review"
    assert result["visual_identity"]["status"] == "owner_confirmed"
    assert gemini.research_calls == 1


@pytest.mark.asyncio
async def test_rejected_claim_stays_rejected_after_rephrased_refinement(tmp_path):
    svc, _, story = service(tmp_path)
    add_voice(svc, story["id"], "voice-1")
    svc.mutate_facts(story["id"], "research-initial", {"action": "research"})
    assert await svc.run_once() is True
    first = svc.story(story["id"])
    supported_id = next(item["fact_id"] for item in first["facts"] if item["evidence_supported"])

    svc.mutate_facts(story["id"], "reject-all", {"selected_fact_ids": []})
    assert svc.story(story["id"])["facts"][0]["selected"] is False

    add_voice(svc, story["id"], "voice-refine", kind="refinement")
    with svc.store.connection() as db:
        pending = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('ready','retry','running')").fetchone()[0]
        assert pending == 0
    svc.mutate_facts(story["id"], "research-refined", {"action": "research"})
    assert await svc.run_once() is True
    refined = svc.story(story["id"])
    same_claim = next(item for item in refined["facts"] if item["fact_id"] == supported_id)
    assert same_claim["selected"] is False
    assert "У центральной площади" not in (refined["draft_text"] or "")


@pytest.mark.asyncio
async def test_mvp_capabilities_expose_only_supported_telegram(tmp_path):
    svc, _, _ = service(tmp_path)
    capabilities = await svc.capabilities()
    assert [item["alias"] for item in capabilities["destinations"]] == ["tg-safe"]
    assert capabilities["mvp_providers"] == {
        "telegram": "active",
        "vk": "deferred",
        "max": "deferred",
    }


def test_publish_caption_limit_fails_before_silent_truncation(tmp_path):
    svc, _, story = service(tmp_path)
    with pytest.raises(ConflictError) as exc:
        svc.mutate_publish(
            story["id"],
            "too-long",
            {"destinations": ["tg-safe"], "delay_minutes": 60, "text_override": "x" * 1025},
        )
    assert exc.value.code == "publish_text_too_long"
