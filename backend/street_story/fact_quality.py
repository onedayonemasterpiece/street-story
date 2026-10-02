"""Mechanical fact-curation contract.

This module deliberately does no semantic NLP. The model decides what a fact means,
whether two statements are duplicates, whether a fact was seen before and whether
claims contradict. Host code validates references/shape and materializes exactly
those model decisions.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any

DISPOSITIONS = {"new", "merge", "seen_before", "update"}
RELATIONS = {
    "contradiction",
    "scope_difference",
    "temporal_sequence",
    "source_disagreement",
    "uncertain",
}
RESOLUTIONS = {"prefer_left", "prefer_right", "both_valid", "unresolved"}

_KEY_SYNTAX = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
_SPACE = re.compile(r"\s+")


class FactCurationError(ValueError):
    pass


def compact_text(value: Any, limit: int = 220) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    if not text or len(text) > limit:
        raise FactCurationError("fact_text_out_of_bounds")
    return text


def semantic_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _KEY_SYNTAX.fullmatch(key):
        raise FactCurationError("semantic_key_invalid")
    return key


def semantic_fact_id(key: str) -> str:
    return "fact_" + hashlib.sha256(semantic_key(key).encode("utf-8")).hexdigest()[:24]


def legacy_semantic_key(fact_id: str) -> str:
    fact_id = str(fact_id or "").strip()
    if not fact_id:
        raise FactCurationError("legacy_fact_id_missing")
    return "legacy:" + fact_id[:140]


def _source_urls(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for source in item.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").rstrip("/")
        if url.startswith("https://") and url not in values:
            values.append(url)
    return values


def _source_summary(item: dict[str, Any]) -> dict[str, Any]:
    sources = [source for source in item.get("sources") or [] if isinstance(source, dict)]
    domains: list[str] = []
    official = False
    from urllib.parse import urlparse
    for source in sources:
        url = str(source.get("url") or "")
        if str(source.get("type") or "") == "official":
            official = True
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host and host not in domains:
            domains.append(host)
    return {
        "source_count": len(_source_urls(item)),
        "domain_count": len(domains),
        "official_source_present": official,
    }


def inventory_context(
    rows: list[Any],
    semantic_keys: dict[str, str] | None = None,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    semantic_keys = semantic_keys or {}
    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        fact_id = str(row["fact_id"])
        key = semantic_keys.get(fact_id) or legacy_semantic_key(fact_id)
        try:
            sources = row.get("sources") if isinstance(row, dict) else None
        except AttributeError:
            sources = None
        if sources is None:
            import json
            try:
                sources = json.loads(row["sources_json"] or "[]")
            except (TypeError, ValueError):
                sources = []
        item = {
            "fact_id": fact_id,
            "semantic_key": key,
            "text": str(row["text"])[:260],
            "confidence": float(row["confidence"]),
            "evidence_supported": bool(row["evidence_supported"]),
            "selected": bool(row["selected"]),
            "sources": sources if isinstance(sources, list) else [],
        }
        result.append(item)
    return result


def compact_inventory_for_model(items: list[dict[str, Any]], limit: int = 48) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items[:limit]:
        try:
            key = semantic_key(item.get("semantic_key"))
            text = compact_text(item.get("text"), 260)
        except FactCurationError:
            continue
        result.append({
            "semantic_key": key,
            "text": text,
            "selected": bool(item.get("selected")),
            **_source_summary(item),
        })
    return result


def _bounded_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FactCurationError("confidence_invalid") from None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise FactCurationError("confidence_invalid")
    return number


def validate_curation(
    payload: dict[str, Any],
    *,
    allowed_source_urls: set[str],
    current_keys: set[str],
    prior_keys: set[str],
    max_facts: int = 24,
    max_conflicts: int = 24,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FactCurationError("curation_not_object")
    raw_facts = payload.get("facts")
    raw_conflicts = payload.get("conflicts", [])
    if not isinstance(raw_facts, list) or not isinstance(raw_conflicts, list):
        raise FactCurationError("curation_lists_invalid")
    if len(raw_facts) > max_facts or len(raw_conflicts) > max_conflicts:
        raise FactCurationError("curation_too_large")

    known_input_keys = current_keys | prior_keys
    facts: list[dict[str, Any]] = []
    output_keys: set[str] = set()
    for raw in raw_facts:
        if not isinstance(raw, dict):
            raise FactCurationError("fact_not_object")
        key = semantic_key(raw.get("semantic_key"))
        if key in output_keys:
            raise FactCurationError("duplicate_semantic_key")
        text = compact_text(raw.get("text"))
        disposition = str(raw.get("disposition") or "")
        if disposition not in DISPOSITIONS:
            raise FactCurationError("fact_disposition_invalid")
        inherits_raw = raw.get("inherits_keys", [])
        source_raw = raw.get("source_urls", [])
        if not isinstance(inherits_raw, list) or not isinstance(source_raw, list):
            raise FactCurationError("fact_references_invalid")
        inherits: list[str] = []
        for value in inherits_raw[:12]:
            inherited = semantic_key(value)
            if inherited not in known_input_keys:
                raise FactCurationError("unknown_inherited_key")
            if inherited not in inherits:
                inherits.append(inherited)
        urls: list[str] = []
        for value in source_raw[:20]:
            url = str(value or "").rstrip("/")
            if url not in allowed_source_urls:
                raise FactCurationError("unseen_source_url")
            if url not in urls:
                urls.append(url)
        if disposition == "merge" and not any(key0 in current_keys for key0 in inherits):
            raise FactCurationError("merge_requires_current_key")
        if disposition == "seen_before" and not any(key0 in prior_keys for key0 in inherits):
            raise FactCurationError("seen_before_requires_prior_key")
        facts.append({
            "semantic_key": key,
            "text": text,
            "disposition": disposition,
            "inherits_keys": inherits,
            "confidence": _bounded_confidence(raw.get("confidence", 0.0)),
            "source_urls": urls,
            "rationale": compact_text(raw.get("rationale") or "model curation", 800),
        })
        output_keys.add(key)

    conflict_keys = known_input_keys | output_keys
    conflicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for raw in raw_conflicts:
        if not isinstance(raw, dict):
            raise FactCurationError("conflict_not_object")
        left = semantic_key(raw.get("left_key"))
        right = semantic_key(raw.get("right_key"))
        if left == right or left not in conflict_keys or right not in conflict_keys:
            raise FactCurationError("conflict_reference_invalid")
        pair = tuple(sorted((left, right)))
        if pair in seen_pairs:
            continue
        relation = str(raw.get("relation") or "")
        resolution = str(raw.get("suggested_resolution") or "")
        if relation not in RELATIONS or resolution not in RESOLUTIONS:
            raise FactCurationError("conflict_class_invalid")
        needs_more_search = bool(raw.get("needs_more_search"))
        search_query = str(raw.get("search_query") or "").strip()[:500]
        if needs_more_search and not search_query:
            raise FactCurationError("conflict_search_query_required")
        conflicts.append({
            "left_key": left,
            "right_key": right,
            "relation": relation,
            "suggested_resolution": resolution,
            "confidence": _bounded_confidence(raw.get("confidence", 0.0)),
            "rationale": compact_text(raw.get("rationale") or "model comparison", 1000),
            "needs_more_search": needs_more_search,
            "search_query": search_query,
        })
        seen_pairs.add(pair)

    return {"facts": facts, "conflicts": conflicts}


def materialize_curation(
    current_items: list[dict[str, Any]],
    proposal_facts: list[dict[str, Any]],
    source_objects: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    current_by_key = {semantic_key(item["semantic_key"]): dict(item) for item in current_items}
    retired: set[str] = set()
    for proposal in proposal_facts:
        if proposal["disposition"] in {"merge", "update"}:
            retired.update(key for key in proposal["inherits_keys"] if key in current_by_key)

    result: dict[str, dict[str, Any]] = {
        key: item for key, item in current_by_key.items() if key not in retired
    }
    order = [key for key in current_by_key if key not in retired]

    for proposal in proposal_facts:
        disposition = proposal["disposition"]
        if disposition == "seen_before" and not any(
            key in current_by_key for key in proposal["inherits_keys"]
        ):
            continue
        key = proposal["semantic_key"]
        inherited_current = [
            current_by_key[item_key]
            for item_key in proposal["inherits_keys"]
            if item_key in current_by_key
        ]
        existing = result.get(key) or current_by_key.get(key)
        base_sources: dict[str, dict[str, Any]] = {}
        for item in ([existing] if existing else []) + inherited_current:
            if not item:
                continue
            for source in item.get("sources") or []:
                if isinstance(source, dict):
                    url = str(source.get("url") or "").rstrip("/")
                    if url.startswith("https://"):
                        base_sources[url] = source
        for url in proposal["source_urls"]:
            source = source_objects.get(url)
            if source:
                base_sources[url] = source

        if existing is not None:
            selected = bool(existing.get("selected"))
        elif inherited_current:
            selected = any(bool(item.get("selected")) for item in inherited_current)
        else:
            selected = bool(base_sources)

        item = {
            "semantic_key": key,
            "fact_id": semantic_fact_id(key),
            "text": proposal["text"],
            "confidence": proposal["confidence"],
            "evidence_supported": bool(base_sources),
            "selected": selected and bool(base_sources),
            "sources": sorted(
                base_sources.values(),
                key=lambda source: (
                    str(source.get("type") or "") != "official",
                    str(source.get("url") or ""),
                ),
            ),
        }
        result[key] = item
        if key not in order:
            order.append(key)

    materialized = [result[key] for key in order]
    key_map = {item["fact_id"]: item["semantic_key"] for item in materialized}
    return materialized, key_map
