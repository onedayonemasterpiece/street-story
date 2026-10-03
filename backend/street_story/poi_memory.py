from __future__ import annotations

import json
from typing import Any

from .model_facts import normalized_claim_key


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




def _research_memory_facts(db, identity: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    rows = db.execute(
        "SELECT fact_id,claim_key,text,confidence,sources_json,updated_at "
        "FROM poi_research_facts WHERE poi_key=? ORDER BY updated_at DESC LIMIT ?",
        (key, max(1, int(limit))),
    )
    return [
        {
            "fact_id": str(row["fact_id"]),
            "claim_key": str(row["claim_key"]),
            "text": str(row["text"]),
            "confidence": float(row["confidence"]),
            "evidence_supported": True,
            "selected": False,
            "sources": json.loads(row["sources_json"] or "[]"),
            "origin": "poi_research",
        }
        for row in rows
    ]


def processed_sources(db, identity: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    rows = db.execute(
        "SELECT url,title,last_query,supports_json,last_seen_at "
        "FROM poi_research_sources WHERE poi_key=? ORDER BY last_seen_at DESC LIMIT ?",
        (key, max(1, min(int(limit), 200))),
    )
    return [
        {
            "url": str(row["url"]),
            "title": str(row["title"]),
            "last_query": str(row["last_query"]),
            "supports": json.loads(row["supports_json"] or "[]"),
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def persist_research_memory(
    db,
    identity: dict[str, Any],
    facts: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    query: str,
    now: float,
) -> None:
    key = poi_key(identity)
    if not key:
        return
    source_by_url: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").rstrip("/")
        if not url.startswith("https://"):
            continue
        source_by_url[url] = source
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        for source in fact.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            if url.startswith("https://"):
                source_by_url[url] = source

    for url, source in source_by_url.items():
        supports = [
            item for item in (source.get("supports") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ][:6]
        db.execute(
            "INSERT INTO poi_research_sources(poi_key,url,title,supports_json,last_query,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(poi_key,url) DO UPDATE SET "
            "title=excluded.title,supports_json=CASE WHEN excluded.supports_json<>'[]' THEN excluded.supports_json ELSE poi_research_sources.supports_json END,"
            "last_query=excluded.last_query,last_seen_at=excluded.last_seen_at",
            (
                key,
                url,
                str(source.get("title") or url)[:300],
                json.dumps(supports, ensure_ascii=False, separators=(",", ":")),
                str(query or "")[:1000],
                now,
                now,
            ),
        )

    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("evidence_supported", bool(fact.get("sources"))):
            continue
        claim_key = normalized_claim_key(fact.get("claim_key"))
        fact_id = str(fact.get("fact_id") or "").strip()
        text = str(fact.get("text") or "").strip()
        if not claim_key or not fact_id or not text:
            continue
        current = db.execute(
            "SELECT sources_json,created_at FROM poi_research_facts WHERE poi_key=? AND claim_key=?",
            (key, claim_key),
        ).fetchone()
        merged: dict[str, dict[str, Any]] = {}
        if current:
            for source in json.loads(current["sources_json"] or "[]"):
                if isinstance(source, dict) and str(source.get("url") or "").startswith("https://"):
                    merged[str(source["url"]).rstrip("/")] = source
        for source in fact.get("sources") or []:
            if isinstance(source, dict) and str(source.get("url") or "").startswith("https://"):
                merged[str(source["url"]).rstrip("/")] = source
        if not merged:
            continue
        created_at = float(current["created_at"]) if current else now
        try:
            confidence = max(0.0, min(1.0, float(fact.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        db.execute(
            "INSERT INTO poi_research_facts(poi_key,claim_key,fact_id,text,confidence,sources_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(poi_key,claim_key) DO UPDATE SET "
            "fact_id=excluded.fact_id,text=excluded.text,confidence=MAX(poi_research_facts.confidence,excluded.confidence),"
            "sources_json=excluded.sources_json,updated_at=excluded.updated_at",
            (
                key,
                claim_key,
                fact_id,
                text[:500],
                confidence,
                json.dumps(list(merged.values()), ensure_ascii=False, separators=(",", ":")),
                created_at,
                now,
            ),
        )

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

    for item in _research_memory_facts(db, identity, limit):
        if item["fact_id"] in seen:
            continue
        seen.add(item["fact_id"])
        result.append(item)
        if len(result) >= limit:
            return result

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
