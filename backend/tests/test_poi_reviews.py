import pytest
from types import SimpleNamespace

from street_story.db import Store
from street_story.fact_conflicts import persist_fact_conflicts
from street_story.poi_external import ingest_poi_evidence
from street_story.poi_reviews import (
    PoiReviewAccessError,
    list_review_cases_for_actor,
    review_case_projection,
    sync_review_cases,
)


OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"
DOCUMENT_ID = "33333333-3333-3333-3333-333333333333"
PAGE_ID = "44444444-4444-4444-4444-444444444444"
REGION_ID = "55555555-5555-5555-5555-555555555555"


def event(
    *,
    event_id,
    candidate_id,
    key,
    text,
    visibility="private",
    owner=OWNER_A,
    verification_score=90,
    semantic_key="producer-key",
):
    return {
        "contract_version": "poi.fact_evidence.v1",
        "event_id": event_id,
        "idempotency_key": key,
        "producer": "regional_knowledge",
        "scope": {
            "visibility": visibility,
            "owner_sub": owner,
            "workspace_id": None,
        },
        "source": {
            "document_ref": f"knowledge://documents/{DOCUMENT_ID}",
            "revision": 1,
            "title": "История Кёнигсберга",
            "publication_year": 1936,
        },
        "poi_locator": {
            "names": ["Королевские ворота", "Königstor"],
            "external_ids": {"wikidata": "Q12345"},
            "latitude": None,
            "longitude": None,
        },
        "claim": {
            "candidate_id": candidate_id,
            "semantic_key": semantic_key,
            "kind": "construction",
            "text": text,
            "time_scope": None,
        },
        "evidence": {
            "evidence_ref": f"knowledge://evidence/{candidate_id}",
            "page_ids": [PAGE_ID],
            "region_ids": [REGION_ID],
            "source_family_id": "unknown",
            "author_profile_refs": [],
            "author_subject_authority": 90,
            "publication_method_score": 85,
            "provenance_precision_score": 100,
            "evidence_verification_score": verification_score,
            "score_policy_version": "book-evidence-v1",
        },
    }


def create_conflict(store):
    first = ingest_poi_evidence(
        store,
        event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            key="knowledge:left",
            text="Ворота построены в 1843 году.",
            semantic_key="construction-date-1843",
        ),
    )
    second = ingest_poi_evidence(
        store,
        event(
            event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            key="knowledge:right",
            text="Ворота построены в 1850 году.",
            semantic_key="construction-date-1850",
        ),
    )
    assert first["poi_id"] == second["poi_id"]
    assert first["claim_id"] and second["claim_id"]

    story_id = "story_model_conflict"
    now = store.now()
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                story_id,
                "client-model-conflict",
                "a" * 64,
                "image/jpeg",
                "/tmp/photo.jpg",
                "voice-chunks-v2",
                "review",
                "{}",
                now,
                now,
            ),
        )

    evidence = {
        "source_count": 1,
        "domain_count": 0,
        "official": False,
        "official_urls": [],
        "source_urls": [],
        "evidence_refs": ["knowledge://evidence/test"],
        "supports": [],
    }
    persist_fact_conflicts(
        SimpleNamespace(store=store),
        story_id,
        str(first["poi_id"]),
        [{
            "conflict_id": "conflict_model_detected",
            "left_fact_id": str(first["claim_id"]),
            "right_fact_id": str(second["claim_id"]),
            "left_text": "Ворота построены в 1843 году.",
            "right_text": "Ворота построены в 1850 году.",
            "relation": "contradiction",
            "detector_confidence": 0.96,
            "suggested_resolution": "unresolved",
            "suggested_fact_id": None,
            "detector_rationale": "Модель выявила две взаимоисключающие даты в одном временном смысле.",
            "evidence": {"left": evidence, "right": evidence},
            "poi_id": str(first["poi_id"]),
        }],
        detector="test_model",
    )
    with store.connection() as db:
        conflict_ids = [
            str(row["conflict_id"])
            for row in db.execute("SELECT conflict_id FROM poi_conflicts ORDER BY conflict_id")
        ]
        review_case_ids = [
            str(row["review_case_id"])
            for row in db.execute("SELECT review_case_id FROM poi_review_cases ORDER BY review_case_id")
        ]
    return {
        "conflict_ids": conflict_ids,
        "review_case_ids": review_case_ids,
    }


def test_conflict_materializes_projects_hub_compatible_review_case(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    second = create_conflict(store)

    assert len(second["conflict_ids"]) == 1
    assert len(second["review_case_ids"]) == 1

    case = review_case_projection(
        store,
        second["review_case_ids"][0],
        actor_sub=OWNER_A,
    )

    assert case["contract_version"] == "poi.review_case.v1"
    assert case["relation"] == "contradiction"
    assert len(case["claims"]) == 2
    assert case["required_reviews"] == 2
    assert case["required_expertise"]["subject"] == ["construction"]
    assert case["scope"]["visibility"] == "private"


def test_private_review_case_is_hidden_from_other_user(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    second = create_conflict(store)
    case_id = second["review_case_ids"][0]

    with pytest.raises(PoiReviewAccessError):
        review_case_projection(
            store,
            case_id,
            actor_sub=OWNER_B,
        )

    assert list_review_cases_for_actor(
        store,
        actor_sub=OWNER_B,
    ) == []


def test_review_sync_is_idempotent(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    second = create_conflict(store)
    conflict_id = second["conflict_ids"][0]

    first = sync_review_cases(store, [conflict_id])
    second_sync = sync_review_cases(store, [conflict_id])

    assert first == second_sync
    with store.connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM poi_review_cases"
        ).fetchone()[0] == 1