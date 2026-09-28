#!/usr/bin/env python3
"""Production acceptance for the current Street Story Live product path.

The canary creates one real-photo topic and keeps one Gemini 3.8 Live session
through search, text editing, visual generation and publication confirmation.
By default it stops before dispatch. The explicit --execute-publication mode
continues through native Telegram scheduling and, by default, cancellation in the
same session. --keep-publication is an explicit owner acceptance mode that leaves
the scheduled test post in place for provider readback.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any

import httpx

BASE_URL = "https://street-story.kenigevents.ru"
MODEL = "gemini-3.8-live"
POLL_SECONDS = 0.5
TURN_TIMEOUT_SECONDS = 120
VISUAL_TIMEOUT_SECONDS = 12 * 60
SOCIAL_TIMEOUT_SECONDS = 6 * 60
OWNER_PROMPT_SHA256 = "4eab6d0cfcafc84881cad86380baa9920785b7e18e9a934923966995802380a3"
DIAGNOSTIC_NAME = "diagnostic-devcoveer-live-product.json"
TEST_DESTINATION_MARKERS = ("test", "safe", "e2e")


class ProductSmokeError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_installer(root: Path):
    path = root / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    if spec is None or spec.loader is None:
        raise ProductSmokeError("installer_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_device_token(installer) -> str:
    path = installer.DEVICE_TOKEN_FILE
    if not path.exists():
        raise ProductSmokeError("device_token_missing")
    installer.require_mode(path, 0o600)
    token, created = installer.device_token()
    if created:
        raise ProductSmokeError("device_token_unexpectedly_created")
    if not isinstance(token, str) or len(token) < 32:
        raise ProductSmokeError("device_token_invalid")
    return token


def _json(response: httpx.Response, code: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise ProductSmokeError(f"{code}_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProductSmokeError(f"{code}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise ProductSmokeError(f"{code}_invalid_shape")
    return payload


def fixture_meta(path: Path | None = None) -> tuple[dict[str, Any], Path]:
    path = (path or Path(__file__).with_name("golden_fixture.json")).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductSmokeError("fixture_metadata_invalid") from exc
    required = ("source_sha1", "source_sha256", "latitude", "longitude", "expected_object")
    if not isinstance(payload, dict) or any(not payload.get(name) for name in required):
        raise ProductSmokeError("fixture_metadata_incomplete")
    if not payload.get("source_file") and not payload.get("download_url"):
        raise ProductSmokeError("fixture_source_missing")
    return payload, path


def fixture_path(value: str | None) -> Path | None:
    if not value:
        return None
    root = repo_root().resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProductSmokeError("fixture_path_outside_repository") from exc
    if not candidate.is_file():
        raise ProductSmokeError("fixture_metadata_missing")
    return candidate


def validate_fixture_bytes(meta: dict[str, Any], data: bytes) -> bytes:
    if hashlib.sha1(data).hexdigest().lower() != str(meta["source_sha1"]).lower():
        raise ProductSmokeError("fixture_sha1_mismatch")
    if hashlib.sha256(data).hexdigest().lower() != str(meta["source_sha256"]).lower():
        raise ProductSmokeError("fixture_sha256_mismatch")
    if len(data) < 10_000:
        raise ProductSmokeError("fixture_too_small")
    return data


def cached_fixture(meta: dict[str, Any], data_root: Path) -> bytes | None:
    database = data_root / "street-story.sqlite3"
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT photo_path FROM stories WHERE photo_sha256=? ORDER BY created_at DESC LIMIT 1",
                (str(meta["source_sha256"]).lower(),),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ProductSmokeError("fixture_cache_lookup_failed") from exc
    if not row:
        return None
    path = Path(str(row[0]))
    if not path.is_file():
        raise ProductSmokeError("fixture_cache_path_missing")
    return validate_fixture_bytes(meta, path.read_bytes())


def load_fixture(
    meta: dict[str, Any],
    data_root: Path,
    *,
    metadata_path: Path,
) -> tuple[bytes, str]:
    source_file = str(meta.get("source_file") or "").strip()
    if source_file:
        candidate = (metadata_path.parent / source_file).resolve()
        if candidate.parent != metadata_path.parent.resolve() or not candidate.is_file():
            raise ProductSmokeError("fixture_local_source_invalid")
        return validate_fixture_bytes(meta, candidate.read_bytes()), "repository_fixture"
    cached = cached_fixture(meta, data_root)
    if cached is not None:
        return cached, "production_cache"
    try:
        response = httpx.get(
            str(meta["download_url"]),
            headers={"User-Agent": "StreetStory-Product-Canary/1"},
            timeout=40,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ProductSmokeError("fixture_download_unavailable") from exc
    return validate_fixture_bytes(meta, response.content), "wikimedia"


def publication_schedule(
    *,
    keep_publication: bool,
    delay_minutes: int,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    if keep_publication:
        if not 2 <= delay_minutes <= 60:
            raise ProductSmokeError("publication_delay_invalid")
        return (current + timedelta(minutes=delay_minutes)).replace(second=0, microsecond=0)
    return (current + timedelta(hours=25)).replace(second=0, microsecond=0)



def event_error(event: dict[str, Any]) -> str | None:
    kind = str(event.get("type") or "")
    if kind == "resource_fallback":
        return "central_authority_fell_back"
    if kind == "error":
        return str(event.get("code") or "live_provider_error")
    return None


def heartbeat_events(
    client: httpx.Client,
    story_id: str,
    session_id: str,
    cursor: int,
) -> int:
    payload = _json(
        client.get(
            f"/v1/stories/{story_id}/live-sessions/{session_id}/events",
            params={"after": cursor},
        ),
        "live_heartbeat",
    )
    batch = payload.get("events") if isinstance(payload.get("events"), list) else []
    for event in batch:
        if not isinstance(event, dict):
            continue
        fatal = event_error(event)
        if fatal:
            raise ProductSmokeError(fatal)
        if event.get("type") == "closed":
            raise ProductSmokeError("live_session_closed")
    return int(payload.get("cursor") or cursor)


def poll_events(
    client: httpx.Client,
    story_id: str,
    session_id: str,
    cursor: int,
    *,
    expected_tool: str,
    timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    require_confirmation: bool = False,
) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    tool_ok = False
    tool_error: str | None = None
    recoverable_tool_errors: list[str] = []
    post_tool_output = False
    post_tool_turn_complete = False
    turn_complete_seen = False
    confirmation: dict[str, Any] | None = None
    event_types: set[str] = set()
    capability_unavailable: set[str] = set()

    while time.monotonic() < deadline:
        payload = _json(
            client.get(
                f"/v1/stories/{story_id}/live-sessions/{session_id}/events",
                params={"after": cursor},
            ),
            "live_events",
        )
        batch = payload.get("events") if isinstance(payload.get("events"), list) else []
        for event in batch:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            if kind:
                event_types.add(kind)
            fatal = event_error(event)
            if fatal:
                raise ProductSmokeError(fatal)
            if kind == "capability_unavailable":
                capability = str(event.get("capability") or "")
                if capability:
                    capability_unavailable.add(capability)
            if kind == "tool_result" and event.get("name") == expected_tool:
                if event.get("status") == "ok":
                    tool_ok = True
                else:
                    code = str(event.get("code") or "tool_error")
                    if expected_tool == "edit_text" and code == "live_text_revision_conflict":
                        recoverable_tool_errors.append(code)
                    else:
                        tool_error = code
            if kind == "publication_confirmation" and isinstance(event.get("confirmation_id"), str):
                confirmation = {
                    key: event.get(key)
                    for key in (
                        "confirmation_id",
                        "text_revision",
                        "visual_revision",
                        "asset_ref",
                        "image_url",
                        "destinations",
                        "scheduled_for",
                        "timezone",
                        "state",
                    )
                }
            if tool_ok and kind == "output_transcript" and str(event.get("text") or "").strip():
                post_tool_output = True
            if kind == "turn_complete":
                turn_complete_seen = True
                if tool_ok:
                    post_tool_turn_complete = True
        cursor = int(payload.get("cursor") or cursor)

        if tool_error:
            raise ProductSmokeError(f"{expected_tool}_failed_{tool_error}")
        if turn_complete_seen and recoverable_tool_errors and not tool_ok:
            raise ProductSmokeError(f"{expected_tool}_recoverable_conflict_unresolved")
        if tool_ok and post_tool_turn_complete and (confirmation is not None or not require_confirmation):
            return cursor, {
                "tool": expected_tool,
                "tool_ok": True,
                "post_tool_output": post_tool_output,
                "turn_complete": True,
                "confirmation": confirmation,
                "event_types": sorted(event_types),
                "capability_unavailable": sorted(capability_unavailable),
                "recoverable_tool_errors": recoverable_tool_errors,
            }
        time.sleep(POLL_SECONDS)

    raise ProductSmokeError(f"{expected_tool}_turn_timeout")


def send_tool_turn(
    client: httpx.Client,
    story_id: str,
    session_id: str,
    cursor: int,
    *,
    expected_tool: str,
    text: str,
    timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    require_confirmation: bool = False,
) -> tuple[int, dict[str, Any]]:
    _json(
        client.post(
            f"/v1/stories/{story_id}/live-sessions/{session_id}/input",
            json={"text": text},
        ),
        f"{expected_tool}_input",
    )
    return poll_events(
        client,
        story_id,
        session_id,
        cursor,
        expected_tool=expected_tool,
        timeout_seconds=timeout_seconds,
        require_confirmation=require_confirmation,
    )


def story(client: httpx.Client, story_id: str) -> dict[str, Any]:
    return _json(client.get(f"/v1/stories/{story_id}"), "story_readback")


def supported_fact_ids(current: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for fact in current.get("facts", []) if isinstance(current.get("facts"), list) else []:
        if not isinstance(fact, dict) or fact.get("evidence_supported") is not True:
            continue
        sources = fact.get("sources") if isinstance(fact.get("sources"), list) else []
        if not any(
            isinstance(source, dict) and str(source.get("url") or "").startswith("https://")
            for source in sources
        ):
            continue
        fact_id = str(fact.get("fact_id") or "")
        if fact_id:
            output.append(fact_id)
    return output


def wait_visual(
    client: httpx.Client,
    story_id: str,
    session_id: str,
    cursor: int,
) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + VISUAL_TIMEOUT_SECONDS
    last_state = ""
    while time.monotonic() < deadline:
        current = story(client, story_id)
        state = str(current.get("state") or "")
        last_state = state
        if state == "ready_to_publish":
            return current, cursor
        if state in {"needs_review", "visual_blocked"}:
            error = current.get("error") if isinstance(current.get("error"), dict) else {}
            raise ProductSmokeError(f"visual_{state}_{error.get('code') or 'unknown'}")
        cursor = heartbeat_events(client, story_id, session_id, cursor)
        time.sleep(2.0)
    raise ProductSmokeError(f"visual_timeout_{last_state or 'unknown'}")


def wait_publication(
    client: httpx.Client,
    story_id: str,
    session_id: str,
    cursor: int,
    expected_states: set[str],
    *,
    timeout_seconds: float = SOCIAL_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout_seconds
    last_state = ""
    while time.monotonic() < deadline:
        current = story(client, story_id)
        publication = current.get("publication") if isinstance(current.get("publication"), dict) else {}
        last_state = str(publication.get("state") or "")
        if last_state in expected_states:
            return current, cursor
        error = current.get("error") if isinstance(current.get("error"), dict) else {}
        if str(current.get("state") or "") == "needs_review" and error.get("code"):
            raise ProductSmokeError(f"publication_needs_review_{error.get('code')}")
        cursor = heartbeat_events(client, story_id, session_id, cursor)
        time.sleep(2.0)
    raise ProductSmokeError(f"publication_timeout_{last_state or 'unknown'}")


def _is_explicit_test_destination(alias: str, label: str) -> bool:
    normalized = f"{alias} {label}".lower()
    return any(marker in normalized for marker in TEST_DESTINATION_MARKERS)


def telegram_destination(
    client: httpx.Client,
    *,
    requested: str | None = None,
    require_test: bool = False,
) -> str:
    payload = _json(client.get("/v1/capabilities"), "capabilities")
    rows = payload.get("destinations") if isinstance(payload.get("destinations"), list) else []
    candidates = [
        {
            "alias": str(row.get("alias") or ""),
            "label": str(row.get("label") or ""),
            "status": str(row.get("status") or ""),
        }
        for row in rows
        if isinstance(row, dict)
        and str(row.get("provider") or "").lower() == "telegram"
        and str(row.get("status") or "") in {"supported", "needs_review"}
        and str(row.get("alias") or "")
    ]

    if require_test and not requested:
        raise ProductSmokeError("publication_destination_required")

    if requested:
        matches = [row for row in candidates if row["alias"] == requested]
        if len(matches) != 1:
            raise ProductSmokeError("publication_destination_unavailable")
        selected = matches[0]
        if require_test and not _is_explicit_test_destination(
            selected["alias"], selected["label"]
        ):
            raise ProductSmokeError("publication_destination_not_test_safe")
        return selected["alias"]

    supported = [row["alias"] for row in candidates if row["status"] == "supported"]
    reviewable = [row["alias"] for row in candidates if row["status"] == "needs_review"]
    if "lovekenig_tg" in supported:
        return "lovekenig_tg"
    if supported:
        return supported[0]
    if "lovekenig_tg" in reviewable:
        return "lovekenig_tg"
    if reviewable:
        return reviewable[0]
    raise ProductSmokeError("telegram_destination_missing")


def validate_visual(client: httpx.Client, current: dict[str, Any], draft_before: str) -> dict[str, Any]:
    if str(current.get("draft_text") or "") != draft_before:
        raise ProductSmokeError("visual_changed_text")
    visual = current.get("visual") if isinstance(current.get("visual"), dict) else {}
    if str(visual.get("prompt_sha256") or "").lower() != OWNER_PROMPT_SHA256:
        raise ProductSmokeError("owner_prompt_hash_mismatch")
    required = (
        "source_asset_ref",
        "operation_id",
        "selected_asset_ref",
        "selected_sha256",
        "prompt_version",
        "prompt_sha256",
        "content_revision",
    )
    missing = [name for name in required if not visual.get(name)]
    if missing:
        raise ProductSmokeError("visual_receipt_incomplete")
    selected_sha = str(visual["selected_sha256"]).lower()
    image_url = str(current.get("processed_image_url") or "")
    if not image_url:
        raise ProductSmokeError("processed_image_url_missing")
    image = client.get(image_url)
    if image.status_code != 200:
        raise ProductSmokeError(f"processed_image_http_{image.status_code}")
    actual_sha = hashlib.sha256(image.content).hexdigest()
    header_sha = str(image.headers.get("x-content-sha256") or "").lower()
    if actual_sha != selected_sha or header_sha != selected_sha:
        raise ProductSmokeError("processed_asset_hash_mismatch")
    return {
        "operation_id": visual["operation_id"],
        "content_revision": visual["content_revision"],
        "selected_asset_ref": visual["selected_asset_ref"],
        "selected_sha256": selected_sha,
        "image_bytes": len(image.content),
    }


def write_receipt(receipt: dict[str, Any]) -> None:
    path = repo_root() / "backend" / "live-e2e-artifacts" / DIAGNOSTIC_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(
    expected_sha: str,
    *,
    execute_publication: bool = False,
    publication_destination: str | None = None,
    keep_publication: bool = False,
    publication_delay_minutes: int = 5,
    fixture_json: Path | None = None,
) -> dict[str, Any]:
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise ProductSmokeError("expected_sha_invalid")
    if keep_publication and not execute_publication:
        raise ProductSmokeError("keep_publication_requires_execution")

    installer = load_installer(repo_root())
    token = existing_device_token(installer)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StreetStory-Live-Product-Canary/1",
    }
    meta, metadata_path = fixture_meta(fixture_json)
    photo, fixture_source = load_fixture(meta, installer.DATA_ROOT, metadata_path=metadata_path)
    photo_sha = hashlib.sha256(photo).hexdigest()
    session_id = ""
    stopped = False
    publication_scheduled = False
    cancel_confirmed = False

    with httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(connect=10, read=50, write=50, pool=10),
        follow_redirects=False,
    ) as client:
        health = _json(client.get("/healthz"), "health")
        if health.get("ok") is not True or str(health.get("source_sha") or "") != expected_sha:
            raise ProductSmokeError("public_source_sha_mismatch")

        publication_test_destination: str | None = None
        if execute_publication:
            publication_test_destination = telegram_destination(
                client,
                requested=publication_destination,
                require_test=True,
            )

        tag = uuid.uuid4().hex[:12]
        created = _json(
            client.post(
                "/v1/stories",
                files={
                    "photo": (
                        str(meta.get("filename") or "street-story-fixture.jpg"),
                        photo,
                        str(meta.get("mime_type") or "image/jpeg"),
                    )
                },
                data={
                    "client_story_id": f"live-product-{tag}",
                    "photo_sha256": photo_sha,
                    "voice_protocol": "voice-chunks-v2",
                    "lat": str(meta["latitude"]),
                    "lon": str(meta["longitude"]),
                },
                headers={
                    **headers,
                    "Idempotency-Key": f"live-product-create-{tag}",
                    "X-Photo-SHA256": photo_sha,
                },
            ),
            "create_story",
        )
        story_id = str(created.get("id") or "")
        if not story_id:
            raise ProductSmokeError("story_id_missing")

        try:
            started = _json(client.post(f"/v1/stories/{story_id}/live-sessions"), "live_start")
            session_id = str(started.get("session_id") or "")
            if not session_id or started.get("model") != MODEL:
                raise ProductSmokeError("live_start_invalid")

            initial = _json(
                client.get(f"/v1/stories/{story_id}/live-sessions/{session_id}/events", params={"after": 0}),
                "initial_events",
            )
            initial_events = initial.get("events") if isinstance(initial.get("events"), list) else []
            for event in initial_events:
                if isinstance(event, dict):
                    fatal = event_error(event)
                    if fatal:
                        raise ProductSmokeError(fatal)
            cursor = int(initial.get("cursor") or 0)

            cursor, search_turn = send_tool_turn(
                client,
                story_id,
                session_id,
                cursor,
                expected_tool="search_web",
                text=(
                    str(meta.get("search_context_ru") or f"Это {meta['expected_object']} в Калининграде.") + " "
                    "Найди несколько проверяемых исторических фактов и реальные источники в интернете. "
                    "Используй функцию search_web, чтобы факты и URL были сохранены в текущей теме. "
                    "Не редактируй текст и не запускай визуал."
                ),
            )
            after_search = story(client, story_id)
            fact_ids = supported_fact_ids(after_search)
            if not fact_ids or int(after_search.get("source_count") or 0) < 1:
                raise ProductSmokeError("search_evidence_missing")
            if str(after_search.get("state") or "") == "researching":
                raise ProductSmokeError("legacy_research_path_used")

            cursor, edit_turn = send_tool_turn(
                client,
                story_id,
                session_id,
                cursor,
                expected_tool="edit_text",
                text=(
                    "Теперь подготовь короткий ясный текст публикации на русском языке, максимум 700 знаков. "
                    "Используй только факты, которые уже сохранены в теме с источниками. Обязательно вызови "
                    "edit_text. Изображение пока не меняй."
                ),
            )
            after_edit = story(client, story_id)
            draft = str(after_edit.get("draft_text") or "").strip()
            if not draft or len(draft) > 1024:
                raise ProductSmokeError("draft_text_invalid")

            cursor, visual_turn = send_tool_turn(
                client,
                story_id,
                session_id,
                cursor,
                expected_tool="generate_visual",
                text=(
                    "Текст оставь без изменений. Теперь создай визуал для этой публикации через generate_visual, "
                    "используя сохранённые подтверждённые факты. Не редактируй текст."
                ),
            )
            ready, cursor = wait_visual(client, story_id, session_id, cursor)
            visual_receipt = validate_visual(client, ready, draft)

            destination = publication_test_destination or telegram_destination(client)
            scheduled_for = publication_schedule(
                keep_publication=keep_publication,
                delay_minutes=publication_delay_minutes,
            ).isoformat()
            cursor, publish_turn = send_tool_turn(
                client,
                story_id,
                session_id,
                cursor,
                expected_tool="prepare_publication",
                require_confirmation=True,
                text=(
                    "Подготовь точную карточку подтверждения публикации, но ничего не публикуй и не подтверждай. "
                    f"Используй prepare_publication: destination {destination}, scheduled_for {scheduled_for}, "
                    "timezone UTC."
                ),
            )
            confirmation = publish_turn.get("confirmation")
            if not isinstance(confirmation, dict) or confirmation.get("state") != "prepared":
                raise ProductSmokeError("publication_confirmation_missing")
            if confirmation.get("destinations") != [destination]:
                raise ProductSmokeError("publication_confirmation_destination_mismatch")

            final_story = story(client, story_id)
            publication = final_story.get("publication") if isinstance(final_story.get("publication"), dict) else {}
            if publication.get("state") in {"scheduled", "verified", "published"}:
                raise ProductSmokeError("publication_dispatched_unexpectedly")

            publication_execution: dict[str, Any] | None = None
            if execute_publication:
                confirmation_id = str(confirmation.get("confirmation_id") or "")
                cursor, confirm_turn = send_tool_turn(
                    client,
                    story_id,
                    session_id,
                    cursor,
                    expected_tool="confirm_publication",
                    text=(
                        "Я явно подтверждаю именно показанную карточку публикации. "
                        f"Вызови confirm_publication с confirmation_id {confirmation_id}. "
                        "Не меняй текст, картинку, канал или время."
                    ),
                )
                scheduled_story, cursor = wait_publication(
                    client,
                    story_id,
                    session_id,
                    cursor,
                    {"scheduled", "verified"},
                )
                scheduled_publication = (
                    scheduled_story.get("publication")
                    if isinstance(scheduled_story.get("publication"), dict)
                    else {}
                )
                publication_id = str(scheduled_publication.get("publication_id") or "")
                publish_operation_id = str(scheduled_publication.get("operation_id") or "")
                if not publication_id or not publish_operation_id:
                    raise ProductSmokeError("scheduled_publication_receipt_incomplete")
                publication_scheduled = True

                if keep_publication:
                    publication_execution = {
                        "confirm_tool_ok": confirm_turn["tool_ok"],
                        "cancel_tool_ok": None,
                        "publication_id": publication_id,
                        "publish_operation_id": publish_operation_id,
                        "cancel_operation_id": None,
                        "scheduled_for": scheduled_story.get("scheduled_for"),
                        "final_state": str(scheduled_publication.get("state") or "scheduled"),
                        "kept_for_owner_readback": True,
                    }
                else:
                    cursor, cancel_turn = send_tool_turn(
                        client,
                        story_id,
                        session_id,
                        cursor,
                        expected_tool="cancel_publication",
                        text=(
                            "Отмени текущую запланированную публикацию через cancel_publication. "
                            "Не создавай новую публикацию и ничего больше не меняй."
                        ),
                    )
                    cancelled_story, cursor = wait_publication(
                        client,
                        story_id,
                        session_id,
                        cursor,
                        {"cancelled"},
                    )
                    cancelled_publication = (
                        cancelled_story.get("publication")
                        if isinstance(cancelled_story.get("publication"), dict)
                        else {}
                    )
                    cancel_operation_id = str(cancelled_publication.get("cancel_operation_id") or "")
                    if not cancel_operation_id:
                        raise ProductSmokeError("cancel_operation_id_missing")
                    cancel_confirmed = True
                    publication_execution = {
                        "confirm_tool_ok": confirm_turn["tool_ok"],
                        "cancel_tool_ok": cancel_turn["tool_ok"],
                        "publication_id": publication_id,
                        "publish_operation_id": publish_operation_id,
                        "cancel_operation_id": cancel_operation_id,
                        "scheduled_for": scheduled_story.get("scheduled_for"),
                        "final_state": "cancelled",
                        "kept_for_owner_readback": False,
                    }

            stop = _json(
                client.post(f"/v1/stories/{story_id}/live-sessions/{session_id}/stop"),
                "live_stop",
            )
            stopped = stop.get("ok") is True
            if not stopped:
                raise ProductSmokeError("live_stop_unconfirmed")

            receipt = {
                "status": "PASS",
                "source_sha": expected_sha,
                "live_model": MODEL,
                "same_live_session": True,
                "central_authority_fallback": False,
                "fixture": {
                    "object": meta["expected_object"],
                    "photo_sha256": photo_sha,
                    "reference_sha256": meta.get("reference_sha256"),
                    "fixture_id": meta.get("fixture_id"),
                    "license": meta.get("license"),
                    "source": fixture_source,
                },
                "search": {
                    "tool_ok": search_turn["tool_ok"],
                    "fact_count": len(after_search.get("facts") or []),
                    "supported_fact_count": len(fact_ids),
                    "source_count": int(after_search.get("source_count") or 0),
                    "capability_unavailable": search_turn["capability_unavailable"],
                },
                "text": {
                    "tool_ok": edit_turn["tool_ok"],
                    "draft_chars": len(draft),
                    "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                    "recoverable_tool_errors": edit_turn["recoverable_tool_errors"],
                },
                "visual": {
                    "tool_ok": visual_turn["tool_ok"],
                    **visual_receipt,
                },
                "publication_confirmation": {
                    "tool_ok": publish_turn["tool_ok"],
                    "confirmation_id": confirmation.get("confirmation_id"),
                    "destination": destination,
                    "scheduled_for": confirmation.get("scheduled_for"),
                    "state": confirmation.get("state"),
                    "publication_dispatched": execute_publication,
                    "keep_publication": keep_publication,
                },
                "publication_execution": publication_execution,
                "session_stopped": True,
                "secrets_disclosed": False,
            }
            write_receipt(receipt)
            return receipt
        finally:
            if execute_publication and publication_scheduled and not cancel_confirmed and not keep_publication:
                try:
                    _json(
                        client.post(
                            f"/v1/stories/{story_id}/cancel",
                            json={},
                            headers={
                                **headers,
                                "Idempotency-Key": f"live-product-cleanup-{tag}",
                            },
                        ),
                        "cleanup_cancel",
                    )
                    cleanup_deadline = time.monotonic() + SOCIAL_TIMEOUT_SECONDS
                    while time.monotonic() < cleanup_deadline:
                        cleanup_story = story(client, story_id)
                        cleanup_publication = (
                            cleanup_story.get("publication")
                            if isinstance(cleanup_story.get("publication"), dict)
                            else {}
                        )
                        if cleanup_publication.get("state") == "cancelled":
                            break
                        time.sleep(2.0)
                except Exception:
                    pass
            if session_id and not stopped:
                try:
                    client.post(
                        f"/v1/stories/{story_id}/live-sessions/{session_id}/stop",
                        timeout=5,
                    )
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--execute-publication",
        action="store_true",
        help="Actually schedule and cancel the canary through the same Live session.",
    )
    parser.add_argument(
        "--publication-destination",
        help="Explicit Telegram test/safe/e2e alias. Required with --execute-publication.",
    )
    parser.add_argument(
        "--keep-publication",
        action="store_true",
        help="Owner acceptance only: keep the scheduled test publication for provider readback.",
    )
    parser.add_argument(
        "--publication-delay-minutes",
        type=int,
        default=5,
        help="Delay for --keep-publication, 2..60 minutes.",
    )
    parser.add_argument(
        "--fixture-json",
        help="Repository-relative fixture metadata JSON. Defaults to golden_fixture.json.",
    )
    args = parser.parse_args()
    try:
        selected_fixture = fixture_path(args.fixture_json)
        receipt = run(
            args.expected_sha,
            execute_publication=args.execute_publication,
            publication_destination=args.publication_destination,
            keep_publication=args.keep_publication,
            publication_delay_minutes=args.publication_delay_minutes,
            fixture_json=selected_fixture,
        )
    except ProductSmokeError as exc:
        receipt = {"status": "FAIL", "error": str(exc), "secrets_disclosed": False}
        write_receipt(receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 1
    except Exception as exc:
        receipt = {
            "status": "FAIL",
            "error": f"unexpected_{type(exc).__name__}",
            "secrets_disclosed": False,
        }
        write_receipt(receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
