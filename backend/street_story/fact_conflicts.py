"""Conflict detection ledger for evidence-backed POI facts.

Detection is model-assisted, but pair selection, validation, persistence and telemetry
are deterministic. A conflict record is evidence for later arbitration, not a truth vote.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse
from typing import Any

from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .fact_quality import atomic_fact_text, fact_kind, semantic_fact_id, semantic_fact_key
from .gemini import GeminiUnavailable


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
_SINGLE_VALUE_KINDS = {"architect", "foundation", "location"}
_INTERESTING_KINDS = {
    "construction", "architect", "foundation", "reconstruction", "demolition",
    "ownership", "visit", "use", "opening", "location", "structure",
}


def _source_summary(item: dict[str, Any]) -> dict[str, Any]:
    sources = [
        source for source in (item.get("sources") or [])
        if isinstance(source, dict) and str(source.get("url") or "").startswith("https://")
    ]
    urls = list(dict.fromkeys(str(source["url"]).rstrip("/") for source in sources))
    domains = list(dict.fromkeys(
        (urlparse(url).hostname or "").lower().removeprefix("www.") for url in urls
    ))
    official_urls = [
        str(source["url"]).rstrip("/")
        for source in sources
        if str(source.get("type") or "") == "official"
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
        "source_count": len(urls),
        "domain_count": len([domain for domain in domains if domain]),
        "official": bool(official_urls),
        "official_urls": official_urls[:3],
        "source_urls": urls[:8],
        "supports": supports,
    }


def _fact_snapshot(item: dict[str, Any]) -> dict[str, Any] | None:
    text = atomic_fact_text(str(item.get("text") or ""))
    if text is None:
        return None
    claim_key = str(item.get("claim_key") or "")
    return {
        "fact_id": str(item.get("fact_id") or semantic_fact_id(claim_key, text)),
        "semantic_key": semantic_fact_key(claim_key, text),
        "kind": fact_kind(text),
        "text": text,
        "evidence": _source_summary(item),
    }


def _pair_id(left: dict[str, Any], right: dict[str, Any]) -> str:
    identities = sorted([
        left["fact_id"] + ":" + hashlib.sha256(left["text"].encode("utf-8")).hexdigest()[:12],
        right["fact_id"] + ":" + hashlib.sha256(right["text"].encode("utf-8")).hexdigest()[:12],
    ])
    return "conflict_" + hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()[:20]


def conflict_candidate_pairs(items: list[dict[str, Any]], max_pairs: int = 24) -> list[dict[str, Any]]:
    facts = [fact for item in items if (fact := _fact_snapshot(item)) is not None]
    candidates: list[tuple[int, dict[str, Any]]] = []
    seen_pairs: set[str] = set()
    for left_index, left in enumerate(facts):
        if left["kind"] not in _INTERESTING_KINDS:
            continue
        for right in facts[left_index + 1:]:
            if right["kind"] != left["kind"] or right["text"].casefold() == left["text"].casefold():
                continue
            pair_id = _pair_id(left, right)
            if pair_id in seen_pairs:
                continue
            seen_pairs.add(pair_id)
            score = 1
            if left["semantic_key"] == right["semantic_key"]:
                score += 8
            if left["kind"] in _SINGLE_VALUE_KINDS:
                score += 5
            if left["evidence"]["official"] != right["evidence"]["official"]:
                score += 2
            score += min(3, left["evidence"]["source_count"] + right["evidence"]["source_count"])
            candidates.append((score, {"pair_id": pair_id, "left": left, "right": right}))
    candidates.sort(key=lambda item: (-item[0], item[1]["pair_id"]))
    return [pair for _score, pair in candidates[:max_pairs]]


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


def conflict_stats(db, story_id: str, poi_key: str | None = None) -> dict[str, Any]:
    by_relation = {
        str(row["relation"]): int(row["count"])
        for row in db.execute(
            "SELECT relation,COUNT(*) AS count FROM fact_conflicts "
            "WHERE story_id=? GROUP BY relation",
            (story_id,),
        )
    }
    result: dict[str, Any] = {
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
        result["poi_total_detected"] = db.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE poi_key=?", (poi_key,)
        ).fetchone()[0]
        result["poi_open"] = db.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE poi_key=? "
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
    pairs = conflict_candidate_pairs(items)
    if not pairs:
        return []
    detector_fn = getattr(service.providers.gemini, "detect_fact_conflicts", None)
    if not callable(detector_fn):
        return []
    try:
        records = await detector_fn(pairs, context or {})
    except (GeminiUnavailable, MalformedProviderResponse, PermanentProviderError, RetryableProviderError) as exc:
        # Conflict analysis is observability/arbitration support. Provider failure
        # must not make the primary factual research unavailable.
        from .identity_telemetry import record_identity_event
        record_identity_event(
            service,
            story_id,
            "fact_conflict_detector_unavailable",
            {
                "error_type": type(exc).__name__,
                "pair_count": len(pairs),
                "detector": detector[:120],
            },
            source="fact_conflict",
        )
        return []
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
