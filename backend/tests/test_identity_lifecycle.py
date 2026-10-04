from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.mvp_research import MvpResearchStreetStoryService
from street_story.poi_memory import persist_research_memory
from street_story.service import ConflictError, NotFoundError, ProviderBundle


PHOTO = b"identity-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
WIKI_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.jpg/640px-Test.jpg"


class FakeOSM:
    async def lookup(self, lat, lon):
        return {
            "reverse": {
                "osm_type": "way",
                "osm_id": 7,
                "display_name": "Тестовые ворота, Калининград",
                "address": {"road": "Тестовая улица"},
                "tags": {"name": "Тестовые ворота", "historic": "city_gate"},
            },
            "nearby": [
                {
                    "type": "way",
                    "id": 7,
                    "display_name": "Тестовые ворота, Калининград",
                    "tags": {"name": "Тестовые ворота", "historic": "city_gate"},
                }
            ],
        }


class FakeWikipedia:
    async def nearby(self, lat, lon):
        return [
            {
                "pageid": 77,
                "title": "Тестовые ворота",
                "url": "https://ru.wikipedia.org/wiki/Test",
                "extract": "Тестовый объект.",
                "thumbnail_url": WIKI_IMAGE,
                "image_url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Test.jpg",
            }
        ]


class FakeGemini:
    def __init__(self):
        self.identity_calls = []
        self.research_calls = 0

    async def identify_photo(self, photo_path, photo_mime, transcript, candidates):
        self.identity_calls.append(candidates)
        return {
            "status": "match",
                "_references_sent": ["wiki:77"],
            "candidate_id": "wiki:77",
            "confidence": 0.97,
            "observations": ["Башни и фасад совпадают."],
            "alternative_candidate_ids": [],
        }

    async def research_v2(self, *args, **kwargs):
        self.research_calls += 1
        raise AssertionError("identity-only lifecycle must not research facts")


class NoopVP:
    async def bootstrap(self):
        return {"routing_revision": 1, "destinations": [], "capabilities": []}


def make_service(tmp_path: Path):
    gemini = FakeGemini()
    settings = Settings(
        data_dir=tmp_path,
        device_token="device-token-" + "x" * 32,
        gemini_api_key="gemini-" + "x" * 32,
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url=None,
        vibepublish_bearer_token=None,
        osm_user_agent="Street Story identity tests",
        worker_poll_seconds=0.01,
    )
    service = MvpResearchStreetStoryService(
        settings,
        ProviderBundle(FakeOSM(), FakeWikipedia(), gemini, NoopVP()),
    )
    return service, gemini


def create(service, client="identity-client", lat=54.7, lon=20.5):
    return service.create_story(
        key=f"create-{client}",
        client_story_id=client,
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=lat,
        lon=lon,
    )


@pytest.mark.asyncio
async def test_identity_job_is_mandatory_and_does_not_collect_facts(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service)

    queued = service.ensure_identity(story["id"])
    assert queued["state"] == "identifying"

    assert await service.run_once() is True
    result = service.story(story["id"])

    assert result["state"] == "identity_ready"
    assert result["place_name"] == "Тестовые ворота"
    assert result["facts"] == []
    assert result["draft_text"] is None
    assert result["visual_identity"]["status"] == "match"
    assert gemini.research_calls == 0
    assert gemini.identity_calls
    wiki = next(item for item in gemini.identity_calls[0] if item["candidate_id"] == "wiki:77")
    assert wiki["reference_image_urls"][0] == WIKI_IMAGE


@pytest.mark.asyncio
async def test_same_poi_hydrates_only_reviewed_eligible_facts_before_new_search(tmp_path):
    service, gemini = make_service(tmp_path)
    identity = {
        "candidate_id": "wiki:77",
        "candidate_name": "Тестовые ворота",
        "candidate_url": "https://ru.wikipedia.org/wiki/Test",
        "candidates": [{
            "candidate_id": "wiki:77",
            "name": "Тестовые ворота",
            "url": "https://ru.wikipedia.org/wiki/Test",
        }],
    }
    source = {
        "type": "web_search",
        "title": "Источник",
        "url": "https://example.org/test-gate",
        "supports": [{
            "kind": "search_snippet",
            "source_url": "https://example.org/test-gate",
            "text": "На фасаде находятся три скульптуры.",
        }],
    }
    durable_fact = {
        "fact_id": "claim-durable-sculptures",
        "claim_key": "test-gate-sculptures",
        "text": "На фасаде находятся три исторические скульптуры.",
        "confidence": .96,
        "evidence_supported": True,
        "selected": True,
        "sources": [source],
    }
    with service.store.tx() as db:
        persist_research_memory(
            db, identity, [durable_fact], [source],
            "скульптуры тестовых ворот", service.store.now(),
        )

    # Unreviewed memory is useful as research context but is not silently
    # promoted into a new topic as an accepted fact.
    first = create(service, client="reuse-first")
    assert service.ensure_identity(first["id"])["state"] == "identifying"
    assert await service.run_once() is True
    first_ready = service.story(first["id"])
    assert first_ready["facts"] == []
    assert gemini.research_calls == 0

    with service.store.tx() as db:
        db.execute(
            "UPDATE poi_research_assertions SET review_status='eligible',eligibility='eligible',"
            "review_story_id='review-source',reviewed_at=?,updated_at=? "
            "WHERE poi_key='wiki:77' AND assertion_id='claim-durable-sculptures'",
            (service.store.now(), service.store.now()),
        )

    service.delete_story(first["id"])
    second = create(service, client="reuse-second")
    assert service.ensure_identity(second["id"])["state"] == "identifying"
    assert await service.run_once() is True
    second_ready = service.story(second["id"])
    assert [fact["fact_id"] for fact in second_ready["facts"]] == [
        "claim-durable-sculptures"
    ]
    assert second_ready["facts"][0]["evidence_supported"] is True
    assert second_ready["facts"][0]["selected"] is False
    # A legacy cache flag without a retained exact model proof is not admission.
    assert second_ready["facts"][0]["eligibility"] != 'eligible'
    assert gemini.research_calls == 0

    with service.store.connection() as db:
        research = __import__("json").loads(
            db.execute(
                "SELECT research_json FROM stories WHERE id=?",
                (second["id"],),
            ).fetchone()[0]
        )
        assert research["poi_reused_fact_count"] == 1
        poi_id = research["poi_id"]
        poi = db.execute(
            "SELECT canonical_name FROM pois WHERE id=?",
            (poi_id,),
        ).fetchone()
        assert poi["canonical_name"] == "Тестовые ворота"
        aliases = {
            (row["namespace"], row["value"])
            for row in db.execute(
                "SELECT namespace,value FROM poi_aliases WHERE poi_id=?",
                (poi_id,),
            )
        }
        assert ("street_story_candidate", "wiki:77") in aliases
        assert ("name", "Тестовые ворота") in aliases

    service.delete_story(second["id"])
    with service.store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM poi_research_assertions "
            "WHERE poi_key='wiki:77' AND assertion_id='claim-durable-sculptures' "
            "AND eligibility='eligible'"
        ).fetchone() is not None


@pytest.mark.asyncio
async def test_identity_without_exif_coordinates_requires_review(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service, client="no-gps", lat=None, lon=None)

    assert service.ensure_identity(story["id"])["state"] == "identifying"
    assert await service.run_once() is True

    result = service.story(story["id"])
    assert result["state"] == "needs_review"
    assert result["error"]["code"] == "identity_location_missing"
    assert result["facts"] == []
    assert gemini.identity_calls == []


def test_delete_story_cascades_and_scheduled_story_is_blocked(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service, client="delete-me")
    photo_path = tmp_path / "stories" / story["id"] / "source.jpg"
    assert photo_path.is_file()

    assert service.delete_story(story["id"]) == {"ok": True, "story_id": story["id"]}
    assert not photo_path.exists()
    with pytest.raises(NotFoundError):
        service.story(story["id"])

    scheduled = create(service, client="scheduled-delete")
    with service.store.tx() as db:
        db.execute("UPDATE stories SET state='scheduled' WHERE id=?", (scheduled["id"],))
    with pytest.raises(ConflictError) as exc:
        service.delete_story(scheduled["id"])
    assert exc.value.code == "scheduled_story_delete_blocked"

def test_candidate_catalog_prioritizes_nearby_visible_objects():
    osm = {
        "reverse": {},
        "nearby": [
            {
                "type": "way",
                "id": 300,
                "tags": {"name": "Дальний памятник"},
                "distance_m": 390.0,
            },
            {
                "type": "node",
                "id": 100,
                "tags": {"name": "Ближние ворота"},
                "distance_m": 45.0,
            },
        ],
    }
    wikipedia = [
        {
            "pageid": 77,
            "title": "Средний объект",
            "url": "https://ru.wikipedia.org/wiki/Middle",
            "extract": "",
            "distance_m": 120.0,
        }
    ]

    candidates = MvpResearchStreetStoryService._candidate_catalog(osm, wikipedia)

    assert [item["name"] for item in candidates[:3]] == [
        "Ближние ворота",
        "Средний объект",
        "Дальний памятник",
    ]
