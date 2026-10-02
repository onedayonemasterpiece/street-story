"""Mechanical boundary for model-produced facts.

Street Story is LLM-first for semantic decisions. This module deliberately does
not decide whether a sentence is a fact, what kind of fact it is, whether two
claims mean the same thing, or which claim is true. It only validates bounded
model output, derives stable opaque identifiers, and merges exact model
identities/sources.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_SPACE = re.compile(r"\s+")


def validated_model_fact_text(raw: Any, maximum: int = 500) -> str | None:
    text = _SPACE.sub(" ", str(raw or "")).strip()
    if not text or len(text) > maximum:
        return None
    return text


def normalized_claim_key(raw: Any, maximum: int = 300) -> str | None:
    key = _SPACE.sub(" ", str(raw or "")).strip().casefold()
    if not key or len(key) > maximum:
        return None
    return key


def model_fact_id(claim_key: Any, text: Any) -> str:
    key = normalized_claim_key(claim_key)
    if key is None:
        value = validated_model_fact_text(text)
        if value is None:
            raise ValueError("model_fact_identity_missing")
        key = "exact-text:" + value.casefold()
    return "claim_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def model_fact_key(item: dict[str, Any]) -> str | None:
    fact_id = str(item.get("fact_id") or "").strip()
    if fact_id:
        return "id:" + fact_id
    key = normalized_claim_key(item.get("claim_key"))
    if key:
        return "claim:" + key
    text = validated_model_fact_text(item.get("text"))
    if text:
        return "exact-text:" + hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:20]
    return None


def _source_key(source: dict[str, Any]) -> str:
    url = str(source.get("url") or "").rstrip("/")
    if url:
        return "url:" + url
    ref = str(source.get("ref") or source.get("evidence_ref") or "").strip()
    if ref:
        return "ref:" + ref
    return ""


def merge_model_fact_inventory(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        text = validated_model_fact_text(item.get("text"))
        key = model_fact_key(item)
        if text is None or key is None:
            continue
        sources = [
            source for source in (item.get("sources") or [])
            if isinstance(source, dict) and _source_key(source)
        ]
        current = merged.get(key)
        if current is None:
            fact_id = str(item.get("fact_id") or "").strip()
            if not fact_id:
                fact_id = model_fact_id(item.get("claim_key"), text)
            current = {
                **item,
                "fact_id": fact_id,
                "text": text,
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                "evidence_supported": bool(item.get("evidence_supported", bool(sources))),
                "selected": bool(item.get("selected")),
                "sources": [],
            }
            merged[key] = current
            order.append(key)
        else:
            # Same opaque model identity: newer model wording may refine the
            # presentation, but code does not attempt semantic equivalence.
            current["text"] = text
            current["confidence"] = max(
                float(current.get("confidence") or 0.0),
                max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
            )
            current["evidence_supported"] = bool(current.get("evidence_supported")) or bool(
                item.get("evidence_supported", bool(sources))
            )
            current["selected"] = bool(current.get("selected")) or bool(item.get("selected"))
        by_source = {
            _source_key(source): source
            for source in current["sources"]
            if _source_key(source)
        }
        for source in sources:
            by_source[_source_key(source)] = source
        current["sources"] = list(by_source.values())
    return [merged[key] for key in order]
