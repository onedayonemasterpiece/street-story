"""Fresh author evidence and bounded noise-turn classification for Live."""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ¿¡]")
_LANGUAGE_SWITCH = re.compile(
    r"\b(?:english|spanish|german|french|language|speak|switch|русск|английск|испанск|немецк|язык)\w*\b",
    re.IGNORECASE,
)


def normalized(text: str) -> str:
    return " ".join(
        re.findall(
            r"[^\W_]+",
            unicodedata.normalize("NFKC", text).casefold().replace("ё", "е"),
            re.UNICODE,
        )
    )


def begin_turn(session, text: str = "", *, origin: str = "audio") -> None:
    serial = int(session.state.get("author_turn_serial") or 0) + 1
    session.state["author_turn_serial"] = serial
    session.state["author_turn"] = {
        "serial": serial,
        "text": text[:4000],
        "at": time.monotonic(),
        "origin": origin,
        "consumed": False,
        "suspected_noise": False,
    }


def observe_input_timing(session, event: dict[str, Any]) -> None:
    """Keep only bounded timing evidence for the next transcript classification."""
    try:
        chunks = max(0, min(10_000, int(event.get("audio_chunks") or 0)))
    except (TypeError, ValueError):
        chunks = 0
    session.state["last_input_timing"] = {
        "audio_chunks": chunks,
        "at": time.monotonic(),
    }


def suspected_noise_transcript(text: str, *, audio_chunks: int, origin: str = "audio") -> bool:
    """Detect the exact high-risk shape seen on the owner phone.

    This is deliberately narrow: audio-only, short foreign fragments, after a
    substantial admitted audio turn. Coherent foreign speech and explicit
    language-switch requests remain valid.
    """
    if origin != "audio":
        return False
    clean = " ".join(str(text or "").split()).strip()
    if not clean or len(clean) > 32 or _LANGUAGE_SWITCH.search(clean):
        return False
    words = normalized(clean).split()
    if not 1 <= len(words) <= 3:
        return False
    if _CYRILLIC.search(clean) or not _LATIN.search(clean):
        return False
    return audio_chunks >= 15


def observe_transcript(session, text: str) -> bool:
    turn = session.state.get("author_turn")
    if (
        not isinstance(turn, dict)
        or turn.get("sealed")
        or time.monotonic() - float(turn.get("at") or 0) > 30
    ):
        begin_turn(session)
        turn = session.state["author_turn"]
    previous = str(turn.get("text") or "")
    if text.startswith(previous):
        turn["text"] = text[:4000]
    elif not previous.endswith(text):
        turn["text"] = (previous + " " + text).strip()[:4000]
    turn["at"] = time.monotonic()

    timing = session.state.get("last_input_timing")
    chunks = 0
    if isinstance(timing, dict) and time.monotonic() - float(timing.get("at") or 0) <= 5:
        try:
            chunks = int(timing.get("audio_chunks") or 0)
        except (TypeError, ValueError):
            chunks = 0
    suspected = suspected_noise_transcript(
        str(turn.get("text") or ""),
        audio_chunks=chunks,
        origin=str(turn.get("origin") or "audio"),
    )
    turn["suspected_noise"] = suspected
    turn["audio_chunks"] = chunks
    return suspected


def suspected_noise_turn(session) -> bool:
    turn = session.state.get("author_turn")
    return bool(
        isinstance(turn, dict)
        and turn.get("suspected_noise")
        and not turn.get("consumed")
        and 0 <= time.monotonic() - float(turn.get("at") or 0) <= 30
    )


def noise_receipt(session) -> dict[str, Any]:
    turn = session.state.get("author_turn") or {}
    return {
        "policy": "russian-preferred-short-foreign-noise-v1",
        "turn_serial": turn.get("serial"),
        "audio_chunks": int(turn.get("audio_chunks") or 0),
    }


def has_place_consent(session, candidate_name: str) -> bool:
    turn = session.state.get("author_turn") or {}
    text = str(turn.get("text") or "")
    value, name = normalized(text), normalized(candidate_name)
    # A short acknowledgement, a greeting, a question and old transcript context
    # cannot authorize the model to confirm whichever candidate it guessed.
    if (
        turn.get("consumed")
        or turn.get("suspected_noise")
        or not 0 <= time.monotonic() - float(turn.get("at") or 0) <= 30
    ):
        return False
    if not name or (" " + name + " ") not in (" " + value + " ") or "?" in text:
        return False
    words = set(value.split())
    if words.intersection(
        {
            "не",
            "нет",
            "not",
            "no",
            "неверно",
            "ошибка",
            "другой",
            "кажется",
            "возможно",
            "наверное",
            "похоже",
        }
    ):
        return False
    positive = words.intersection(
        {"подтверждаю", "подтверждаем", "верно", "правильно", "да", "yes", "confirm", "confirmed", "sí", "ja"}
    )
    return bool(positive or ("это " + name) in value)


def consent_receipt(session) -> dict:
    turn = session.state.get("author_turn") or {}
    return {
        "kind": "fresh_explicit_named_author_turn",
        "turn_serial": turn.get("serial"),
        "origin": turn.get("origin"),
        "policy": "identity-consent-v1",
    }
