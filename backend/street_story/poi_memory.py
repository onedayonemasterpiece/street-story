from __future__ import annotations

import json
from typing import Any


def poi_key(identity: dict[str, Any]) -> str | None:
    value = str(identity.get("candidate_id") or "").strip()
    return value or None


def _identity_alias_values(identity: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in (
        identity.get("candidate_id"),
        identity.get("candidate_name"),
        identity.get("wikidata"),
        identity.get("wikipedia_url"),
        identity.get("osm_id"),
    ):
        value = str(raw or "").strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    candidate_id = str(identity.get("candidate_id") or "")
    for candidate in identity.get("candidates") or []:
        if not isinstance(candidate, dict) or str(candidate.get("candidate_id") or "") != candidate_id:
            continue
        for raw in (
            candidate.get("candidate_id"),
            candidate.get("name"),
            candidate.get("wikidata"),
            candidate.get("wikipedia_url"),
            candidate.get("osm_id"),
        ):
            value = str(raw or "").strip()
            if value and value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
    return values


def _public_regional_knowledge_facts(db, identity: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    aliases = _identity_alias_values(identity)
    if not aliases:
        return []
    normalized = [" ".join(value.split()).casefold() for value in aliases]
    placeholders = ",".join("?" for _ in normalized)
    poi_rows = list(db.execute(
        f"SELECT DISTINCT poi_id FROM poi_aliases WHERE normalized_value IN ({placeholders})",
        tuple(normalized),
    ))
    if len(poi_rows) != 1:
        return []
    poi_id = str(poi_rows[0]["poi_id"])
    rows = db.execute(
        """
        SELECT c.id,c.semantic_key,c.kind,c.text,c.status,
               e.evidence_ref,e.source_family_id,e.author_score,e.publication_score,
               e.provenance_score,e.verification_score,e.evidence_json,
               x.source_ref,x.payload_json,x.updated_at
        FROM poi_claims c
        JOIN poi_claim_evidence e ON e.claim_id=c.id
        JOIN poi_external_events x ON x.event_id=e.event_id
        WHERE c.poi_id=? AND x.visibility='public'
          AND c.status IN ('candidate','accepted','contested')
        ORDER BY x.updated_at DESC
        LIMIT ?
        """,
        (poi_id, max(1, int(limit))),
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fact_id = str(row["id"])
        if fact_id in seen:
            continue
        seen.add(fact_id)
        try:
            evidence_payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence_payload = {}
        result.append({
            "fact_id": fact_id,
            "claim_key": str(row["semantic_key"] or ""),
            "text": str(row["text"]),
            "confidence": (float(row["verification_score"]) / 100.0) if row["verification_score"] is not None else 0.0,
            "evidence_supported": True,
            "selected": False,
            "sources": [{
                "type": "regional_knowledge",
                "title": str(row["source_ref"]),
                "ref": str(row["evidence_ref"]),
                "source_family_id": str(row["source_family_id"]),
                "author_subject_authority": row["author_score"],
                "publication_method_score": row["publication_score"],
                "provenance_precision_score": row["provenance_score"],
                "evidence_verification_score": row["verification_score"],
                "evidence": evidence_payload,
            }],
            "poi_id": poi_id,
            "poi_claim_status": str(row["status"]),
        })
        if len(result) >= limit:
            break
    return result


def prior_facts(db, identity: dict[str, Any], story_id: str, limit: int = 60) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    rows = db.execute(
        "SELECT f.fact_id,f.text,f.confidence,f.evidence_supported,f.selected,f.sources_json,s.updated_at "
        "FROM facts f JOIN stories s ON s.id=f.story_id "
        "WHERE s.id<>? AND json_extract(s.research_json,'$.visual_identity.candidate_id')=? "
        "ORDER BY s.updated_at DESC,f.rowid LIMIT ?",
        (story_id, key, limit),
    )
    for row in rows:
        fact_id = str(row["fact_id"])
        if fact_id in seen:
            continue
        seen.add(fact_id)
        result.append({
            "fact_id": fact_id,
            "claim_key": "",
            "text": str(row["text"]),
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "sources": json.loads(row["sources_json"] or "[]"),
            "origin": "street_story",
        })
        if len(result) >= limit:
            return result

    for item in _public_regional_knowledge_facts(db, identity, limit - len(result)):
        if item["fact_id"] in seen:
            continue
        seen.add(item["fact_id"])
        result.append({**item, "origin": "regional_knowledge"})
        if len(result) >= limit:
            break
    return result
