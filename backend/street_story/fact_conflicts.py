"""Conflict detection ledger for evidence-backed POI facts.

Conflict semantics are LLM-first. Deterministic code only bounds model input, validates
model references/schema, persists the ledger and emits telemetry. A conflict record is
evidence for later arbitration, not a truth vote.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse
from typing import Any

from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .gemini import GeminiUnavailable
from .model_facts import model_fact_id, normalized_claim_key, validated_model_fact_text


RELATIONS = {
    "contradiction",
    "scope_difference",
    "temporal_sequence",
    "source_disagreement",
    "uncertain",
}
SUGGESTED_RESOLUTIONS = {
    "prefer_left",
    "prefer_right",
    "both_valid",
    "unresolved",
}
FINAL_RESOLUTIONS = SUGGESTED_RESOLUTIONS


def _source_summary(item: dict[str, Any]) -> dict[str, Any]:
    sources = [
        source for source in (item.get("sources") or []) if isinstance(source, dict)
    ]
    urls = list(dict.fromkeys(
        str(source.get("url") or "").rstrip("/")
        for source in sources
        if str(source.get("url") or "").startswith("https://")
    ))
    refs = list(dict.fromkeys(
        str(source.get("ref") or source.get("evidence_ref") or "").strip()
        for source in sources
        if str(source.get("ref") or source.get("evidence_ref") or "").strip()
    ))
    domains = list(dict.fromkeys(
        (urlparse(url).hostname or "").lower().removeprefix("www.") for url in urls
    ))
    official_urls = [
        str(source.get("url") or "").rstrip("/")
        for source in sources
        if str(source.get("type") or "") == "official"
        and str(source.get("url") or "").startswith("https://")
    ]
    supports: list[dict[str, str]] = []
    for source in sources:
        for support in source.get("supports") or []:
            if isinstance(support, dict) and str(support.get("text") or "").strip():
                supports.append({
                    "url": str(source.get("url") or "")[:500],
                    "text": str(support.get("text") or "")[:500],
                })
                if len(supports) >= 4:
                    break
        if len(supports) >= 4:
            break
    return {
        "source_count": len(urls) + len(refs),
        "domain_count": len([domain for domain in domains if domain]),
        "official": bool(official_urls),
        "official_urls": official_urls[:3],
        "source_urls": urls[:8],
        "evidence_refs": refs[:8],
        "supports": supports,
    }


def _fact_snapshot(item: dict[str, Any]) -> dict[str, Any] | None:
    text = validated_model_fact_text(item.get("text"))
    if text is None:
        return None
    claim_key = normalized_claim_key(item.get("claim_key"))
    fact_id = str(item.get("fact_id") or "").strip()
    if not fact_id:
        try:
            fact_id = model_fact_id(claim_key, text)
        except ValueError:
            return None
    return {
        "fact_id": fact_id,
        "claim_key": claim_key,
        "text": text,
        "evidence": _source_summary(item),
        "origin": str(item.get("origin") or "")[:80] or None,
        "poi_id": str(item.get("poi_id") or "")[:120] or None,
        "poi_claim_status": str(item.get("poi_claim_status") or "")[:40] or None,
    }


def _pair_id(left: dict[str, Any], right: dict[str, Any]) -> str:
    identities = sorted([left["fact_id"], right["fact_id"]])
    return "conflict_" + hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()[:20]


def conflict_scan_items(
    items: list[dict[str, Any]],
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize/deduplicate model input without semantic filtering.

    max_items is only for explicit compatibility helpers. The main detector
    receives the complete inventory and performs bounded model batching itself.
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        snapshot = _fact_snapshot(item)
        if snapshot is None or snapshot["fact_id"] in seen:
            continue
        seen.add(snapshot["fact_id"])
        result.append(snapshot)
        if max_items is not None and len(result) >= max_items:
            break
    return result


def conflict_candidate_pairs(items: list[dict[str, Any]], max_pairs: int = 24) -> list[dict[str, Any]]:
    """Compatibility helper: mechanically enumerate pairs, never rank plausibility."""
    facts = conflict_scan_items(items, max_items=max(2, max_pairs + 1))
    result: list[dict[str, Any]] = []
    for index, left in enumerate(facts):
        for right in facts[index + 1:]:
            result.append({"pair_id": _pair_id(left, right), "left": left, "right": right})
            if len(result) >= max_pairs:
                return result
    return result


def normalize_model_conflict_records(
    items: list[dict[str, Any]],
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Validate model-selected conflicting claim references without deciding semantics."""
    by_id = {item["fact_id"]: item for item in items}
    raw = payload.get("conflicts") if isinstance(payload, dict) else []
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        left = by_id.get(str(item.get("left_fact_id") or ""))
        right = by_id.get(str(item.get("right_fact_id") or ""))
        relation = str(item.get("relation") or "")
        suggested = str(item.get("suggested_resolution") or "")
        if left is None or right is None or left["fact_id"] == right["fact_id"]:
            continue
        if relation not in RELATIONS or suggested not in SUGGESTED_RESOLUTIONS:
            continue
        conflict_id = _pair_id(left, right)
        if conflict_id in seen:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        preferred_fact_id = None
        if suggested == "prefer_left":
            preferred_fact_id = left["fact_id"]
        elif suggested == "prefer_right":
            preferred_fact_id = right["fact_id"]
        same_poi = left.get("poi_id") and left.get("poi_id") == right.get("poi_id")
        records.append({
            "conflict_id": conflict_id,
            "left_fact_id": left["fact_id"],
            "right_fact_id": right["fact_id"],
            "left_text": left["text"],
            "right_text": right["text"],
            "relation": relation,
            "detector_confidence": confidence,
            "suggested_resolution": suggested,
            "suggested_fact_id": preferred_fact_id,
            "detector_rationale": str(item.get("rationale") or "")[:1000],
            "evidence": {"left": left["evidence"], "right": right["evidence"]},
            "poi_id": left.get("poi_id") if same_poi else None,
        })
        seen.add(conflict_id)
    return records

def normalize_conflict_records(
    pairs: list[dict[str, Any]],
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    by_id = {pair["pair_id"]: pair for pair in pairs}
    raw = payload.get("conflicts") if isinstance(payload, dict) else []
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[: len(pairs)]:
        if not isinstance(item, dict):
            continue
        pair_id = str(item.get("pair_id") or "")
        pair = by_id.get(pair_id)
        relation = str(item.get("relation") or "")
        suggested = str(item.get("suggested_resolution") or "")
        if pair is None or relation not in RELATIONS or suggested not in SUGGESTED_RESOLUTIONS:
            continue
        if pair_id in seen:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        preferred_fact_id = None
        if suggested == "prefer_left":
            preferred_fact_id = pair["left"]["fact_id"]
        elif suggested == "prefer_right":
            preferred_fact_id = pair["right"]["fact_id"]
        records.append({
            "conflict_id": pair_id,
            "left_fact_id": pair["left"]["fact_id"],
            "right_fact_id": pair["right"]["fact_id"],
            "left_text": pair["left"]["text"],
            "right_text": pair["right"]["text"],
            "relation": relation,
            "detector_confidence": confidence,
            "suggested_resolution": suggested,
            "suggested_fact_id": preferred_fact_id,
            "detector_rationale": str(item.get("rationale") or "")[:1000],
            "evidence": {
                "left": pair["left"]["evidence"],
                "right": pair["right"]["evidence"],
            },
        })
        seen.add(pair_id)
    return records


def _record_conflict_scan(
    service,
    story_id: str,
    poi_key: str | None,
    *,
    detector: str,
    status: str,
    pair_count: int,
    detected_count: int,
    coverage_complete: bool = False,
    error_type: str | None = None,
) -> None:
    now = service.store.now()
    with service.store.tx() as db:
        if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
            return
        db.execute(
            """
            INSERT INTO fact_conflict_scans(
              story_id,poi_key,detector,status,pair_count,detected_count,
              coverage_complete,error_type,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                story_id,
                poi_key,
                detector[:120],
                status[:40],
                max(0, int(pair_count)),
                max(0, int(detected_count)),
                int(bool(coverage_complete)),
                str(error_type or "")[:120] or None,
                now,
            ),
        )
    from .identity_telemetry import record_identity_event
    record_identity_event(
        service,
        story_id,
        "fact_conflict_scan",
        {
            "status": status[:40],
            "pair_count": max(0, int(pair_count)),
            "detected_count": max(0, int(detected_count)),
            "coverage_complete": bool(coverage_complete),
            "error_type": str(error_type or "")[:120] or None,
            "detector": detector[:120],
        },
        source="fact_conflict",
    )


def conflict_stats(db, story_id: str, poi_key: str | None = None) -> dict[str, Any]:
    by_relation = {
        str(row["relation"]): int(row["count"])
        for row in db.execute(
            "SELECT relation,COUNT(*) AS count FROM fact_conflicts "
            "WHERE story_id=? GROUP BY relation",
            (story_id,),
        )
    }
    scan = db.execute(
        "SELECT COUNT(*) AS scans,COALESCE(SUM(pair_count),0) AS pairs,"
        "COALESCE(SUM(detected_count),0) AS detected,"
        "COALESCE(SUM(CASE WHEN status='ok' OR status='no_candidates' THEN 0 ELSE 1 END),0) AS failures "
        "FROM fact_conflict_scans WHERE story_id=?",
        (story_id,),
    ).fetchone()
    result: dict[str, Any] = {
        "scan_count": int(scan["scans"]),
        "pairs_checked": int(scan["pairs"]),
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
        "by_relation": by_relation,
    }
    if poi_key:
        poi_scan = db.execute(
            "SELECT COUNT(*) AS scans,COALESCE(SUM(pair_count),0) AS pairs,"
            "COALESCE(SUM(detected_count),0) AS detected "
            "FROM fact_conflict_scans WHERE poi_key=?",
            (poi_key,),
        ).fetchone()
        result["poi_scan_count"] = int(poi_scan["scans"])
        result["poi_pairs_checked"] = int(poi_scan["pairs"])
        result["poi_detected_observations"] = int(poi_scan["detected"])
        result["poi_total_detected"] = db.execute(
            "SELECT COUNT(DISTINCT conflict_id) FROM fact_conflicts WHERE poi_key=?", (poi_key,)
        ).fetchone()[0]
        result["poi_observations"] = db.execute(
            "SELECT COALESCE(SUM(times_seen),0) FROM fact_conflicts WHERE poi_key=?", (poi_key,)
        ).fetchone()[0]
        result["poi_open"] = db.execute(
            "SELECT COUNT(DISTINCT conflict_id) FROM fact_conflicts WHERE poi_key=? "
            "AND (final_resolution IS NULL OR final_resolution='unresolved')",
            (poi_key,),
        ).fetchone()[0]
    return result


def persist_fact_conflicts(
    service,
    story_id: str,
    poi_key: str | None,
    records: list[dict[str, Any]],
    *,
    detector: str,
) -> list[dict[str, Any]]:
    if not records:
        return []
    now = service.store.now()
    regional_conflict_ids: list[str] = []
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
        for record in records:
            regional_poi_id = str(record.get("poi_id") or "")
            if not regional_poi_id:
                continue
            left_claim_id, right_claim_id = sorted((record["left_fact_id"], record["right_fact_id"]))
            regional_conflict_id = "poi_conflict_" + hashlib.sha256(
                "|".join((left_claim_id, right_claim_id)).encode("utf-8")
            ).hexdigest()[:24]
            db.execute(
                """
                INSERT INTO poi_conflicts(
                  conflict_id,poi_id,left_claim_id,right_claim_id,relation,status,
                  times_seen,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,'open',1,?,?)
                ON CONFLICT(conflict_id) DO UPDATE SET
                  relation=excluded.relation,
                  status=CASE WHEN poi_conflicts.status='resolved' THEN 'resolved' ELSE 'open' END,
                  times_seen=poi_conflicts.times_seen+1,
                  last_seen_at=excluded.last_seen_at
                """,
                (
                    regional_conflict_id,
                    regional_poi_id,
                    left_claim_id,
                    right_claim_id,
                    record["relation"],
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE poi_claims SET status='contested',updated_at=? WHERE id IN (?,?)",
                (now, left_claim_id, right_claim_id),
            )
            regional_conflict_ids.append(regional_conflict_id)
        rows = list(db.execute(
            """
            SELECT conflict_id,left_fact_id,right_fact_id,left_text,right_text,relation,
                   detector_confidence,suggested_resolution,suggested_fact_id,
                   detector_rationale,final_resolution,final_fact_id,arbitration_reason,
                   arbitration_confidence,arbitrated_by,evidence_json,times_seen,first_seen_at,last_seen_at
            FROM fact_conflicts WHERE story_id=? ORDER BY last_seen_at DESC LIMIT 40
            """,
            (story_id,),
        ))
        durable = [
            {
                **{key: item[key] for key in item.keys() if key != "evidence_json"},
                "evidence": json.loads(item["evidence_json"] or "{}"),
            }
            for item in rows
        ]
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
    if regional_conflict_ids:
        from .poi_reviews import sync_review_cases
        sync_review_cases(service.store, regional_conflict_ids)

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
                "detector": detector[:120],
            },
            source="fact_conflict",
        )
    return durable


async def analyze_fact_conflicts(
    service,
    story_id: str,
    poi_key: str | None,
    items: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    detector: str = "gemini_research",
) -> list[dict[str, Any]]:
    model_items = conflict_scan_items(items)
    pair_count = len(model_items) * (len(model_items) - 1) // 2
    if len(model_items) < 2:
        _record_conflict_scan(
            service,
            story_id,
            poi_key,
            detector=detector,
            status="no_candidates",
            pair_count=0,
            detected_count=0,
            coverage_complete=True,
        )
        return []
    detector_fn = getattr(service.providers.gemini, "detect_fact_conflicts", None)
    if not callable(detector_fn):
        _record_conflict_scan(
            service,
            story_id,
            poi_key,
            detector=detector,
            status="detector_missing",
            pair_count=pair_count,
            detected_count=0,
        )
        return []
    try:
        detector_result = await detector_fn(model_items, context or {})
        if isinstance(detector_result, dict):
            records = list(detector_result.get("records") or [])
            coverage_complete = detector_result.get("coverage_complete") is True
        else:
            records = list(detector_result or [])
            # Compatibility providers predate paginated/full scans. They can only
            # be considered complete while the historical bounded set fits.
            coverage_complete = len(model_items) <= 80
    except (GeminiUnavailable, MalformedProviderResponse, PermanentProviderError, RetryableProviderError) as exc:
        _record_conflict_scan(
            service,
            story_id,
            poi_key,
            detector=detector,
            status="detector_unavailable",
            pair_count=pair_count,
            detected_count=0,
            error_type=type(exc).__name__,
        )
        from .identity_telemetry import record_identity_event
        record_identity_event(
            service,
            story_id,
            "fact_conflict_detector_unavailable",
            {
                "error_type": type(exc).__name__,
                "pair_count": pair_count,
                "detector": detector[:120],
            },
            source="fact_conflict",
        )
        return []
    _record_conflict_scan(
        service,
        story_id,
        poi_key,
        detector=detector,
        status="ok",
        pair_count=pair_count,
        detected_count=len(records),
        coverage_complete=coverage_complete,
    )
    return persist_fact_conflicts(
        service,
        story_id,
        poi_key,
        records,
        detector=detector,
    )


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
    if resolution not in FINAL_RESOLUTIONS:
        raise ValueError("invalid_fact_conflict_resolution")
    if not reason or len(reason) > 1200:
        raise ValueError("fact_conflict_reason_required")
    confidence = max(0.0, min(1.0, float(confidence)))
    changed = False
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
            changed = True
            evidence = json.loads(row["evidence_json"] or "{}")
            evidence["mira_arbitration_confidence"] = confidence
            db.execute(
                """
                UPDATE fact_conflicts
                SET final_resolution=?,final_fact_id=?,arbitration_reason=?,
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
            db.execute(
                "UPDATE fact_conflicts SET evidence_json=? WHERE story_id=? AND conflict_id=?",
                (
                    json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    story_id,
                    conflict_id,
                ),
            )
        story = db.execute(
            "SELECT research_json FROM stories WHERE id=?", (story_id,)
        ).fetchone()
        durable = conflict_rows(db, story_id, limit=40)
        current = next(item for item in durable if item["conflict_id"] == conflict_id)
        poi_key = db.execute(
            "SELECT poi_key FROM fact_conflicts WHERE story_id=? AND conflict_id=?",
            (story_id, conflict_id),
        ).fetchone()["poi_key"]
        from .fact_ledger import refresh_review_status
        refresh_review_status(db, story_id, service.store.now())
        research = json.loads(story["research_json"] or "{}")
        research["fact_conflicts"] = durable
        research["fact_conflict_stats"] = conflict_stats(db, story_id, poi_key)
        db.execute(
            "UPDATE stories SET research_json=? WHERE id=?",
            (json.dumps(research, ensure_ascii=False, separators=(",", ":")), story_id),
        )
    if changed:
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
               arbitration_confidence,arbitrated_by,evidence_json,times_seen
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
