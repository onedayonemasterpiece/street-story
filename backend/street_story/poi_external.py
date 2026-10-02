"""Idempotent external POI evidence intake.

This module is deliberately transport/auth agnostic. Network routes must authenticate
the caller and authorize the event scope before calling ingest_poi_evidence().
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .poi_semantic import queue_poi_semantic_candidates


_VISIBILITIES = {"private", "workspace", "public"}
_KINDS = {
    "construction", "architect", "foundation", "reconstruction", "demolition",
    "ownership", "visit", "use", "opening", "location", "structure", "other",
}
_SPACE = re.compile(r"\s+")


class PoiEvidenceError(ValueError):
    pass


class PoiEvidenceConflict(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise PoiEvidenceError(f"{field}_invalid") from None


def _text(value: Any, field: str, maximum: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = _SPACE.sub(" ", str(value or "")).strip()
    if required and not text:
        raise PoiEvidenceError(f"{field}_required")
    if len(text) > maximum:
        raise PoiEvidenceError(f"{field}_too_long")
    return text or None


def _score(value: Any, field: str, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        raise PoiEvidenceError(f"{field}_invalid") from None
    if not 0 <= score <= 100:
        raise PoiEvidenceError(f"{field}_invalid")
    return score


def _normalized_alias(value: str) -> str:
    return _SPACE.sub(" ", value).strip().casefold()


def _exact_keys(value: Any, allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise PoiEvidenceError(f"{field}_invalid")
    return value


def normalize_poi_evidence(event: dict[str, Any]) -> dict[str, Any]:
    top = _exact_keys(
        event,
        {
            "contract_version", "event_id", "idempotency_key", "producer",
            "scope", "source", "poi_locator", "claim", "evidence",
        },
        "event",
    )
    if top.get("contract_version") != "poi.fact_evidence.v1":
        raise PoiEvidenceError("contract_version_invalid")
    if top.get("producer") != "regional_knowledge":
        raise PoiEvidenceError("producer_invalid")

    event_id = _uuid(top.get("event_id"), "event_id")
    idempotency_key = _text(top.get("idempotency_key"), "idempotency_key", 300)

    scope = _exact_keys(
        top.get("scope"),
        {"visibility", "owner_sub", "workspace_id"},
        "scope",
    )
    visibility = str(scope.get("visibility") or "")
    if visibility not in _VISIBILITIES:
        raise PoiEvidenceError("visibility_invalid")
    owner_sub = _text(scope.get("owner_sub"), "owner_sub", 200, required=False)
    workspace_id = _text(
        scope.get("workspace_id"),
        "workspace_id",
        200,
        required=False,
    )
    if visibility in {"private", "workspace"} and not owner_sub:
        raise PoiEvidenceError("owner_sub_required")
    if visibility == "workspace" and not workspace_id:
        raise PoiEvidenceError("workspace_id_required")

    source = _exact_keys(
        top.get("source"),
        {"document_ref", "revision", "title", "publication_year"},
        "source",
    )
    document_ref = _text(source.get("document_ref"), "document_ref", 500)
    if not document_ref.startswith("knowledge://documents/"):
        raise PoiEvidenceError("document_ref_invalid")
    try:
        revision = int(source.get("revision"))
    except (TypeError, ValueError):
        raise PoiEvidenceError("source_revision_invalid") from None
    if revision < 1:
        raise PoiEvidenceError("source_revision_invalid")
    source_title = _text(source.get("title"), "source_title", 1000)
    publication_year = source.get("publication_year")
    if publication_year is not None:
        try:
            publication_year = int(publication_year)
        except (TypeError, ValueError):
            raise PoiEvidenceError("publication_year_invalid") from None
        if not 1 <= publication_year <= 3000:
            raise PoiEvidenceError("publication_year_invalid")

    locator = _exact_keys(
        top.get("poi_locator"),
        {"names", "external_ids", "latitude", "longitude"},
        "poi_locator",
    )
    raw_names = locator.get("names")
    if not isinstance(raw_names, list) or not 1 <= len(raw_names) <= 30:
        raise PoiEvidenceError("poi_names_invalid")
    names = []
    for raw in raw_names:
        name = _text(raw, "poi_name", 300)
        if name not in names:
            names.append(name)
    external_ids_raw = locator.get("external_ids") or {}
    if not isinstance(external_ids_raw, dict) or len(external_ids_raw) > 20:
        raise PoiEvidenceError("poi_external_ids_invalid")
    external_ids: dict[str, str] = {}
    for raw_namespace, raw_value in external_ids_raw.items():
        namespace = _text(raw_namespace, "poi_external_namespace", 80)
        value = _text(raw_value, "poi_external_value", 500)
        external_ids[namespace.casefold()] = value
    latitude = locator.get("latitude")
    longitude = locator.get("longitude")
    if latitude is not None:
        latitude = float(latitude)
        if not -90 <= latitude <= 90:
            raise PoiEvidenceError("latitude_invalid")
    if longitude is not None:
        longitude = float(longitude)
        if not -180 <= longitude <= 180:
            raise PoiEvidenceError("longitude_invalid")

    claim = _exact_keys(
        top.get("claim"),
        {"candidate_id", "semantic_key", "kind", "text", "time_scope"},
        "claim",
    )
    candidate_id = _uuid(claim.get("candidate_id"), "candidate_id")
    producer_kind = str(claim.get("kind") or "")
    if producer_kind not in _KINDS:
        raise PoiEvidenceError("claim_kind_invalid")
    source_text = _text(claim.get("text"), "claim_text", 500)
    semantic_key = _text(
        claim.get("semantic_key"),
        "semantic_key",
        300,
    )
    normalized_kind = producer_kind
    producer_semantic_key = semantic_key
    time_scope = _text(
        claim.get("time_scope"),
        "time_scope",
        100,
        required=False,
    )

    evidence = _exact_keys(
        top.get("evidence"),
        {
            "evidence_ref", "page_ids", "region_ids", "source_family_id",
            "author_profile_refs", "author_subject_authority",
            "publication_method_score", "provenance_precision_score",
            "evidence_verification_score", "score_policy_version",
        },
        "evidence",
    )
    evidence_ref = _text(evidence.get("evidence_ref"), "evidence_ref", 500)
    if not evidence_ref.startswith("knowledge://evidence/"):
        raise PoiEvidenceError("evidence_ref_invalid")
    page_ids = evidence.get("page_ids")
    region_ids = evidence.get("region_ids")
    if not isinstance(page_ids, list) or not page_ids:
        raise PoiEvidenceError("page_ids_required")
    if not isinstance(region_ids, list) or not region_ids:
        raise PoiEvidenceError("region_ids_required")
    page_ids = [_uuid(value, "page_id") for value in page_ids[:50]]
    region_ids = [_uuid(value, "region_id") for value in region_ids[:200]]
    source_family_id = _text(
        evidence.get("source_family_id"),
        "source_family_id",
        300,
    )
    if source_family_id.startswith("unresolved:"):
        source_family_id = "unknown"
    author_refs = evidence.get("author_profile_refs") or []
    if not isinstance(author_refs, list) or len(author_refs) > 20:
        raise PoiEvidenceError("author_profile_refs_invalid")
    author_refs = [
        _text(value, "author_profile_ref", 500)
        for value in author_refs
    ]
    if any(not value.startswith("knowledge://authors/") for value in author_refs):
        raise PoiEvidenceError("author_profile_ref_invalid")

    author_score = _score(
        evidence.get("author_subject_authority"),
        "author_subject_authority",
    )
    publication_score = _score(
        evidence.get("publication_method_score"),
        "publication_method_score",
    )
    provenance_score = _score(
        evidence.get("provenance_precision_score"),
        "provenance_precision_score",
        required=True,
    )
    verification_score = _score(
        evidence.get("evidence_verification_score"),
        "evidence_verification_score",
    )
    score_policy_version = _text(
        evidence.get("score_policy_version"),
        "score_policy_version",
        100,
    )

    normalized = {
        "contract_version": "poi.fact_evidence.v1",
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "producer": "regional_knowledge",
        "scope": {
            "visibility": visibility,
            "owner_sub": owner_sub,
            "workspace_id": workspace_id,
        },
        "source": {
            "document_ref": document_ref,
            "revision": revision,
            "title": source_title,
            "publication_year": publication_year,
        },
        "poi_locator": {
            "names": names,
            "external_ids": external_ids,
            "latitude": latitude,
            "longitude": longitude,
        },
        "claim": {
            "candidate_id": candidate_id,
            "producer_semantic_key": producer_semantic_key,
            "semantic_key": semantic_key,
            "producer_kind": producer_kind,
            "kind": normalized_kind,
            "text": source_text,
            "time_scope": time_scope,
        },
        "evidence": {
            "evidence_ref": evidence_ref,
            "page_ids": page_ids,
            "region_ids": region_ids,
            "source_family_id": source_family_id,
            "author_profile_refs": author_refs,
            "author_subject_authority": author_score,
            "publication_method_score": publication_score,
            "provenance_precision_score": provenance_score,
            "evidence_verification_score": verification_score,
            "score_policy_version": score_policy_version,
        },
    }
    return normalized


def _alias_rows(db, aliases: list[tuple[str, str]]) -> set[str]:
    result: set[str] = set()
    for namespace, value in aliases:
        row = db.execute(
            "SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=?",
            (namespace, _normalized_alias(value)),
        ).fetchone()
        if row:
            result.add(str(row["poi_id"]))
    return result


def _safe_add_alias(db, poi_id: str, namespace: str, value: str, now: float) -> None:
    normalized = _normalized_alias(value)
    row = db.execute(
        "SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=?",
        (namespace, normalized),
    ).fetchone()
    if row and str(row["poi_id"]) != poi_id:
        return
    db.execute(
        "INSERT OR IGNORE INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) "
        "VALUES(?,?,?,?,?)",
        (poi_id, namespace, value, normalized, now),
    )


def _resolve_poi(db, locator: dict[str, Any], now: float) -> tuple[str | None, str]:
    external = [
        (str(namespace), str(value))
        for namespace, value in locator["external_ids"].items()
    ]
    matched = _alias_rows(db, external)
    if len(matched) > 1:
        return None, "unresolved_identity"
    if len(matched) == 1:
        poi_id = next(iter(matched))
    elif external:
        poi_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO pois(id,status,canonical_name,latitude,longitude,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                poi_id,
                "candidate",
                locator["names"][0],
                locator.get("latitude"),
                locator.get("longitude"),
                now,
                now,
            ),
        )
    else:
        name_aliases = [("name", name) for name in locator["names"]]
        matched = _alias_rows(db, name_aliases)
        if len(matched) != 1:
            return None, "unresolved_identity"
        poi_id = next(iter(matched))

    for namespace, value in external:
        _safe_add_alias(db, poi_id, namespace, value, now)
    for name in locator["names"]:
        _safe_add_alias(db, poi_id, "name", name, now)
    return poi_id, "attached"


def _claim_id(poi_id: str, semantic_key: str) -> str:
    raw = f"{poi_id}|{semantic_key}".encode("utf-8")
    return "poi_claim_" + hashlib.sha256(raw).hexdigest()[:24]


def _conflict_id(left: str, right: str) -> str:
    pair = "|".join(sorted((left, right)))
    return "poi_conflict_" + hashlib.sha256(pair.encode("utf-8")).hexdigest()[:24]


def _visible_claim_items(db, poi_id: str, normalized: dict[str, Any]) -> list[dict[str, Any]]:
    scope = normalized["scope"]
    clauses = ["e.visibility='public'"]
    args: list[Any] = [poi_id]
    if scope.get("owner_sub"):
        clauses.append("(e.visibility='private' AND e.owner_sub=?)")
        args.append(scope["owner_sub"])
    if scope.get("workspace_id"):
        clauses.append("(e.visibility='workspace' AND e.workspace_id=?)")
        args.append(scope["workspace_id"])
    where_scope = " OR ".join(clauses)
    rows = db.execute(
        "SELECT DISTINCT c.id,c.semantic_key,c.kind,c.text "
        "FROM poi_claims c JOIN poi_claim_evidence ce ON ce.claim_id=c.id "
        "JOIN poi_external_events e ON e.event_id=ce.event_id "
        f"WHERE c.poi_id=? AND ({where_scope})",
        tuple(args),
    )
    return [
        {
            "fact_id": str(row["id"]),
            "claim_key": str(row["semantic_key"]),
            "kind": str(row["kind"]),
            "text": str(row["text"]),
            "sources": [],
        }
        for row in rows
    ]


def ingest_poi_evidence(store, event: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_poi_evidence(event)
    payload_digest = _digest(normalized)
    now = store.now()

    with store.tx() as db:
        existing = db.execute(
            "SELECT * FROM poi_external_events WHERE event_id=? OR idempotency_key=?",
            (normalized["event_id"], normalized["idempotency_key"]),
        ).fetchone()
        if existing:
            if (
                str(existing["event_id"]) != normalized["event_id"]
                or str(existing["idempotency_key"]) != normalized["idempotency_key"]
                or str(existing["payload_digest"]) != payload_digest
            ):
                raise PoiEvidenceConflict("poi_evidence_idempotency_conflict")
            return {
                "event_id": str(existing["event_id"]),
                "state": str(existing["state"]),
                "poi_id": existing["poi_id"],
                "claim_id": existing["claim_id"],
                "replayed": True,
                "conflict_ids": [],
                "semantic_candidate_ids": [],
            }

        poi_id, state = _resolve_poi(db, normalized["poi_locator"], now)
        claim_id: str | None = None
        semantic_candidate_ids: list[str] = []

        if poi_id is not None:
            claim = normalized["claim"]
            existing_claim = db.execute(
                "SELECT id,kind,text FROM poi_claims "
                "WHERE poi_id=? AND semantic_key=?",
                (poi_id, claim["semantic_key"]),
            ).fetchone()
            if existing_claim is not None:
                if (
                    str(existing_claim["kind"]) != claim["kind"]
                    or str(existing_claim["text"]) != claim["text"]
                ):
                    raise PoiEvidenceConflict(
                        "semantic_key_claim_collision"
                    )
                claim_id = str(existing_claim["id"])
                db.execute(
                    "UPDATE poi_claims SET updated_at=? WHERE id=?",
                    (now, claim_id),
                )
            else:
                claim_id = _claim_id(poi_id, claim["semantic_key"])
                db.execute(
                    "INSERT INTO poi_claims("
                    "id,poi_id,semantic_key,kind,text,status,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'candidate',?,?)",
                    (
                        claim_id,
                        poi_id,
                        claim["semantic_key"],
                        claim["kind"],
                        claim["text"],
                        now,
                        now,
                    ),
                )

        db.execute(
            "INSERT INTO poi_external_events("
            "event_id,idempotency_key,producer,payload_digest,payload_json,"
            "visibility,owner_sub,workspace_id,source_ref,source_revision,"
            "candidate_id,poi_id,claim_id,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                normalized["event_id"],
                normalized["idempotency_key"],
                normalized["producer"],
                payload_digest,
                _canonical(normalized),
                normalized["scope"]["visibility"],
                normalized["scope"].get("owner_sub"),
                normalized["scope"].get("workspace_id"),
                normalized["source"]["document_ref"],
                normalized["source"]["revision"],
                normalized["claim"]["candidate_id"],
                poi_id,
                claim_id,
                state,
                now,
                now,
            ),
        )

        if claim_id is not None:
            evidence = normalized["evidence"]
            db.execute(
                "INSERT INTO poi_claim_evidence("
                "claim_id,event_id,evidence_ref,source_family_id,author_score,"
                "publication_score,provenance_score,verification_score,evidence_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    normalized["event_id"],
                    evidence["evidence_ref"],
                    evidence["source_family_id"],
                    evidence["author_subject_authority"],
                    evidence["publication_method_score"],
                    evidence["provenance_precision_score"],
                    evidence["evidence_verification_score"],
                    _canonical(evidence),
                    now,
                ),
            )

            items = _visible_claim_items(db, poi_id, normalized)
            semantic_candidate_ids = queue_poi_semantic_candidates(
                db,
                poi_id=poi_id,
                normalized_scope=normalized["scope"],
                items=items,
                focus_claim_id=claim_id,
                now=now,
            )

        return {
            "event_id": normalized["event_id"],
            "state": state,
            "poi_id": poi_id,
            "claim_id": claim_id,
            "replayed": False,
            "conflict_ids": [],
            "semantic_candidate_ids": sorted(set(semantic_candidate_ids)),
        }
