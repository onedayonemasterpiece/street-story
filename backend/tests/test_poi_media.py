import copy
import uuid

import pytest

from street_story.db import Store
from street_story.poi_external import PoiEvidenceConflict
from street_story.poi_media import (
    ingest_poi_media_evidence,
    normalize_poi_media_evidence,
    poi_media_for_actor,
)


OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"
DOCUMENT_ID = "33333333-3333-3333-3333-333333333333"
PAGE_ID = "44444444-4444-4444-4444-444444444444"
REGION_ID = "55555555-5555-5555-5555-555555555555"
ILLUSTRATION_ID = "66666666-6666-6666-6666-666666666666"
LINK_ID = "77777777-7777-7777-7777-777777777777"


def event(
    *,
    event_id=None,
    idempotency_key=None,
    owner=OWNER_A,
    visibility="private",
    workspace_id=None,
    external_ids=None,
    rights_status="unknown",
    media_visibility=None,
    vibepublish_entry_ref=None,
):
    event_id = event_id or str(uuid.uuid4())
    return {
        "contract_version": "poi.media_evidence.v1",
        "event_id": event_id,
        "idempotency_key": idempotency_key or f"knowledge-media:{event_id}",
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
            "publication_year": 1930,
        },
        "poi_locator": {
            "names": ["Королевские ворота", "Königstor"],
            "external_ids": (
                {"wikidata": "Q12345"}
                if external_ids is None
                else external_ids
            ),
            "latitude": None,
            "longitude": None,
        },
        "media": {
            "link_id": LINK_ID,
            "illustration_id": ILLUSTRATION_ID,
            "illustration_ref": (
                f"knowledge://illustrations/{ILLUSTRATION_ID}"
            ),
            "relation": "depicts",
            "time_scope": "early-20th-century",
            "kind": "photo",
            "caption": "Königstor. Historische Aufnahme.",
            "page_id": PAGE_ID,
            "source_region_id": REGION_ID,
            "caption_region_ids": [],
            "source_crop_sha256": "a" * 64,
            "rights_status": rights_status,
            "visibility": media_visibility or visibility,
            "vibepublish_entry_ref": vibepublish_entry_ref,
        },
        "evidence": {
            "page_ids": [PAGE_ID],
            "region_ids": [REGION_ID],
            "source_family_id": "unknown",
        },
    }


def test_media_normalizer_rejects_scope_media_visibility_mismatch():
    payload = event(visibility="private", media_visibility="public")
    with pytest.raises(ValueError):
        normalize_poi_media_evidence(payload)


def test_idempotent_media_ingest_reuses_poi_and_preserves_reference(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    payload = event(
        event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        idempotency_key="knowledge-media:one",
    )

    first = ingest_poi_media_evidence(store, payload)
    again = ingest_poi_media_evidence(store, payload)

    assert first["state"] == "attached"
    assert first["poi_id"]
    assert first["illustration_ref"] == (
        f"knowledge://illustrations/{ILLUSTRATION_ID}"
    )
    assert first["replayed"] is False
    assert again == {**first, "replayed": True}

    changed = copy.deepcopy(payload)
    changed["media"]["caption"] = "Changed"
    with pytest.raises(PoiEvidenceConflict):
        ingest_poi_media_evidence(store, changed)


def test_private_media_is_visible_only_to_owner(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    result = ingest_poi_media_evidence(
        store,
        event(event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    )

    owner_rows = poi_media_for_actor(
        store,
        result["poi_id"],
        owner_sub=OWNER_A,
    )
    stranger_rows = poi_media_for_actor(
        store,
        result["poi_id"],
        owner_sub=OWNER_B,
    )

    assert len(owner_rows) == 1
    assert stranger_rows == []


def test_public_publishable_media_requires_verified_rights(tmp_path):
    store = Store(tmp_path / "street.sqlite3")

    allowed = ingest_poi_media_evidence(
        store,
        event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            idempotency_key="knowledge-media:allowed",
            visibility="public",
            rights_status="public_domain_verified",
        ),
    )
    ingest_poi_media_evidence(
        store,
        event(
            event_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            idempotency_key="knowledge-media:restricted",
            visibility="public",
            rights_status="restricted",
        ),
    )

    rows = poi_media_for_actor(
        store,
        allowed["poi_id"],
        publishable_only=True,
    )
    assert len(rows) == 1
    assert rows[0]["rights_status"] == "public_domain_verified"


def test_name_only_unknown_media_stays_unresolved(tmp_path):
    store = Store(tmp_path / "street.sqlite3")
    result = ingest_poi_media_evidence(
        store,
        event(
            external_ids={},
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
    )
    assert result["state"] == "unresolved_identity"
    assert result["poi_id"] is None
