from __future__ import annotations

import hashlib

from street_story.config import Settings
from street_story.mvp_acceptance import MvpAcceptanceStreetStoryService
from street_story.service import ProviderBundle, canonical


PHOTO = b"acceptance-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class Dummy:
    pass


def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story acceptance tests",
        worker_poll_seconds=0.01,
    )


def service(tmp_path):
    svc = MvpAcceptanceStreetStoryService(
        settings(tmp_path), ProviderBundle(Dummy(), Dummy(), Dummy(), Dummy())
    )
    story = svc.create_story(
        key="create-acceptance",
        client_story_id="story-acceptance",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.71,
        lon=20.51,
    )
    return svc, story


def test_new_voice_after_scheduling_does_not_change_native_publication_state(tmp_path):
    svc, story = service(tmp_path)
    session_id = "voice-after-schedule"
    svc.open_voice(
        story["id"],
        "open-after-schedule",
        {
            "session_id": session_id,
            "kind": "refinement",
            "started_at": "2026-09-13T12:00:00+02:00",
            "timezone": "Europe/Kaliningrad",
            "device_label": "test",
            "capture_policy": "voice_activity_auto_pause_v1",
        },
    )
    data = b"voice-after-schedule"
    sha = hashlib.sha256(data).hexdigest()
    svc.put_chunk(
        story["id"], session_id, 0, "chunk-after-schedule", sha, data,
        {"start_ms": 0, "end_ms": 1000, "wall_start_ms": 0, "wall_end_ms": 1100},
        "audio/mp4",
    )
    with svc.store.tx() as db:
        db.execute(
            "UPDATE stories SET state='scheduled',scheduled_for='2026-09-14T12:00:00Z' WHERE id=?",
            (story["id"],),
        )
    svc.complete_voice(
        story["id"],
        session_id,
        "complete-after-schedule",
        {
            "session_id": session_id,
            "kind": "refinement",
            "ended_at": "2026-09-13T12:00:02+02:00",
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
    result = svc.story(story["id"])
    assert result["state"] == "scheduled"
    assert result["scheduled_for"] == "2026-09-14T12:00:00Z"
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_claim_support_is_content_bound_and_owner_decisions_are_retained(tmp_path):
    svc, story = service(tmp_path)
    good_id = "claim_good"
    bad_id = "claim_bad"
    with svc.store.tx() as db:
        db.execute(
            "UPDATE stories SET state='review',place_name='Дом Советов',research_json=? WHERE id=?",
            (
                canonical({
                    "author_note": "Мне важен этот городской силуэт.",
                    "claim_decisions": {"claim_historical": False},
                }),
                story["id"],
            ),
        )
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,?,?,?,?,?)",
            (
                story["id"],
                good_id,
                "Дом Советов расположен у центральной площади Калининграда.",
                0.9,
                1,
                1,
                canonical([
                    {
                        "type": "web",
                        "title": "Источник",
                        "url": "https://example.test/good",
                        "supports": [
                            {
                                "kind": "google_grounding",
                                "source_url": "https://example.test/good",
                                "text": "Дом Советов расположен у центральной площади Калининграда.",
                            }
                        ],
                    }
                ]),
            ),
        )
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,?,?,?,?,?)",
            (
                story["id"],
                bad_id,
                "Здание построено в 1980 году.",
                0.7,
                1,
                1,
                canonical([
                    {
                        "type": "web",
                        "title": "Нерелевантный источник",
                        "url": "https://example.test/bad",
                        "supports": [
                            {
                                "kind": "google_grounding",
                                "source_url": "https://example.test/bad",
                                "text": "Сегодня в Калининграде переменная облачность и сильный ветер.",
                            }
                        ],
                    }
                ]),
            ),
        )

    svc._enforce_claim_support_and_decisions(
        story["id"],
        {"claim_historical": False, good_id: False},
    )
    result = svc.story(story["id"])
    by_id = {fact["fact_id"]: fact for fact in result["facts"]}
    assert by_id[good_id]["evidence_supported"] is True
    assert by_id[good_id]["selected"] is False
    assert by_id[bad_id]["evidence_supported"] is False
    assert by_id[bad_id]["selected"] is False
    assert "Дом Советов расположен" not in (result["draft_text"] or "")
    assert result["image_notes"] == ""
    with svc.store.connection() as db:
        research = __import__("json").loads(
            db.execute("SELECT research_json FROM stories WHERE id=?", (story["id"],)).fetchone()[0]
        )
    assert research["claim_decisions"]["claim_historical"] is False
    assert research["claim_decisions"][good_id] is False
