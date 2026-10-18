import pytest

from street_story.db import Store
from street_story.poi_external import ingest_poi_evidence
from street_story.poi_review_actions import (
    PoiReviewActionAccessError,
    PoiReviewActionConflict,
    accept_review_case,
    request_review_research,
    resolve_review_case,
)
from street_story.poi_reviews import review_case_projection
from street_story.poi_semantic import apply_poi_semantic_analysis


OWNER = "11111111-1111-1111-1111-111111111111"
EXPERT_A = "expert:alpha"
EXPERT_B = "expert:beta"
DOCUMENT_ID = "33333333-3333-3333-3333-333333333333"
PAGE_ID = "44444444-4444-4444-4444-444444444444"
REGION_ID = "55555555-5555-5555-5555-555555555555"


def evidence_event(*, event_id, candidate_id, key, text):
    return {
        "contract_version": "poi.fact_evidence.v1",
        "event_id": event_id,
        "idempotency_key": key,
        "producer": "regional_knowledge",
        "scope": {
            "visibility": "public",
            "owner_sub": OWNER,
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
            "evidence_verification_score": 90,
            "score_policy_version": "book-evidence-v1",
        },
    }


def create_public_review_case(store):
    ingest_poi_evidence(
        store,
        evidence_event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            key="knowledge:left",
            text="Ворота построены в 1843 году.",
        ),
    )
    second = ingest_poi_evidence(
        store,
        evidence_event(
            event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            key="knowledge:right",
            text="Ворота построены в 1850 году.",
        ),
    )
    candidate_id = second["semantic_candidate_ids"][0]
    result = apply_poi_semantic_analysis(
        store,
        candidate_id,
        {
            "contract_version": "poi.semantic_review.v1",
            "candidate_id": candidate_id,
            "classification": "conflict",
            "relation": "contradiction",
            "suggested_resolution": "unresolved",
            "confidence": 0.9,
            "rationale": "Mira обнаружила конкурирующие даты одного события.",
        },
    )
    return result["review_case_ids"][0], result["conflict_id"]


def expertise(subject):
    return {
        "subject": subject,
        "verification_state": "verified",
        "geography": ["kaliningrad_oblast"],
        "periods": [],
        "subjects": ["construction"],
        "languages": [],
        "institutional_roles": [],
    }


def test_accept_is_revision_guarded_and_idempotent(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    case_id, _ = create_public_review_case(store)

    first = accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expertise_snapshot=expertise(EXPERT_A),
        expected_revision=1,
        command_id="accept-alpha",
    )
    replay = accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expertise_snapshot=expertise(EXPERT_A),
        expected_revision=1,
        command_id="accept-alpha",
    )

    assert first == replay
    assert first["case_revision"] == 2
    assert first["assignment_revision"] == 1

    with pytest.raises(PoiReviewActionConflict):
        accept_review_case(
            store,
            case_id,
            actor_sub=EXPERT_B,
            expertise_snapshot=expertise(EXPERT_B),
            expected_revision=1,
            command_id="accept-stale",
        )


def test_accept_rejects_unverified_or_wrong_expertise(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    case_id, _ = create_public_review_case(store)

    broken = expertise(EXPERT_A)
    broken["subjects"] = ["architecture"]
    with pytest.raises(PoiReviewActionAccessError):
        accept_review_case(
            store,
            case_id,
            actor_sub=EXPERT_A,
            expertise_snapshot=broken,
            expected_revision=1,
            command_id="accept-bad-expertise",
        )


def test_two_independent_matching_decisions_apply_human_consensus(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    case_id, conflict_id = create_public_review_case(store)
    initial_projection = review_case_projection(
        store,
        case_id,
        actor_sub=None,
    )
    left_text = initial_projection["claims"][0]["text"]
    right_text = initial_projection["claims"][1]["text"]

    accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expertise_snapshot=expertise(EXPERT_A),
        expected_revision=1,
        command_id="accept-alpha",
    )
    accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_B,
        expertise_snapshot=expertise(EXPERT_B),
        expected_revision=2,
        command_id="accept-beta",
    )

    first = resolve_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expected_revision=3,
        resolution="prefer_left",
        rationale="Левый источник имеет более убедительный первичный контекст.",
        confidence=0.8,
        command_id="resolve-alpha",
    )
    assert first["status"] == "in_review"
    assert first["resolution_applied"] is False
    assert first["submitted_reviews"] == 1

    second = resolve_review_case(
        store,
        case_id,
        actor_sub=EXPERT_B,
        expected_revision=4,
        resolution="prefer_left",
        rationale="Независимо подтверждаю левую дату по источнику.",
        confidence=0.75,
        command_id="resolve-beta",
    )
    assert second["status"] == "resolved"
    assert second["consensus"] is True
    assert second["resolution_applied"] is True

    with store.connection() as db:
        conflict = db.execute(
            "SELECT status FROM poi_conflicts WHERE conflict_id=?",
            (conflict_id,),
        ).fetchone()
        assert conflict["status"] == "resolved"
        statuses = {
            row["text"]: row["status"]
            for row in db.execute("SELECT text,status FROM poi_claims")
        }
        assert statuses[left_text] == "accepted"
        assert statuses[right_text] == "rejected"

    projection = review_case_projection(
        store,
        case_id,
        actor_sub=None,
    )
    assert projection["status"] == "resolved"
    assert projection["case_revision"] == 5


def test_disagreement_never_becomes_majority_vote(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    case_id, conflict_id = create_public_review_case(store)

    accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expertise_snapshot=expertise(EXPERT_A),
        expected_revision=1,
        command_id="accept-alpha",
    )
    accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_B,
        expertise_snapshot=expertise(EXPERT_B),
        expected_revision=2,
        command_id="accept-beta",
    )
    resolve_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expected_revision=3,
        resolution="prefer_left",
        rationale="Считаю левый источник более убедительным.",
        confidence=0.7,
        command_id="resolve-alpha",
    )
    second = resolve_review_case(
        store,
        case_id,
        actor_sub=EXPERT_B,
        expected_revision=4,
        resolution="prefer_right",
        rationale="Считаю правый источник более убедительным.",
        confidence=0.7,
        command_id="resolve-beta",
    )

    assert second["status"] == "in_review"
    assert second["consensus"] is False
    assert second["resolution_applied"] is False
    with store.connection() as db:
        conflict = db.execute(
            "SELECT status FROM poi_conflicts WHERE conflict_id=?",
            (conflict_id,),
        ).fetchone()
        assert conflict["status"] == "open"


def test_need_more_sources_defers_case_without_fact_arbitration(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    case_id, conflict_id = create_public_review_case(store)

    accept_review_case(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expertise_snapshot=expertise(EXPERT_A),
        expected_revision=1,
        command_id="accept-alpha",
    )
    receipt = request_review_research(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expected_revision=2,
        rationale="Нужен независимый довоенный источник с точной датой.",
        command_id="research-alpha",
    )
    replay = request_review_research(
        store,
        case_id,
        actor_sub=EXPERT_A,
        expected_revision=2,
        rationale="Нужен независимый довоенный источник с точной датой.",
        command_id="research-alpha",
    )

    assert receipt == replay
    assert receipt["status"] == "deferred"
    assert receipt["resolution_applied"] is False
    with store.connection() as db:
        conflict = db.execute(
            "SELECT status FROM poi_conflicts WHERE conflict_id=?",
            (conflict_id,),
        ).fetchone()
        assert conflict["status"] == "open"
