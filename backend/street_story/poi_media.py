"""POI-linked historical media intake and access filtering.

Transport/auth layers must authenticate the caller before invoking mutation
functions. Query helpers apply evidence scope again so a public POI does not
implicitly expose private media.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .poi_external import (
    PoiEvidenceConflict,
    PoiEvidenceError,
    _canonical,
    _digest,
    _exact_keys,
    _resolve_poi,
    _text,
    _uuid,
)


_VISIBILITIES = {"private", "workspace", "public"}
_RELATIONS = {"depicts", "illustrates", "map_of", "detail_of"}
_MEDIA_KINDS = {"photo", "map", "drawing", "diagram", "facsimile", "other"}
_RIGHTS = {
    "unknown",
    "restricted",
    "licensed",
    "permission_granted",
    "public_domain_candidate",
    "public_domain_verified",
    "statutory_access_verified",
}
_PUBLICATION_RIGHTS = {
    "licensed",
    "permission_granted",
    "public_domain_verified",
    "statutory_access_verified",
}
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _number(value: Any, field: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PoiEvidenceError(f"{field}_invalid") from None
    if not low <= number <= high:
        raise PoiEvidenceError(f"{field}_invalid")
    return number


def _normalize_locator(value: Any) -> dict[str, Any]:
    locator = _exact_keys(
        value,
        {"names", "external_ids", "latitude", "longitude"},
        "poi_locator",
    )
    raw_names = locator.get("names")
    if not isinstance(raw_names, list) or not 1 <= len(raw_names) <= 30:
        raise PoiEvidenceError("poi_names_invalid")
    names: list[str] = []
    for raw in raw_names:
        name = _text(raw, "poi_name", 300)
        if name not in names:
            names.append(name)

    external_raw = locator.get("external_ids") or {}
    if not isinstance(external_raw, dict) or len(external_raw) > 20:
        raise PoiEvidenceError("poi_external_ids_invalid")
    external_ids: dict[str, str] = {}
    for raw_namespace, raw_value in external_raw.items():
        namespace = _text(raw_namespace, "poi_external_namespace", 80)
        value = _text(raw_value, "poi_external_value", 500)
        external_ids[namespace.casefold()] = value

    return {
        "names": names,
        "external_ids": external_ids,
        "latitude": _number(locator.get("latitude"), "latitude", -90, 90),
        "longitude": _number(locator.get("longitude"), "longitude", -180, 180),
    }


def normalize_poi_media_evidence(event: dict[str, Any]) -> dict[str, Any]:
    top = _exact_keys(
        event,
        {
            "contract_version",
            "event_id",
            "idempotency_key",
            "producer",
            "scope",
            "source",
            "poi_locator",
            "media",
            "evidence",
        },
        "event",
    )
    if top.get("contract_version") != "poi.media_evidence.v1":
        raise PoiEvidenceError("contract_version_invalid")
    if top.get("producer") != "regional_knowledge":
        raise PoiEvidenceError("producer_invalid")

    event_id = _uuid(top.get("event_id"), "event_id")
    idempotency_key = _text(
        top.get("idempotency_key"), "idempotency_key", 300
    )

    scope = _exact_keys(
        top.get("scope"),
        {"visibility", "owner_sub", "workspace_id"},
        "scope",
    )
    visibility = str(scope.get("visibility") or "")
    if visibility not in _VISIBILITIES:
        raise PoiEvidenceError("visibility_invalid")
    owner_sub = _text(
        scope.get("owner_sub"), "owner_sub", 200, required=False
    )
    workspace_id = _text(
        scope.get("workspace_id"), "workspace_id", 200, required=False
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

    locator = _normalize_locator(top.get("poi_locator"))

    media = _exact_keys(
        top.get("media"),
        {
            "link_id",
            "illustration_id",
            "illustration_ref",
            "relation",
            "time_scope",
            "kind",
            "caption",
            "page_id",
            "source_region_id",
            "caption_region_ids",
            "source_crop_sha256",
            "rights_status",
            "visibility",
            "vibepublish_entry_ref",
        },
        "media",
    )
    link_id = _uuid(media.get("link_id"), "link_id")
    illustration_id = _uuid(
        media.get("illustration_id"), "illustration_id"
    )
    illustration_ref = _text(
        media.get("illustration_ref"), "illustration_ref", 500
    )
    if illustration_ref != f"knowledge://illustrations/{illustration_id}":
        raise PoiEvidenceError("illustration_ref_invalid")
    relation = str(media.get("relation") or "")
    if relation not in _RELATIONS:
        raise PoiEvidenceError("media_relation_invalid")
    kind = str(media.get("kind") or "")
    if kind not in _MEDIA_KINDS:
        raise PoiEvidenceError("media_kind_invalid")
    time_scope = _text(
        media.get("time_scope"), "time_scope", 100, required=False
    )
    caption = _text(
        media.get("caption"), "caption", 2000, required=False
    )
    page_id = _uuid(media.get("page_id"), "page_id")
    source_region_id = _uuid(
        media.get("source_region_id"), "source_region_id"
    )
    raw_caption_ids = media.get("caption_region_ids") or []
    if not isinstance(raw_caption_ids, list) or len(raw_caption_ids) > 50:
        raise PoiEvidenceError("caption_region_ids_invalid")
    caption_region_ids = [
        _uuid(value, "caption_region_id") for value in raw_caption_ids
    ]
    crop_sha = str(media.get("source_crop_sha256") or "")
    if not _SHA256.fullmatch(crop_sha):
        raise PoiEvidenceError("source_crop_sha256_invalid")
    rights_status = str(media.get("rights_status") or "")
    if rights_status not in _RIGHTS:
        raise PoiEvidenceError("rights_status_invalid")
    media_visibility = str(media.get("visibility") or "")
    if media_visibility != visibility:
        raise PoiEvidenceError("media_visibility_mismatch")
    vibepublish_entry_ref = _text(
        media.get("vibepublish_entry_ref"),
        "vibepublish_entry_ref",
        1000,
        required=False,
    )

    evidence = _exact_keys(
        top.get("evidence"),
        {"page_ids", "region_ids", "source_family_id"},
        "evidence",
    )
    raw_page_ids = evidence.get("page_ids")
    raw_region_ids = evidence.get("region_ids")
    if not isinstance(raw_page_ids, list) or not raw_page_ids:
        raise PoiEvidenceError("page_ids_required")
    if not isinstance(raw_region_ids, list) or not raw_region_ids:
        raise PoiEvidenceError("region_ids_required")
    page_ids = [_uuid(value, "page_id") for value in raw_page_ids[:20]]
    region_ids = [
        _uuid(value, "region_id") for value in raw_region_ids[:100]
    ]
    if page_id not in page_ids:
        raise PoiEvidenceError("media_page_not_in_evidence")
    if source_region_id not in region_ids:
        raise PoiEvidenceError("media_region_not_in_evidence")
    source_family_id = _text(
        evidence.get("source_family_id"), "source_family_id", 300
    )
    if source_family_id.startswith("unresolved:"):
        source_family_id = "unknown"

    return {
        "contract_version": "poi.media_evidence.v1",
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
        "poi_locator": locator,
        "media": {
            "link_id": link_id,
            "illustration_id": illustration_id,
            "illustration_ref": illustration_ref,
            "relation": relation,
            "time_scope": time_scope,
            "kind": kind,
            "caption": caption,
            "page_id": page_id,
            "source_region_id": source_region_id,
            "caption_region_ids": caption_region_ids,
            "source_crop_sha256": crop_sha,
            "rights_status": rights_status,
            "visibility": media_visibility,
            "vibepublish_entry_ref": vibepublish_entry_ref,
        },
        "evidence": {
            "page_ids": page_ids,
            "region_ids": region_ids,
            "source_family_id": source_family_id,
        },
    }


def ingest_poi_media_evidence(store, event: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_poi_media_evidence(event)
    payload_digest = _digest(normalized)
    now = store.now()

    with store.tx() as db:
        existing = db.execute(
            "SELECT * FROM poi_media_events "
            "WHERE event_id=? OR idempotency_key=?",
            (
                normalized["event_id"],
                normalized["idempotency_key"],
            ),
        ).fetchone()
        if existing:
            if (
                str(existing["event_id"]) != normalized["event_id"]
                or str(existing["idempotency_key"])
                != normalized["idempotency_key"]
                or str(existing["payload_digest"]) != payload_digest
            ):
                raise PoiEvidenceConflict(
                    "poi_media_idempotency_conflict"
                )
            return {
                "event_id": str(existing["event_id"]),
                "state": str(existing["state"]),
                "poi_id": existing["poi_id"],
                "illustration_ref": str(existing["illustration_ref"]),
                "replayed": True,
            }

        poi_id, state = _resolve_poi(
            db, normalized["poi_locator"], now
        )
        media = normalized["media"]
        db.execute(
            "INSERT INTO poi_media_events("
            "event_id,idempotency_key,producer,payload_digest,payload_json,"
            "visibility,owner_sub,workspace_id,source_ref,source_revision,"
            "poi_id,illustration_ref,illustration_id,relation,time_scope,"
            "media_kind,caption,page_id,source_region_id,source_crop_sha256,"
            "rights_status,vibepublish_entry_ref,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                poi_id,
                media["illustration_ref"],
                media["illustration_id"],
                media["relation"],
                media["time_scope"],
                media["kind"],
                media["caption"],
                media["page_id"],
                media["source_region_id"],
                media["source_crop_sha256"],
                media["rights_status"],
                media["vibepublish_entry_ref"],
                state,
                now,
                now,
            ),
        )

        return {
            "event_id": normalized["event_id"],
            "state": state,
            "poi_id": poi_id,
            "illustration_ref": media["illustration_ref"],
            "replayed": False,
        }


def poi_media_for_actor(
    store,
    poi_id: str,
    *,
    owner_sub: str | None = None,
    workspace_ids: Iterable[str] = (),
    publishable_only: bool = False,
) -> list[dict[str, Any]]:
    workspace_ids = tuple(
        dict.fromkeys(str(value) for value in workspace_ids if value)
    )
    clauses: list[str] = ["visibility='public'"]
    args: list[Any] = [poi_id]
    if owner_sub:
        clauses.append("(visibility='private' AND owner_sub=?)")
        args.append(owner_sub)
    if workspace_ids:
        placeholders = ",".join("?" for _ in workspace_ids)
        clauses.append(
            f"(visibility='workspace' "
            f"AND workspace_id IN ({placeholders}))"
        )
        args.extend(workspace_ids)

    where = " OR ".join(clauses)
    sql = (
        "SELECT * FROM poi_media_events "
        f"WHERE poi_id=? AND state='attached' AND ({where})"
    )
    if publishable_only:
        placeholders = ",".join("?" for _ in _PUBLICATION_RIGHTS)
        sql += (
            " AND visibility='public' "
            f"AND rights_status IN ({placeholders})"
        )
        args.extend(sorted(_PUBLICATION_RIGHTS))
    sql += " ORDER BY updated_at DESC,event_id"

    with store.connection() as db:
        rows = list(db.execute(sql, tuple(args)))

    return [
        {
            "event_id": str(row["event_id"]),
            "poi_id": str(row["poi_id"]),
            "illustration_ref": str(row["illustration_ref"]),
            "relation": str(row["relation"]),
            "time_scope": row["time_scope"],
            "kind": str(row["media_kind"]),
            "caption": row["caption"],
            "source_ref": str(row["source_ref"]),
            "source_revision": int(row["source_revision"]),
            "source_crop_sha256": str(row["source_crop_sha256"]),
            "rights_status": str(row["rights_status"]),
            "visibility": str(row["visibility"]),
            "vibepublish_entry_ref": row["vibepublish_entry_ref"],
        }
        for row in rows
    ]
