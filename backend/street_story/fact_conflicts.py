"""Durable model-owned contradiction ledger for POI facts.

The host does not infer semantic kinds or preselect "likely" conflict pairs. A model
receives the bounded fact inventory and decides which semantic keys conflict. Host
code only validates references, stores evidence and records telemetry.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .fact_quality import FactCurationError, compact_text, semantic_key
from .gemini import GeminiUnavailable

RELATIONS = {
    "contradiction",
    "scope_difference",
    "temporal_sequence",
    "source_disagreement",
    "uncertain",
}
RESOLUTIONS = {"prefer_left", "prefer_right", "both_valid", "unresolved"}


def _source_summary(item: dict[str, Any]) -> dict[str, Any]:
    sources = [
        source for source in (item.get("sources") or [])
        if isinstance(source, dict) and str(source.get("url") or "").startswith("https://")
    ]
    urls: list[str] = []
    domains: list[str] = []
    official_urls: list[str] = []
    supports: list[dict[str, str]] = []
    for source in sources:
        url = str(source.get("url") or "").rstrip("/")
        if url and url not in urls:
            urls.append(url)
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host and host not in domains:
            domains.append(host)
        if str(source.get("type") or "") == "official" and url not in official_urls:
            official_urls.append(url)
        for support in source.get("supports") or []:
            if not isinstance(support, dict):
                continue
            support_text = str(support.get("text") or "").strip()
            if support_text:
                supports.append({"url": url[:500], "text": support_text[:500]})
                if len(supports) >= 4:
                    break
        if len(supports) >= 4:
            break
    return {
        "source_count": len(urls),
        "domain_count": len(domains),
        "official": bool(official_urls),
        "official_urls": official_urls[:3],
        "source_urls": urls[:8],
        "supports": supports,
    }


def fact_snapshot(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        key = semantic_key(item.get("semantic_key"))
        text = compact_text(item.get("text"), 260)
    except FactCurationError:
        return None
    return {
        "fact_id": str(item.get("fact_id") or "")[:160],
        "semantic_key": key,
        "text": text,
        "evidence": _source_summary(item),
    }


def compact_conflict_inventory(items: list[dict[str, Any]], limit: int = 48) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        snapshot = fact_snapshot(item)
        if snapshot is None or snapshot["semantic_key"] in seen:
            continue
        seen.add(snapshot["semantic_key"])
        result.append(snapshot)
        if len(result) >= limit:
            break
    return result


def _conflict_id(left_key: str, right_key: str) -> str:
    keys = sorted((semantic_key(left_key), semantic_key(right_key)))
    return "conflict_" + hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:20]


def normalize_conflict_records(
    facts: list[dict[str, Any]],
    payload: dict[str, Any] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    by_key = {
        snapshot["semantic_key"]: snapshot
        for item in facts
        if (snapshot := fact_snapshot(item)) is not None
    }
    raw = payload.get("conflicts") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw[:24]:
        if not isinstance(item, dict):
            continue
        try:
            left_key = semantic_key(item.get("left_key"))
            right_key = semantic_key(item.get("right_key"))
        except FactCurationError:
            continue
        left = by_key.get(left_key)
        right = by_key.get(right_key)
        relation = str(item.get("relation") or "")
        resolution = str(item.get("suggested_resolution") or "")
        if (
            left is None
            or right is None
            or left_key == right_key
            or relation not in RELATIONS
            or resolution not in RESOLUTIONS
        ):
            continue
        pair = tuple(sorted((left_key, right_key)))
        if pair in seen:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= confidence <= 1.0:
            continue
        suggested_fact_id = None
        if resolution == "prefer_left":
            suggested_fact_id = left["fact_id"]
        elif resolution == "prefer_right":
            suggested_fact_id = right["fact_id"]
        records.append({
            "conflict_id": _conflict_id(left_key, right_key),
            "left_fact_id": left["fact_id"],
            "right_fact_id": right["fact_id"],
            "left_key": left_key,
            "right_key": right_key,
            "left_text": left["text"],
            "right_text": right["text"],
            "relation": relation,
            "detector_confidence": confidence,
            "suggested_resolution": resolution,
            "suggested_fact_id": suggested_fact_id,
            "detector_rationale": str(item.get("rationale") or "")[:1000],
            "needs_more_search": bool(item.get("needs_more_search")),
            "search_query": str(item.get("search_query") or "")[:500],
            "evidence": {
                "left": left["evidence"],
                "right": right["evidence"],
            },
        })
        seen.add(pair)
    return records


def _record_scan(
    service,
    story_id: str,
    poi_key: str | None,
    *,
    detector: str,
    status: str,
    fact_count: int,
    detected_count: int,
    error_type: str | None = None,
) -> None:
    now = service.store.now()
    with service.store.tx() as db:
        if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
            return
        db.execute(
            """
            INSERT INTO fact_semantic_scans(
              story_id,poi_key,detector,status,fact_count,detected_count,error_type,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                story_id,
                poi_key,
                detector[:120],
                status[:40],
                max(0, int(fact_count)),
                max(0, int(detected_count)),
                str(error_type or "")[:120] or None,
                now,
            ),
        )
    from .identity_telemetry import record_identity_event
    record_identity_event(
        service,
        story_id,
        "fact_semantic_scan",
        {
            "status": status[:40],
            "fact_count": max(0, int(fact_count)),
            "detected_count": max(0, int(detected_count)),
            "error_type": str(error_type or "")[:120] or None,
            "detector": detector[:120],
        },
        source="fact_conflict",
    )


def conflict_stats(db, story_id: str, poi_key: str | None = None) -> dict[str, Any]:
    relation_rows = db.execute(
        "SELECT relation,COUNT(*) AS count FROM fact_conflicts "
        "WHERE story_id=? GROUP BY relation",
        (story_id,),
    )
    scan = db.execute(
        "SELECT COUNT(*) AS scans,COALESCE(SUM(fact_count),0) AS facts,"
        "COALESCE(SUM(detected_count),0) AS detected,"
        "COALESCE(SUM(CASE WHEN status='ok' OR status='no_facts' THEN 0 ELSE 1 END),0) AS failures "
        "FROM fact_semantic_scans WHERE story_id=?",
        (story_id,),
    ).fetchone()
    result: dict[str, Any] = {
        "scan_count": int(scan["scans"]),
        "facts_reviewed": int(scan["facts"]),
        "detected_observations": int(scan["detected"]),
        "scan_failures": int(scan["failures"]),
        "total_detected": db.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE story_id=?", (story_id,)
        ).fetchone()[0],
        "open": db.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE story_id=? "
            "AND (final_resolution IS NULL OR final_resolution='unresolved')",
            (story_id,),
        ).fetchone()[0],
        "arbitrated": db.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE story_id=? "
            "AND final_resolution IS NOT NULL AND final_resolution<>'unresolved'",
            (story_id,),
        ).fetchone()[0],
        "by_relation": {str(row["relation"]): int(row["count"]) for row in relation_rows},
    }
    if poi_key:
        poi_scan = db.execute(
            "SELECT COUNT(*) AS scans,COALESCE(SUM(fact_count),0) AS facts,"
            "COALESCE(SUM(detected_count),0) AS detected "
            "FROM fact_semantic_scans WHERE poi_key=?",
            (poi_key,),
        ).fetchone()
        result.update({
            "poi_scan_count": int(poi_scan["scans"]),
            "poi_facts_reviewed": int(poi_scan["facts"]),
            "poi_detected_observations": int(poi_scan["detected"]),
            "poi_total_detected": db.execute(
                "SELECT COUNT(DISTINCT conflict_id) FROM fact_conflicts WHERE poi_key=?",
                (poi_key,),
            ).fetchone()[0],
            "poi_open": db.execute(
                "SELECT COUNT(DISTINCT conflict_id) FROM fact_conflicts WHERE poi_key=? "
                "AND (final_resolution IS NULL OR final_resolution='unresolved')",
                (poi_key,),
            ).fetchone()[0],
        })
    return result


def persist_fact_conflicts(
    service,
    story_id: str,
    poi_key: str | None,
    records: list[dict[str, Any]],
    *,
    detector: str,
) -> list[dict[str, Any]]:
    now = service.store.now()
    with service.store.tx() as db:
        row = db.execute("SELECT research_json FROM stories WHERE id=?", (story_id,)).fetchone()
        if row is None:
            return []
        for record in records:
            db.execute(
                """
                INSERT INTO fact_conflicts(
                  story_id,conflict_id,poi_key,left_fact_id,right_fact_id,left_text,right_text,
                  relation,detector_confidence,suggested_resolution,suggested_fact_id,
                  detector_rationale,final_resolution,final_fact_id,arbitration_reason,
                  arbitration_confidence,arbitrated_by,evidence_json,times_seen,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,1,?,?)
                ON CONFLICT(story_id,conflict_id) DO UPDATE SET
                  poi_key=excluded.poi_key,
                  left_fact_id=excluded.left_fact_id,
                  right_fact_id=excluded.right_fact_id,
                  left_text=excluded.left_text,
                  right_text=excluded.right_text,
                  relation=excluded.relation,
                  detector_confidence=excluded.detector_confidence,
                  suggested_resolution=excluded.suggested_resolution,
                  suggested_fact_id=excluded.suggested_fact_id,
                  detector_rationale=excluded.detector_rationale,
                  evidence_json=excluded.evidence_json,
                  times_seen=fact_conflicts.times_seen+1,
                  last_seen_at=excluded.last_seen_at
                """,
                (
                    story_id,
                    record["conflict_id"],
                    poi_key,
                    record["left_fact_id"],
                    record["right_fact_id"],
                    record["left_text"],
                    record["right_text"],
                    record["relation"],
                    record["detector_confidence"],
                    record["suggested_resolution"],
                    record["suggested_fact_id"],
                    record["detector_rationale"],
                    json.dumps(record["evidence"], ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        durable = conflict_rows(db, story_id, limit=40)
        research = json.loads(row["research_json"] or "{}")
        research["fact_conflicts"] = durable
        research["fact_conflict_stats"] = {
            **conflict_stats(db, story_id, poi_key),
            "detector": detector[:120],
        }
        db.execute(
            "UPDATE stories SET research_json=? WHERE id=?",
            (json.dumps(research, ensure_ascii=False, separators=(",", ":")), story_id),
        )
    from .identity_telemetry import record_identity_event
    for record in records:
        evidence = record["evidence"]
        record_identity_event(
            service,
            story_id,
            "fact_conflict_detected",
            {
                "conflict_id": record["conflict_id"],
                "relation": record["relation"],
                "confidence": record["detector_confidence"],
                "suggested_resolution": record["suggested_resolution"],
                "left_sources": evidence["left"]["source_count"],
                "right_sources": evidence["right"]["source_count"],
                "left_domains": evidence["left"]["domain_count"],
                "right_domains": evidence["right"]["domain_count"],
                "left_official": evidence["left"]["official"],
                "right_official": evidence["right"]["official"],
                "needs_more_search": record.get("needs_more_search", False),
                "detector": detector[:120],
            },
            source="fact_conflict",
        )
    return durable


def persist_curation_conflicts(
    service,
    story_id: str,
    poi_key: str | None,
    facts: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    *,
    detector: str,
) -> list[dict[str, Any]]:
    compact = compact_conflict_inventory(facts)
    records = normalize_conflict_records(compact, conflicts)
    _record_scan(
        service,
        story_id,
        poi_key,
        detector=detector,
        status="ok" if compact else "no_facts",
        fact_count=len(compact),
        detected_count=len(records),
    )
    return persist_fact_conflicts(
        service, story_id, poi_key, records, detector=detector
    ) if records else []


async def analyze_fact_conflicts(
    service,
    story_id: str,
    poi_key: str | None,
    items: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    detector: str = "gemini_research",
) -> list[dict[str, Any]]:
    facts = compact_conflict_inventory(items)
    if len(facts) < 2:
        _record_scan(
            service, story_id, poi_key,
            detector=detector, status="no_facts",
            fact_count=len(facts), detected_count=0,
        )
        return []
    detector_fn = getattr(service.providers.gemini, "detect_fact_conflicts", None)
    if not callable(detector_fn):
        _record_scan(
            service, story_id, poi_key,
            detector=detector, status="detector_missing",
            fact_count=len(facts), detected_count=0,
        )
        return []
    try:
        records = await detector_fn(facts, context or {})
    except (GeminiUnavailable, MalformedProviderResponse, PermanentProviderError, RetryableProviderError) as exc:
        _record_scan(
            service, story_id, poi_key,
            detector=detector, status="detector_unavailable",
            fact_count=len(facts), detected_count=0,
            error_type=type(exc).__name__,
        )
        return []
    _record_scan(
        service, story_id, poi_key,
        detector=detector, status="ok",
        fact_count=len(facts), detected_count=len(records),
    )
    return persist_fact_conflicts(
        service, story_id, poi_key, records, detector=detector
    ) if records else []


def resolve_fact_conflict(
    service,
    story_id: str,
    conflict_id: str,
    resolution: str,
    reason: str,
    confidence: float,
    *,
    arbitrated_by: str = "mira_live",
) -> dict[str, Any]:
    conflict_id = str(conflict_id or "").strip()
    resolution = str(resolution or "").strip()
    reason = str(reason or "").strip()
    if resolution not in RESOLUTIONS:
        raise ValueError("invalid_fact_conflict_resolution")
    if not reason or len(reason) > 1200:
        raise ValueError("fact_conflict_reason_required")
    confidence = max(0.0, min(1.0, float(confidence)))
    with service.store.tx() as db:
        row = db.execute(
            "SELECT * FROM fact_conflicts WHERE story_id=? AND conflict_id=?",
            (story_id, conflict_id),
        ).fetchone()
        if row is None:
            raise KeyError("fact_conflict_not_found")
        final_fact_id = None
        if resolution == "prefer_left":
            final_fact_id = row["left_fact_id"]
        elif resolution == "prefer_right":
            final_fact_id = row["right_fact_id"]
        same = (
            row["final_resolution"] == resolution
            and row["final_fact_id"] == final_fact_id
            and row["arbitration_reason"] == reason
            and row["arbitrated_by"] == arbitrated_by[:120]
            and row["arbitration_confidence"] is not None
            and abs(float(row["arbitration_confidence"]) - confidence) < 1e-9
        )
        if not same:
            db.execute(
                """
                UPDATE fact_conflicts SET
                  final_resolution=?,final_fact_id=?,arbitration_reason=?,
                  arbitration_confidence=?,arbitrated_by=?,last_seen_at=?
                WHERE story_id=? AND conflict_id=?
                """,
                (
                    resolution,
                    final_fact_id,
                    reason,
                    confidence,
                    arbitrated_by[:120],
                    service.store.now(),
                    story_id,
                    conflict_id,
                ),
            )
        research_row = db.execute(
            "SELECT research_json FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        durable = conflict_rows(db, story_id, limit=40)
        current = next(item for item in durable if item["conflict_id"] == conflict_id)
        research = json.loads(research_row["research_json"] or "{}")
        research["fact_conflicts"] = durable
        research["fact_conflict_stats"] = conflict_stats(db, story_id, row["poi_key"])
        db.execute(
            "UPDATE stories SET research_json=? WHERE id=?",
            (json.dumps(research, ensure_ascii=False, separators=(",", ":")), story_id),
        )
    if not same:
        from .identity_telemetry import record_identity_event
        record_identity_event(
            service,
            story_id,
            "fact_conflict_arbitrated",
            {
                "conflict_id": conflict_id,
                "resolution": resolution,
                "confidence": confidence,
                "final_fact_id": final_fact_id,
                "arbitrated_by": arbitrated_by[:120],
            },
            source="fact_conflict",
        )
    return current


def conflict_rows(db, story_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = list(db.execute(
        """
        SELECT conflict_id,left_fact_id,right_fact_id,left_text,right_text,relation,
               detector_confidence,suggested_resolution,suggested_fact_id,
               detector_rationale,final_resolution,final_fact_id,arbitration_reason,
               arbitration_confidence,arbitrated_by,evidence_json,times_seen,
               first_seen_at,last_seen_at
        FROM fact_conflicts WHERE story_id=? ORDER BY last_seen_at DESC LIMIT ?
        """,
        (story_id, limit),
    ))
    return [
        {
            **{key: row[key] for key in row.keys() if key != "evidence_json"},
            "evidence": json.loads(row["evidence_json"] or "{}"),
        }
        for row in rows
    ]
