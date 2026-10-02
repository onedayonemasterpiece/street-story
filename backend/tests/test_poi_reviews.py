import pytest

from street_story.db import Store
from street_story.poi_external import ingest_poi_evidence
from street_story.poi_reviews import (
    PoiReviewAccessError,
    list_review_cases_for_actor,
    review_case_projection,
    sync_review_cases,
)
from street_story.poi_semantic import apply_poi_semantic_analysis


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
            "semantic_key": f"construction:{text}",
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
    ingest_poi_evidence(
        store,
        event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            key="knowledge:left",
            text="Ворота построены в 1843 году.",
        ),
    )
    second = ingest_poi_evidence(
        store,
        event(
            event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            key="knowledge:right",
            text="Ворота построены в 1850 году.",
        ),
    )
    candidate_id = second["semantic_candidate_ids"][0]
    return apply_poi_semantic_analysis(
        store,
        candidate_id,
        {
            "contract_version": "poi.semantic_review.v1",
            "candidate_id": candidate_id,
            "classification": "conflict",
            "relation": "uncertain",
            "suggested_resolution": "unresolved",
            "confidence": 0.82,
            "rationale": "Mira считает, что утверждения требуют экспертной проверки.",
        },
    )


def test_conflict_materializes_projects_hub_compatible_review_case(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    result = create_conflict(store)

    assert result["conflict_id"]
    assert len(result["review_case_ids"]) == 1

    case = review_case_projection(
        store,
        result["review_case_ids"][0],
        actor_sub=OWNER_A,
    )

    assert case["contract_version"] == "poi.review_case.v1"
    assert case["relation"] == "uncertain"
    assert len(case["claims"]) == 2
    assert case["required_reviews"] == 2
    assert case["required_expertise"]["subject"] == ["construction"]
    assert case["scope"]["visibility"] == "private"
    assert case["detector_suggestion"]["source"] == "mira_semantic_review"


def test_private_review_case_is_hidden_from_other_user(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    result = create_conflict(store)
    case_id = result["review_case_ids"][0]

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
    result = create_conflict(store)
    conflict_id = result["conflict_id"]

    first = sync_review_cases(store, [conflict_id])
    second_sync = sync_review_cases(store, [conflict_id])

    assert first == second_sync
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM poi_review_cases").fetchone()[0]
            == 1
        )
