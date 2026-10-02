"""Deterministic fact quality boundary shared by research and Live web-search."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_SPACE = re.compile(r"\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+|\s*;\s*")
_YEAR = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2}|[5-9][0-9]{2})\b")
_BAD = re.compile(
    r"(?:интересн\w*\s+факт|истори\w*\s+создани|смотрите\s+также|"
    r"\b(?:copyright|license|лицензи\w*|фотограф\w*|автор\s+фото|"
    r"фото\s*[:—-]|изображени\w*|читать\s+далее|подробнее|вечером|"
    r"как\s+добраться|где\s+наход\w*|новая\s+жизн\w*|цены\s+в|экскурси\w*)\b)",
    re.IGNORECASE,
)
_PERSONAL = re.compile(
    r"\b(?:я|мы|мне|нам|побывал\w*|посетил[аи]?\s+я|советую|рекомендую|отзыв\w*)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(
    r"^(?:история(?:\s+создания)?|ранняя\s+история|интересные\s+факты|"
    r"полный\s+гид|туры\s+на)\b",
    re.IGNORECASE,
)
_ROLE = re.compile(
    r"^(?:архитектор|основатель|заказчик|владелец|автор\s+проекта|"
    r"первоначальное\s+назначение|современное\s+назначение)\b",
    re.IGNORECASE,
)
_SIGNAL = re.compile(
    r"(?:постро\w*|строительств\w*|залож\w*|заверш\w*|возвед\w*|сооруж\w*|основан\w*|откры\w*|"
    r"реконстру\w*|реставр\w*|восстанов\w*|снес\w*|демонтир\w*|"
    r"разруш\w*|передан\w*|вош[её]л\w*|стал\w*\s+частью|"
    r"использовал\w*|размещал\w*|посетил\w*|посещал\w*|"
    r"спроектир\w*|является\s+частью|принадлеж\w*|наход\w*|располож\w*|"
    r"потерял\w*\s+оборонительн\w*|перестал\w*|служил\w*|"
    r"существовал\w*|символ\w*|является\s+памятник\w*|получил\w*\s+статус|"
    r"прибыл\w*|разобрал\w*|имел\w*\s+назван\w*|одно\s+из\s+зданий|"
    r"размеща\w*|работает\s+экспозици\w*|имеет\b|имеют\b|состоит\b|состоят\b)",
    re.IGNORECASE,
)
_KINDS = (
    ("construction", re.compile(r"(?:постро\w*|строительств\w*|залож\w*|возвед\w*|сооруж\w*)", re.IGNORECASE)),
    ("architect", re.compile(r"(?:архитектор|автор\s+проекта|спроектир\w*)", re.IGNORECASE)),
    ("foundation", re.compile(r"(?:основател\w*|основан\w*)", re.IGNORECASE)),
    ("reconstruction", re.compile(r"(?:реконстру\w*|реставр\w*|восстанов\w*)", re.IGNORECASE)),
    ("demolition", re.compile(r"(?:снес\w*|демонтир\w*|разруш\w*)", re.IGNORECASE)),
    ("ownership", re.compile(r"(?:передан\w*|вош[её]л\w*|стал\w*\s+частью|принадлеж\w*|одно\s+из\s+зданий|филиал\w*)", re.IGNORECASE)),
    ("visit", re.compile(r"(?:посетил\w*|посещал\w*|прибыл\w*)", re.IGNORECASE)),
    ("name", re.compile(r"(?:имел\w*\s+назван\w*|называл\w*)", re.IGNORECASE)),
    ("use", re.compile(r"(?:использовал\w*|размеща\w*|назначени\w*|служил\w*|перестал\w*|работает\s+экспозици\w*)", re.IGNORECASE)),
    ("opening", re.compile(r"(?:откры\w*)", re.IGNORECASE)),
    ("location", re.compile(r"(?:наход\w*|располож\w*)", re.IGNORECASE)),
    ("structure", re.compile(r"(?:имеет\b|имеют\b|состоит\b|состоят\b)", re.IGNORECASE)),
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


def _candidate_score(text: str, index: int) -> tuple[int, int, int]:
    score = 0
    if _ROLE.search(text):
        score += 5
    if _SIGNAL.search(text):
        score += 4
    if _YEAR.search(text):
        score += 3
    if len(text) <= 140:
        score += 1
    return score, -index, -len(text)


def atomic_fact_text(raw: str) -> str | None:
    original = _SPACE.sub(" ", str(raw or "")).strip(" \t\r\n-•")
    if len(original) < 12:
        return None
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for index, sentence in enumerate(_SENTENCE_SPLIT.split(original)):
        text = compact_fact_text(sentence)
        if _HEADING.search(text):
            # Legacy snippets often glue a page heading directly to a valid claim
            # ("История создания ... построены ..."). Keep the claim, not the heading.
            signal = _ROLE.search(text) or _SIGNAL.search(text)
            if signal is None:
                continue
            text = compact_fact_text(text[signal.start():])
        if len(text) < 12 or len(text) > 181:
            continue
        if _BAD.search(text) or _PERSONAL.search(text):
            continue
        if "http://" in text.lower() or "https://" in text.lower():
            continue
        # A date by itself is not a fact. Require a factual predicate/role;
        # the year then enriches/deduplicates that claim.
        if not (_ROLE.search(text) or _SIGNAL.search(text)):
            continue
        candidates.append((_candidate_score(text, index), text))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


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
        stable_words = sorted(set(words))[:8]
        return f"{kind}:" + ":".join(stable_words)
    normalized_claim = re.sub(r"[^a-zа-яё0-9]+", " ", str(claim_key or "").lower()).strip()
    if normalized_claim and normalized_claim not in {"fact", "history", "история", "факт"}:
        return "claim:" + normalized_claim[:120]
    return "text:" + hashlib.sha256(compact.lower().encode("utf-8")).hexdigest()[:20]


def semantic_fact_id(claim_key: str, text: str) -> str:
    key = semantic_fact_key(claim_key, text)
    return "claim_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


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
                "fact_id": semantic_fact_id(str(item.get("claim_key") or ""), text),
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
