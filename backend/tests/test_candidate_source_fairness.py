import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story import article_media
from street_story.live import StreetStoryLiveAdapter
from test_identity_lifecycle import make_service
from test_reference_image_codec import jpeg


def setup(tmp_path):
    svc, _ = make_service(tmp_path)
    photo = jpeg((640, 480))
    story = svc.create_story(
        key="fairness",
        client_story_id="fairness",
        photo_sha256="opaque-upload-token",
        photo_mime_type="image/jpeg",
        photo_bytes=photo,
        voice_protocol="voice-chunks-v2",
        lat=54.7,
        lon=20.5,
    )
    physical = {
        "candidate_id": "wiki:123",
        "name": "A candidate",
        "url": "https://en.wikipedia.org/wiki/An_actual_article",
        "reference_image_urls": ["https://upload.wikimedia.org/initial.jpg"],
    }
    gallery = [
        {
            "candidate_id": "web:city",
            "name": "City",
            "url": "https://city.example/gallery",
            "discovery": "web_article_media",
            "reference_image_urls": [f"https://city.example/frame-{i}.jpg"],
            "reference_batch": True,
        }
        for i in range(172)
    ]
    state = {
        "generation": 0,
        "photo_sha256": story["photo_sha256"],
        "control_revision": 0,
        "queue": gallery,
        "reviewed_reference_ids": ["finished-ref"],
        "browser_budget": {"remaining": 2},
        "sources": {
            "https://city.example/gallery": {"source": {"url": "https://city.example/gallery"}, "status": "partial", "attempts": 1},
            physical["url"]: {
                "source": {"url": physical["url"], "candidate_id": physical["candidate_id"]},
                "status": "pending",
                "attempts": 0,
            },
        },
        "searches": {},
        "fetch_failures": [],
        "query": "Candidate",
        "query_seed": "Candidate",
        "verdict_history": [{"comparison_id": "completed", "status": "mismatch"}],
    }
    with svc.store.tx() as db:
        db.execute(
            "UPDATE stories SET research_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "identity_generation": 0,
                        "visual_identity": {"status": "uncertain", "candidates": [physical]},
                        "visual_search_operation": state,
                    }
                ),
                story["id"],
            ),
        )
    adapter = StreetStoryLiveAdapter(svc, lambda *_: None, lambda *_: None)
    session = SimpleNamespace(id="headless:fairness", resource_id=story["id"], model="gemini-3.8-live", state={})
    return svc, story, physical, gallery, adapter, session


@pytest.mark.asyncio
async def test_unread_exact_article_preempts_broad_gallery_without_discarding_frames(tmp_path, monkeypatch):
    svc, story, physical, gallery, adapter, session = setup(tmp_path)
    acquired, loaded = [], []
    new = {
        "candidate_id": physical["candidate_id"],
        "name": physical["name"],
        "url": physical["url"],
        "discovery": "wikipedia_article_media",
        "reference_image_urls": ["https://upload.wikimedia.org/new.jpg"],
    }

    async def articles(_svc, _story, sources, _excluded, *, receipts):
        acquired.extend(sources)
        receipts.append({"status": "completed"})
        return [new]

    async def images(candidates, limit, *, story_id, evidence):
        candidate = candidates[0]
        loaded.append(candidate)
        evidence.append(
            {
                "candidate_id": candidate["candidate_id"],
                "source_url": candidate["reference_image_urls"][0],
                "article_url": candidate["url"],
            }
        )
        return [(candidate["candidate_id"], "image/jpeg", candidate["reference_image_urls"][0])]

    monkeypatch.setattr(article_media, "article_candidates", articles)
    svc._candidate_reference_images = images
    result = await adapter._compare_place_images(session, {})
    assert [p["url"] for p in acquired] == [physical["url"]]
    assert loaded[0]["candidate_id"] == physical["candidate_id"]
    assert result["references"][0]["candidate_id"] == physical["candidate_id"]
    # Acquiring a better reference is not a comparison or an accepted identity.
    current = svc.story(story["id"])
    assert current["visual_identity"]["status"] == "uncertain"
    assert current["identity_progress"].get("images_reviewed_count", 0) == 0
    state = session.state["visual_comparison"]
    assert state["queue"] == gallery
    assert state["reviewed_reference_ids"] == ["finished-ref"]
    assert state["verdict_history"] == [{"comparison_id": "completed", "status": "mismatch"}]
    with svc.store.connection() as db:
        saved = json.loads(db.execute("SELECT research_json FROM stories WHERE id=?", (story["id"],)).fetchone()[0])[
            "visual_search_operation"
        ]
    assert saved["queue"] == gallery
    assert saved["pending_descriptor"]["candidates"][0]["reference_image_urls"] == new["reference_image_urls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["unknown", "submitted", "prompt_intent", "abort_outcome_unknown"])
async def test_unknown_receipt_preserves_front_and_does_not_prefetch(tmp_path, monkeypatch, phase):
    svc, story, physical, gallery, adapter, session = setup(tmp_path)
    receipt = json.dumps({"phase": phase, "binding": {"photo_sha256": story["photo_sha256"], "generation": 0}})
    with svc.store.tx() as db:
        db.execute(
            "INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)",
            ("rattempt_unknown", "same-unit", story["id"], "vision_native", receipt, 1, 1),
        )

    async def forbidden(*_, **__):
        pytest.fail("Do not preempt an unacknowledged dispatch")

    monkeypatch.setattr(article_media, "article_candidates", forbidden)

    from street_story.errors import RetryableProviderError
    svc._candidate_reference_images = forbidden
    with pytest.raises(RetryableProviderError, match='research_visual_outcome_unknown'):
        await adapter._compare_place_images(session, {})
    with svc.store.connection() as db:
        assert (
            db.execute("SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?", ("rattempt_unknown",)).fetchone()[0]
            == receipt
        )
    assert session.state["visual_comparison"]["queue"] == gallery
    assert not session.state["visual_comparison"].get("pending")


@pytest.mark.asyncio
async def test_failed_priority_page_keeps_gallery_and_respects_page_budget(tmp_path, monkeypatch):
    svc, story, physical, gallery, adapter, session = setup(tmp_path)
    calls = []

    async def unavailable(_svc, _story, sources, _excluded, *, receipts):
        calls.extend(sources)
        receipts.append({"status": "temporary_failure"})
        return []

    monkeypatch.setattr(article_media, "article_candidates", unavailable)

    async def images(candidates, limit, *, story_id, evidence):
        evidence.append(
            {
                "candidate_id": "web:city",
                "source_url": gallery[0]["reference_image_urls"][0],
            }
        )
        return [("web:city", "image/jpeg", gallery[0]["reference_image_urls"][0])]

    svc._candidate_reference_images = images
    await adapter._compare_place_images(session, {}, page_budget=1)
    assert len(calls) == 1
    assert session.state["visual_comparison"]["queue"] == gallery[1:]
    assert session.state["visual_comparison"]["sources"][physical["url"]]["retry_at"] > svc.store.now()


@pytest.mark.asyncio
async def test_stop_fences_priority_acquisition_before_any_read(tmp_path, monkeypatch):
    from street_story.research_control import stop_research
    from street_story.service import ConflictError

    svc, story, physical, gallery, adapter, session = setup(tmp_path)
    stop_research(svc, story["id"], purpose="identity")

    async def forbidden(*_, **__):
        pytest.fail("Stopped acquisition must not run")

    monkeypatch.setattr(article_media, "article_candidates", forbidden)
    with pytest.raises(ConflictError):
        await adapter._compare_place_images(session, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_cache", [True, False])
async def test_linked_cached_acquisition_priority_is_outcome_neutral_and_hash_fenced(tmp_path, monkeypatch, valid_cache):
    import base64

    svc, story, physical, gallery, adapter, session = setup(tmp_path)
    linked_urls = ["https://a.example/uncached-article", "https://z.example/acquired-article"]
    with svc.store.tx() as db:
        research = json.loads(db.execute("SELECT research_json FROM stories WHERE id=?", (story["id"],)).fetchone()[0])
        sources = research["visual_search_operation"]["sources"]
        sources[physical["url"]]["status"] = "completed"
        for url in linked_urls:
            sources[url] = {
                "source": {"url": url, "discovery_provider": "poi_memory", "memory_candidate_ids": [physical["candidate_id"]]},
                "status": "pending",
                "attempts": 0,
            }
        db.execute("UPDATE stories SET research_json=? WHERE id=?", (json.dumps(research), story["id"]))
    body = b"<article>Already acquired source</article>"
    svc.store.cache_put(
        "public-article-acquisition-v1:" + hashlib.sha256(linked_urls[1].encode()).hexdigest(),
        {
            "final_url": linked_urls[1],
            "body": base64.b64encode(body).decode(),
            "sha256": hashlib.sha256(body).hexdigest() if valid_cache else "0" * 64,
        },
        3600,
    )
    called = []

    async def articles(_svc, _story, sources, _excluded, *, receipts):
        called.extend(p["url"] for p in sources)
        receipts.append({"status": "completed"})
        return [
            {
                "candidate_id": "web:linked",
                "name": "Linked article",
                "url": sources[0]["url"],
                "discovery": "web_article_media",
                "reference_image_urls": ["https://z.example/media.jpg"],
            }
        ]

    monkeypatch.setattr(article_media, "article_candidates", articles)

    async def images(candidates, limit, *, story_id, evidence):
        evidence.append(
            {
                "candidate_id": "web:linked",
                "source_url": "https://z.example/media.jpg",
            }
        )
        return [("web:linked", "image/jpeg", "https://z.example/media.jpg")]

    svc._candidate_reference_images = images
    await adapter._compare_place_images(session, {})
    assert called == [linked_urls[1 if valid_cache else 0]]
    assert session.state["visual_comparison"]["queue"] == gallery
    assert svc.story(story["id"])["visual_identity"]["status"] == "uncertain"
