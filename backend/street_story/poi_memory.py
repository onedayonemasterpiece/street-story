from __future__ import annotations

import json
from typing import Any

from .fact_quality import legacy_semantic_key


def poi_key(identity: dict[str, Any]) -> str | None:
    value = str(identity.get("candidate_id") or "").strip()
    return value or None


def prior_facts(db, identity: dict[str, Any], story_id: str, limit: int = 60) -> list[dict[str, Any]]:
    key = poi_key(identity)
    if not key:
        return []
    rows = db.execute(
        "SELECT f.fact_id,f.text,f.confidence,f.evidence_supported,f.selected,f.sources_json,"
        "s.research_json,s.updated_at "
        "FROM facts f JOIN stories s ON s.id=f.story_id "
        "WHERE s.id<>? AND json_extract(s.research_json,'$.visual_identity.candidate_id')=? "
        "ORDER BY s.updated_at DESC,f.rowid LIMIT ?",
        (story_id, key, limit * 2),
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fact_id = str(row["fact_id"])
        try:
            research = json.loads(row["research_json"] or "{}")
        except (TypeError, ValueError):
            research = {}
        semantic_keys = research.get("fact_semantic_keys")
        semantic_keys = semantic_keys if isinstance(semantic_keys, dict) else {}
        semantic = str(semantic_keys.get(fact_id) or legacy_semantic_key(fact_id))
        if semantic in seen:
            continue
        seen.add(semantic)
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except (TypeError, ValueError):
            sources = []
        result.append({
            "fact_id": fact_id,
            "semantic_key": semantic,
            "text": str(row["text"]),
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "sources": sources if isinstance(sources, list) else [],
        })
        if len(result) >= limit:
            break
    return result
