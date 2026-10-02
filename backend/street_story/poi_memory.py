from __future__ import annotations

import json
from typing import Any


def poi_key(identity: dict[str, Any]) -> str | None:
    value = str(identity.get("candidate_id") or "").strip()
    return value or None


def prior_facts(db, identity: dict[str, Any], story_id: str, limit: int = 60) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    rows = db.execute(
        "SELECT f.fact_id,f.text,f.confidence,f.evidence_supported,f.selected,f.sources_json,s.updated_at "
        "FROM facts f JOIN stories s ON s.id=f.story_id "
        "WHERE s.id<>? AND json_extract(s.research_json,'$.visual_identity.candidate_id')=? "
        "ORDER BY s.updated_at DESC,f.rowid LIMIT ?",
        (story_id, key, limit),
    )
    result = []
    seen = set()
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
        })
    return result
