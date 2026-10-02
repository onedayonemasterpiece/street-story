"""Host-side final-identity eligibility; search context may be broader than a physical object."""
from __future__ import annotations

import re

_LOCALITY = re.compile(
    r"[—–-]\s*(?:город|пос[её]лок(?:\s+городского\s+типа)?|село|деревня|"
    r"муниципальное\s+образование|городской\s+округ|муниципальный\s+округ|"
    r"район|область|край|административно[- ]территориальная\s+единица)\b",
    re.IGNORECASE,
)


def wikipedia_identity_eligible(title: str, extract: str) -> bool:
    """False for locality/container articles that can supply context but not the object name."""
    text = re.sub(r"\s+", " ", str(extract or ""))[:1400]
    if _LOCALITY.search(text):
        return False
    title_text = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    if re.search(r"\b(?:район|область|городской округ|муниципальный округ)\b", title_text):
        return False
    return True


def candidate_identity_eligible(candidate: dict) -> bool:
    return candidate.get("identity_eligible") is not False
