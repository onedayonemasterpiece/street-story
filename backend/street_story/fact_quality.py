"""Deterministic fact quality boundary shared by research and Live web-search."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_SPACE = re.compile(r"\s+")
_YEAR = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2}|[5-9][0-9]{2})\b")
_BAD = re.compile(
    r"(?:интересн\w*\s+факт|истори\w*\s+создани|"
    r"\b(?:copyright|license|лицензи\w*|фотограф\w*|автор\s+фото|"
    r"фото\s*[:—-]|изображени\w*|читать\s+далее|подробнее|вечером)\b)",
    re.IGNORECASE,
)
_ROLE = re.compile(
    r"^(?:архитектор|основатель|заказчик|владелец|автор\s+проекта|"
    r"первоначальное\s+назначение|современное\s+назначение)\b",
    re.IGNORECASE,
)
_SIGNAL = re.compile(
    r"(?:постро\w*|возвед\w*|сооруж\w*|основан\w*|откры\w*|"
    r"реконстру\w*|реставр\w*|восстанов\w*|снес\w*|демонтир\w*|"
    r"разруш\w*|передан\w*|вош[её]л\w*|стал\w*\s+частью|"
    r"использовал\w*|размещал\w*|посетил\w*|посещал\w*|"
    r"спроектир\w*|является\s+частью|принадлеж\w*)",
    re.IGNORECASE,
)
_KINDS = (
    ("construction", re.compile(r"(?:постро\w*|возвед\w*|сооруж\w*)", re.IGNORECASE)),
    ("architect", re.compile(r"(?:архитектор|автор\s+проекта|спроектир\w*)", re.IGNORECASE)),
    ("foundation", re.compile(r"(?:основател\w*|основан\w*)", re.IGNORECASE)),
    ("reconstruction", re.compile(r"(?:реконстру\w*|реставр\w*|восстанов\w*)", re.IGNORECASE)),
    ("demolition", re.compile(r"(?:снес\w*|демонтир\w*|разруш\w*)", re.IGNORECASE)),
    ("ownership", re.compile(r"(?:передан\w*|вош[её]л\w*|стал\w*\s+частью|принадлеж\w*)", re.IGNORECASE)),
    ("visit", re.compile(r"(?:посетил\w*|посещал\w*)", re.IGNORECASE)),
    ("use", re.compile(r"(?:использовал\w*|размещал\w*|назначени\w*)", re.IGNORECASE)),
    ("opening", re.compile(r"(?:откры\w*)", re.IGNORECASE)),
)
_STOP = {
    "котор", "этого", "этой", "этот", "была", "были", "было", "стал", "стала",
    "после", "перед", "частью", "город", "калининград", "королевск", "ворот",
    "объект", "здани", "сооружени", "год", "году", "годы", "века",
}


def compact_fact_text(raw: str, limit: int = 180) -> str:
    text = _SPACE.sub(" ", str(raw or "")).strip(" \t\r\n-•")
    if len(text) <= limit:
        return text
    stops = [pos + 1 for mark in (".", ";") if (pos := text.rfind(mark, 0, limit)) >= 60]
    end = max(stops, default=text.rfind(" ", 0, limit))
    if end < 60:
        end = limit - 1
    return text[:end].rstrip(" ,;:-") + "…"


def atomic_fact_text(raw: str) -> str | None:
    text = compact_fact_text(raw)
    if len(text) < 12 or _BAD.search(text):
        return None
    if "http://" in text.lower() or "https://" in text.lower():
        return None
    if not (_YEAR.search(text) or _ROLE.search(text) or _SIGNAL.search(text)):
        return None
    return text


def fact_kind(text: str) -> str:
    for name, pattern in _KINDS:
        if pattern.search(text):
            return name
    return "other"


def semantic_fact_key(claim_key: str, text: str) -> str:
    compact = atomic_fact_text(text) or compact_fact_text(text)
    kind = fact_kind(compact)
    numbers = "-".join(dict.fromkeys(_YEAR.findall(compact)))
    if kind != "other" and numbers:
        return f"{kind}:{numbers}"
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", compact)
        if word.lower() not in _STOP
    ]
    if kind != "other" and words:
        return f"{kind}:" + ":".join(dict.fromkeys(words[:5]))
    normalized_claim = re.sub(r"[^a-zа-яё0-9]+", " ", str(claim_key or "").lower()).strip()
    if normalized_claim and normalized_claim not in {"fact", "history", "история", "факт"}:
        return "claim:" + normalized_claim[:120]
    return "text:" + hashlib.sha256(compact.lower().encode("utf-8")).hexdigest()[:20]


def _source_key(source: dict[str, Any]) -> str:
    return str(source.get("url") or "").rstrip("/")


def merge_fact_inventory(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        text = atomic_fact_text(str(item.get("text") or ""))
        if text is None:
            continue
        key = semantic_fact_key(str(item.get("claim_key") or ""), text)
        current = merged.get(key)
        sources = [
            source for source in (item.get("sources") or [])
            if isinstance(source, dict) and _source_key(source).startswith("https://")
        ]
        if current is None:
            current = {
                **item,
                "semantic_key": key,
                "fact_id": "claim_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20],
                "text": text,
                "confidence": float(item.get("confidence") or 0.0),
                "evidence_supported": bool(item.get("evidence_supported", bool(sources))),
                "selected": bool(item.get("selected")),
                "sources": [],
            }
            merged[key] = current
            order.append(key)
        else:
            if len(text) < len(str(current.get("text") or "")):
                current["text"] = text
            current["confidence"] = max(float(current.get("confidence") or 0.0), float(item.get("confidence") or 0.0))
            current["evidence_supported"] = bool(current.get("evidence_supported")) or bool(item.get("evidence_supported", bool(sources)))
            current["selected"] = bool(current.get("selected")) or bool(item.get("selected"))
        by_url = {_source_key(source): source for source in current["sources"]}
        for source in sources:
            by_url[_source_key(source)] = source
        current["sources"] = sorted(
            by_url.values(),
            key=lambda source: (str(source.get("type") or "") != "official", _source_key(source)),
        )
    return [merged[key] for key in order]
