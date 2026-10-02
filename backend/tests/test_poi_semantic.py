import pytest

from street_story.db import Store
from street_story.poi_external import ingest_poi_evidence
from street_story.poi_reviews import review_case_projection
from street_story.poi_semantic import (
    PoiSemanticAccessError,
    PoiSemanticConflict,
    apply_poi_semantic_analysis,
    semantic_candidate_projection,
)


OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"
DOCUMENT_ID = "33333333-3333-3333-3333-333333333333"
PAGE_ID = "44444444-4444-4444-4444-444444444444"
REGION_ID = "55555555-5555-5555-5555-555555555555"


def event(*, event_id, candidate_id, key, text, score=90):
    return {
        "contract_version": "poi.fact_evidence.v1",
        "event_id": event_id,
        "idempotency_key": key,
        "producer": "regional_knowledge",
        "scope": {
            "visibility": "private",
            "owner_sub": OWNER_A,
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
            "evidence_verification_score": score,
            "score_policy_version": "book-evidence-v1",
        },
    }


def candidate(store):
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
    return second["semantic_candidate_ids"][0]


def conflict_analysis(candidate_id):
    return {
        "contract_version": "poi.semantic_review.v1",
        "candidate_id": candidate_id,
        "classification": "conflict",
        "relation": "contradiction",
        "suggested_resolution": "unresolved",
        "confidence": 0.87,
        "rationale": "Даты описывают один и тот же тип события и требуют проверки.",
    }


def test_candidate_projection_is_scope_checked_and_model_ready(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    candidate_id = candidate(store)

    projection = semantic_candidate_projection(
        store,
        candidate_id,
        actor_sub=OWNER_A,
    )
    assert projection["contract_version"] == "poi.semantic_candidate.v1"
    assert projection["left"]["kind"] == "construction"
    assert projection["right"]["kind"] == "construction"
    assert projection["left"]["evidence_refs"]
    assert projection["right"]["evidence_refs"]

    with pytest.raises(PoiSemanticAccessError):
        semantic_candidate_projection(
            store,
            candidate_id,
            actor_sub=OWNER_B,
        )


def test_model_can_confirm_no_conflict_without_contesting_claims(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    candidate_id = candidate(store)
    result = apply_poi_semantic_analysis(
        store,
        candidate_id,
        {
            "contract_version": "poi.semantic_review.v1",
            "candidate_id": candidate_id,
            "classification": "no_conflict",
            "relation": None,
            "suggested_resolution": None,
            "confidence": 0.92,
            "rationale": "Даты относятся к разным этапам строительства.",
        },
    )

    assert result["classification"] == "no_conflict"
    assert result["conflict_id"] is None
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_conflicts").fetchone()[0] == 0
        statuses = {
            row["status"]
            for row in db.execute("SELECT status FROM poi_claims")
        }
        assert statuses == {"candidate"}


def test_only_model_confirmed_conflict_creates_review_case(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    candidate_id = candidate(store)
    result = apply_poi_semantic_analysis(
        store,
        candidate_id,
        conflict_analysis(candidate_id),
    )

    assert result["classification"] == "conflict"
    assert result["conflict_id"]
    assert len(result["review_case_ids"]) == 1

    with store.connection() as db:
        conflict = db.execute("SELECT * FROM poi_conflicts").fetchone()
        assert conflict["relation"] == "contradiction"
        assert conflict["status"] == "open"
        statuses = {
            row["status"]
            for row in db.execute("SELECT status FROM poi_claims")
        }
        assert statuses == {"contested"}

    case = review_case_projection(
        store,
        result["review_case_ids"][0],
        actor_sub=OWNER_A,
    )
    assert case["relation"] == "contradiction"
    assert case["detector_suggestion"]["source"] == "mira_semantic_review"
    assert (
        case["detector_suggestion"]["analysis"]["classification"]
        == "conflict"
    )


def test_semantic_analysis_is_idempotent_but_not_mutable(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    candidate_id = candidate(store)
    payload = conflict_analysis(candidate_id)

    first = apply_poi_semantic_analysis(store, candidate_id, payload)
    replay = apply_poi_semantic_analysis(store, candidate_id, payload)
    assert replay["replayed"] is True
    assert replay["conflict_id"] == first["conflict_id"]

    changed = dict(payload)
    changed["relation"] = "source_disagreement"
    with pytest.raises(PoiSemanticConflict):
        apply_poi_semantic_analysis(store, candidate_id, changed)
