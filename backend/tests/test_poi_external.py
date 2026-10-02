import copy
import uuid

import pytest

from street_story.db import Store
from street_story.poi_external import (
    PoiEvidenceConflict,
    ingest_poi_evidence,
    normalize_poi_evidence,
)


OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"
DOCUMENT_ID = "33333333-3333-3333-3333-333333333333"
PAGE_ID = "44444444-4444-4444-4444-444444444444"
REGION_ID = "55555555-5555-5555-5555-555555555555"


def event(
    *,
    event_id=None,
    candidate_id=None,
    idempotency_key=None,
    owner=OWNER_A,
    visibility="private",
    workspace_id=None,
    external_ids=None,
    names=None,
    text="Ворота построены в 1843 году.",
    kind="construction",
):
    event_id = event_id or str(uuid.uuid4())
    candidate_id = candidate_id or str(uuid.uuid4())
    return {
        "contract_version": "poi.fact_evidence.v1",
        "event_id": event_id,
        "idempotency_key": idempotency_key or f"knowledge:{DOCUMENT_ID}:1:{candidate_id}",
        "producer": "regional_knowledge",
        "scope": {
            "visibility": visibility,
            "owner_sub": owner,
            "workspace_id": workspace_id,
        },
        "source": {
            "document_ref": f"knowledge://documents/{DOCUMENT_ID}",
            "revision": 1,
            "title": "История Кёнигсберга",
            "publication_year": 1936,
        },
        "poi_locator": {
            "names": names or ["Королевские ворота", "Königstor"],
            "external_ids": (
                {"wikidata": "Q12345"}
                if external_ids is None
                else external_ids
            ),
            "latitude": None,
            "longitude": None,
        },
        "claim": {
            "candidate_id": candidate_id,
            "semantic_key": "producer-key",
            "kind": kind,
            "text": text,
            "time_scope": None,
        },
        "evidence": {
            "evidence_ref": f"knowledge://evidence/{candidate_id}",
            "page_ids": [PAGE_ID],
            "region_ids": [REGION_ID],
            "source_family_id": "unknown",
            "author_profile_refs": [],
            "author_subject_authority": None,
            "publication_method_score": 85,
            "provenance_precision_score": 100,
            "evidence_verification_score": None,
            "score_policy_version": "book-evidence-v1",
        },
    }


def test_normalizer_recomputes_street_story_semantics():
    payload = event()
    normalized = normalize_poi_evidence(payload)
    assert normalized["claim"]["kind"] == "construction"
    assert normalized["claim"]["semantic_key"] == "construction:1843"
    assert normalized["claim"]["producer_semantic_key"] == "producer-key"
    assert normalized["evidence"]["source_family_id"] == "unknown"


def test_idempotent_external_evidence_creates_candidate_poi_and_claim(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    payload = event(event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    first = ingest_poi_evidence(store, payload)
    again = ingest_poi_evidence(store, payload)

    assert first["state"] == "attached"
    assert first["poi_id"]
    assert first["claim_id"]
    assert first["replayed"] is False
    assert again == {**first, "replayed": True, "conflict_ids": []}

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pois").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM poi_external_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM poi_claims").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM poi_claim_evidence").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM poi_aliases WHERE namespace='wikidata' AND normalized_value='q12345'"
        ).fetchone()[0] == 1

    changed = copy.deepcopy(payload)
    changed["claim"]["text"] = "Ворота построены в 1850 году."
    with pytest.raises(PoiEvidenceConflict):
        ingest_poi_evidence(store, changed)


def test_name_only_unknown_place_stays_unresolved_without_creating_poi(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    payload = event(
        external_ids={},
        names=["Неоднозначный старый объект"],
    )
    result = ingest_poi_evidence(store, payload)

    assert result["state"] == "unresolved_identity"
    assert result["poi_id"] is None
    assert result["claim_id"] is None

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pois").fetchone()[0] == 0
        row = db.execute("SELECT state,poi_id,claim_id FROM poi_external_events").fetchone()
        assert row["state"] == "unresolved_identity"
        assert row["poi_id"] is None
        assert row["claim_id"] is None


def test_same_external_id_merges_poi_and_opens_uncertain_conflict(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    first = ingest_poi_evidence(
        store,
        event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            idempotency_key="knowledge:first",
            text="Ворота построены в 1843 году.",
        ),
    )
    second = ingest_poi_evidence(
        store,
        event(
            event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            idempotency_key="knowledge:second",
            text="Ворота построены в 1850 году.",
        ),
    )

    assert second["poi_id"] == first["poi_id"]
    assert len(second["conflict_ids"]) == 1

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pois").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM poi_claims").fetchone()[0] == 2
        conflict = db.execute("SELECT * FROM poi_conflicts").fetchone()
        assert conflict["relation"] == "uncertain"
        assert conflict["status"] == "open"
        statuses = {
            row["status"]
            for row in db.execute("SELECT status FROM poi_claims")
        }
        assert statuses == {"contested"}


def test_private_evidence_from_different_owners_does_not_cross_conflict(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    first = ingest_poi_evidence(
        store,
        event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            idempotency_key="knowledge:a",
            owner=OWNER_A,
            text="Ворота построены в 1843 году.",
        ),
    )
    second = ingest_poi_evidence(
        store,
        event(
            event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            idempotency_key="knowledge:b",
            owner=OWNER_B,
            text="Ворота построены в 1850 году.",
        ),
    )

    assert second["poi_id"] == first["poi_id"]
    assert second["conflict_ids"] == []
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_conflicts").fetchone()[0] == 0


def test_public_evidence_is_visible_to_private_owner_conflict_check(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    public_payload = event(
        event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        candidate_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        idempotency_key="knowledge:public",
        visibility="public",
        owner=OWNER_A,
        text="Ворота построены в 1843 году.",
    )
    private_payload = event(
        event_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        candidate_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        idempotency_key="knowledge:private",
        visibility="private",
        owner=OWNER_B,
        text="Ворота построены в 1850 году.",
    )
    ingest_poi_evidence(store, public_payload)
    second = ingest_poi_evidence(store, private_payload)

    assert len(second["conflict_ids"]) == 1
