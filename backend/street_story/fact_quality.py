"""Deterministic fact quality boundary shared by research and Live web-search."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_SPACE = re.compile(r"\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+|\s*;\s*")
_CLAUSE_SPLIT = re.compile(
    r",\s*(?=(?:а\s+вместо|однако|поэтому)\b)",
    re.IGNORECASE,
)
_INLINE_HEADING = re.compile(
    r".*?\b(?:интересн\w*\s+факт\w*|история\s+создания)\b[:\s]*",
    re.IGNORECASE,
)
_RELATIVE_ASIDE = re.compile(
    r",\s*(?:именем|в\s+честь)\s+котор\w*[^,]{0,120},\s*",
    re.IGNORECASE,
)
_TEMPORAL_MARKER = re.compile(
    r"\b(?:"
    r"В\s+(?:начале|конце|середине)\s+(?:[IVXLCDM]+|\d{3,4})\s*(?:века|столетия)?|"
    r"(?:В|К|С|До|После)\s+\d{3,4}\s+(?:году|года)|"
    r"(?:Летом|Зимой|Осенью|Весной)\s+\d{3,4}\s+года"
    r")\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)(?:1[0-9]{3}|20[0-9]{2}|[5-9][0-9]{2})(?!\d)(?![-‑–—](?:лет|лети|летн|й|я|у)\w*)")
_BAD = re.compile(
    r"(?:интересн\w*\s+факт|истори\w*\s+создани|смотрите\s+также|"
    r"\b(?:copyright|license|лицензи\w*|фотограф\w*|автор\s+фото|"
    r"фото\s*[:—-]|изображени\w*|читать\s+далее|подробнее|вечером|"
    r"как\s+добраться|где\s+наход\w*|новая\s+жизн\w*|украша\w*\s+собой|цены\s+в|экскурси\w*)\b)",
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
    r"первоначальное\s+назначение|современное\s+назначение|проект\s+архитектора)\b",
    re.IGNORECASE,
)
_CONSTRUCTION_ACTION = r"(?:постро(?:ен\w*|ил\w*|ить|ят\w*)|строительств\w*|залож\w*|возвед\w*|сооруж\w*)"
_SIGNAL = re.compile(
    rf"(?:{_CONSTRUCTION_ACTION}|заверш\w*|основан\w*|откры\w*|"
    r"реконстру\w*|реставр\w*|восстанов\w*|снес\w*|демонтир\w*|"
    r"разруш\w*|передан\w*|вош[её]л\w*|стал\w*\s+частью|"
    r"использовал\w*|размещал\w*|посетил\w*|посещал\w*|"
    r"спроектир\w*|является\s+частью|принадлеж\w*|наход\w*|располож\w*|"
    r"потерял\w*\s+оборонительн\w*|перестал\w*|служил\w*|"
    r"существовал\w*|символ\w*|является\s+памятник\w*|получил\w*\s+статус|"
    r"прибыл\w*|присутствовал\w*|разобрал\w*|имел\w*\s+назван\w*|одно\s+из\s+зданий|"
    r"размеща\w*|работает\s+экспозици\w*|имеет\b|имеют\b|состоит\b|состоят\b)",
    re.IGNORECASE,
)
_KINDS = (
    ("construction", re.compile(_CONSTRUCTION_ACTION, re.IGNORECASE)),
    ("architect", re.compile(r"(?:архитектор|автор\s+проекта|проект\s+архитектора|спроектир\w*)", re.IGNORECASE)),
    ("foundation", re.compile(r"(?:основател\w*|основан\w*)", re.IGNORECASE)),
    ("reconstruction", re.compile(r"(?:реконстру\w*|реставр\w*|восстанов\w*)", re.IGNORECASE)),
    ("demolition", re.compile(r"(?:снес\w*|демонтир\w*|разруш\w*|разобрал\w*)", re.IGNORECASE)),
    ("ownership", re.compile(r"(?:передан\w*|вош[её]л\w*|стал\w*\s+частью|принадлеж\w*|одно\s+из\s+зданий|филиал\w*)", re.IGNORECASE)),
    ("visit", re.compile(r"(?:посетил\w*|посещал\w*|прибыл\w*|присутствовал\w*)", re.IGNORECASE)),
    ("name", re.compile(r"(?:имел\w*\s+назван\w*|называл\w*)", re.IGNORECASE)),
    ("use", re.compile(r"(?:использовал\w*|размеща\w*|назначени\w*|служил\w*|перестал\w*|потерял\w*\s+оборонительн\w*|работает\s+экспозици\w*)", re.IGNORECASE)),
    ("status", re.compile(r"(?:символ\w*|является\s+памятник\w*|получил\w*\s+статус)", re.IGNORECASE)),
    ("existence", re.compile(r"(?:существовал\w*)", re.IGNORECASE)),
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
    text = re.sub(r"\s*\[\d{1,3}\]\s*", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
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


def _normalize_candidate(sentence: str, prior_year: str | None = None) -> tuple[str | None, tuple[int, int, int] | None]:
    text = _SPACE.sub(" ", str(sentence or "")).strip(" \t\r\n-•")
    inline_heading = _INLINE_HEADING.search(text)
    if inline_heading:
        text = text[inline_heading.end():].strip(" :—-")
    heading = _HEADING.search(text)
    if heading:
        text = text[heading.end():].strip(" :—-")
    text = _RELATIVE_ASIDE.sub(" ", text)
    if prior_year and re.match(r"^с\s+того\s+же\s+года\b", text, re.IGNORECASE):
        text = re.sub(
            r"^с\s+того\s+же\s+года\b",
            f"С {prior_year} года",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    bad = _BAD.search(text)
    if bad:
        signal = _SIGNAL.search(text, bad.end()) or _ROLE.search(text, bad.end())
        if signal is None:
            return None, None
        prefix = text[bad.end():signal.start()]
        temporal = _TEMPORAL_PREFIX.search(prefix)
        start = bad.end() + temporal.start() if temporal else signal.start()
        text = text[start:]
    signal = _ROLE.search(text) or _SIGNAL.search(text)
    if signal is not None:
        temporal_matches = list(_TEMPORAL_MARKER.finditer(text[:signal.start()]))
        if temporal_matches:
            text = text[temporal_matches[-1].start():]
        elif signal.start() > 60:
            text = text[signal.start():]
    text = compact_fact_text(text)
    if re.search(r"(?:снес\w*|разобрал\w*|демонтир\w*|разруш\w*)", text, re.IGNORECASE):
        comma = text.find(",")
        if comma >= 12:
            text = text[:comma].rstrip()
    if text.endswith(("...", "…")):
        return None, None
    if len(text) < 12 or len(text) > 181:
        return None, None
    if _BAD.search(text) or _PERSONAL.search(text):
        return None, None
    if "http://" in text.lower() or "https://" in text.lower():
        return None, None
    if not (_ROLE.search(text) or _SIGNAL.search(text)):
        return None, None
    text = re.sub(r"^(?:а|однако)\s+", "", text, flags=re.IGNORECASE)
    if text and text[0].isalpha():
        text = text[0].upper() + text[1:]
    return text, _candidate_score(text, 0)


def atomic_fact_texts(raw: str, limit: int = 4) -> list[str]:
    original = _SPACE.sub(" ", str(raw or "")).strip(" \t\r\n-•")
    if len(original) < 12:
        return []
    result: list[str] = []
    prior_year: str | None = None
    for sentence in _SENTENCE_SPLIT.split(original):
        years = _YEAR.findall(sentence)
        clauses = _CLAUSE_SPLIT.split(sentence)
        first_signal = _SIGNAL.search(clauses[0]) or _ROLE.search(clauses[0])
        subject = clauses[0][:first_signal.start()].strip(" ,:;—-") if first_signal else ""
        if not subject or len(subject) > 80 or _YEAR.search(subject):
            subject = ""
        for clause_index, clause in enumerate(clauses):
            if clause_index > 0 and re.match(r"^а\s+вместо\s+них\b", clause, re.IGNORECASE):
                clause = re.sub(r"^а\s+вместо\s+них\s+", "", clause, flags=re.IGNORECASE)
                if subject and re.search(r"\bновые\.?$", clause, re.IGNORECASE):
                    clause = re.sub(
                        r"\bновые(\.)?$",
                        lambda match: f"новые {subject}{match.group(1) or ''}",
                        clause,
                        count=1,
                        flags=re.IGNORECASE,
                    )
            text, _score = _normalize_candidate(clause, prior_year)
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        # A sentence can contain a second independent architect claim.
        architect = re.search(
            r"\bпо\s+проекту\s+архитектора\s+([^,.;]{3,100})",
            sentence,
            re.IGNORECASE,
        )
        if architect:
            derived = compact_fact_text("Проект архитектора " + architect.group(1).strip() + ".")
            if derived not in result:
                result.append(derived)
        if years:
            prior_year = years[-1]
        if len(result) >= limit:
            break
    return result[:limit]


def atomic_fact_text(raw: str) -> str | None:
    facts = atomic_fact_texts(raw, limit=4)
    if not facts:
        return None
    return max(
        enumerate(facts),
        key=lambda item: _candidate_score(item[1], item[0]),
    )[1]

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
        texts = atomic_fact_texts(str(item.get("text") or ""))
        if not texts:
            continue
        sources = [
            source for source in (item.get("sources") or [])
            if isinstance(source, dict) and _source_key(source).startswith("https://")
        ]
        for text in texts:
            key = semantic_fact_key(str(item.get("claim_key") or ""), text)
            current = merged.get(key)
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
