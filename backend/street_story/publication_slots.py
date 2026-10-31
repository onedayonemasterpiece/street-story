"""Street Story native publication slots; occupancy lives in publish_intents.

The channel account owns the native queue. This module does not run a scheduler.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

KALININGRAD = timezone(timedelta(hours=2))
SLOT_CLOCK = {"morning": time(9, 10), "evening": time(19, 10)}
NETWORK_DELAY = {"telegram": 0, "vk": 10}


def _part(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    return {"утром": "morning", "утро": "morning", "morning": "morning",
            "вечером": "evening", "вечер": "evening", "evening": "evening"}.get(normalized) or _invalid()


def _invalid():
    raise ValueError("Invalid publication slot day or daypart")


def _date(value: str | None, today: date) -> date | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)
    if normalized in {"сегодня", "today"}:
        return today
    try:
        result = date.fromisoformat(normalized)
    except ValueError:
        return _invalid()
    return result


def _occupied(db) -> set[tuple[date, str]]:
    booked: set[tuple[date, str]] = set()
    for row in db.execute("SELECT scheduled_for,request_json FROM publish_intents "
                          "WHERE state NOT IN ('cancelled','deleted')"):
        request = json.loads(row["request_json"] or "{}")
        raw = request.get("slot_at") or row["scheduled_for"]
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(KALININGRAD)
        except (ValueError, TypeError):
            continue
        for part, clock in SLOT_CLOCK.items():
            base = datetime.combine(moment.date(), clock, tzinfo=KALININGRAD)
            if timedelta(0) <= moment - base <= timedelta(minutes=20):
                booked.add((moment.date(), part))
    return booked


def choose_slot(db, *, day: str | None = None, period: str | None = None,
                now: datetime | None = None) -> datetime:
    """Return the nearest free requested slot; the caller holds the SQLite write tx."""
    present = (now or datetime.now(timezone.utc)).astimezone(KALININGRAD)
    requested_date, requested_period = _date(day, present.date()), _part(period)
    if requested_date and (requested_date < present.date() or
                           requested_date > present.date() + timedelta(days=90)):
        _invalid()
    occupied = _occupied(db)
    days = [requested_date] if requested_date else [
        present.date() + timedelta(days=i) for i in range(91)]
    for current in days:
        for part, clock in SLOT_CLOCK.items():
            if requested_period and part != requested_period:
                continue
            candidate = datetime.combine(current, clock, tzinfo=KALININGRAD)
            if candidate <= present + timedelta(minutes=2) or (current, part) in occupied:
                continue
            return candidate
    raise ValueError("No free publication slot for the requested day/daypart")


def delivery_time(slot: datetime, alias: str) -> str:
    """Same logical slot, distinct native TG/VK delivery times (10 minutes apart)."""
    provider = "telegram" if alias.startswith("tg_") else "vk" if alias.startswith("vk_") else ""
    if provider not in NETWORK_DELAY:
        raise ValueError("Unsupported publication slot provider")
    return (slot + timedelta(minutes=NETWORK_DELAY[provider])).isoformat()
