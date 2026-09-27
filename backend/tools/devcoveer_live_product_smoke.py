#!/usr/bin/env python3
"""Production acceptance for the current Street Story Live product path.

The canary creates one real-photo topic and keeps one Gemini 3.8 Live session
through search, text editing, visual generation and publication confirmation.
It never confirms or dispatches a publication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import time
import uuid
from typing import Any

import httpx

BASE_URL = "https://street-story.kenigevents.ru"
MODEL = "gemini-3.8-live"
POLL_SECONDS = 0.5
TURN_TIMEOUT_SECONDS = 120
VISUAL_TIMEOUT_SECONDS = 12 * 60
OWNER_PROMPT_SHA256 = "4eab6d0cfcafc84881cad86380baa9920785b7e18e9a934923966995802380a3"
DIAGNOSTIC_NAME = "diagnostic-devcoveer-live-product.json"


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


def fixture_meta() -> dict[str, Any]:
    path = Path(__file__).with_name("golden_fixture.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductSmokeError("fixture_metadata_invalid") from exc
    required = ("download_url", "source_sha1", "latitude", "longitude", "expected_object")
    if not isinstance(payload, dict) or any(not payload.get(name) for name in required):
        raise ProductSmokeError("fixture_metadata_incomplete")
    return payload


def download_fixture(meta: dict[str, Any]) -> bytes:
    response = httpx.get(
        str(meta["download_url"]),
        headers={"User-Agent": "StreetStory-Product-Canary/1"},
        timeout=40,
        follow_redirects=True,
    )
    response.raise_for_status()
    data = response.content
    if hashlib.sha1(data).hexdigest().lower() != str(meta["source_sha1"]).lower():
        raise ProductSmokeError("fixture_sha1_mismatch")
    if len(data) < 10_000:
        raise ProductSmokeError("fixture_too_small")
    return data


def event_error(event: dict[str, Any]) -> str | None:
    kind = str(event.get("type") or "")
    if kind == "resource_fallback":
        return "central_authority_fell_back"
    if kind == "error":
        return str(event.get("code") or "live_provider_error")
    return None


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
    post_tool_output = False
    post_tool_turn_complete = False
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
                    tool_error = str(event.get("code") or "tool_error")
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
            if tool_ok and kind == "turn_complete":
                post_tool_turn_complete = True
        cursor = int(payload.get("cursor") or cursor)

        if tool_error:
            raise ProductSmokeError(f"{expected_tool}_failed_{tool_error}")
        if tool_ok and post_tool_turn_complete and (confirmation is not None or not require_confirmation):
            return cursor, {
                "tool": expected_tool,
                "tool_ok": True,
                "post_tool_output": post_tool_output,
                "turn_complete": True,
                "confirmation": confirmation,
                "event_types": sorted(event_types),
                "capability_unavailable": sorted(capability_unavailable),
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


def wait_visual(client: httpx.Client, story_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + VISUAL_TIMEOUT_SECONDS
    last_state = ""
    while time.monotonic() < deadline:
        current = story(client, story_id)
        state = str(current.get("state") or "")
        last_state = state
        if state == "ready_to_publish":
            return current
        if state in {"needs_review", "visual_blocked"}:
            error = current.get("error") if isinstance(current.get("error"), dict) else {}
            raise ProductSmokeError(f"visual_{state}_{error.get('code') or 'unknown'}")
        time.sleep(2.0)
    raise ProductSmokeError(f"visual_timeout_{last_state or 'unknown'}")


def telegram_destination(client: httpx.Client) -> str:
    payload = _json(client.get("/v1/capabilities"), "capabilities")
    rows = payload.get("destinations") if isinstance(payload.get("destinations"), list) else []
    supported = [
        str(row.get("alias") or "")
        for row in rows
        if isinstance(row, dict)
        and str(row.get("provider") or "").lower() == "telegram"
        and str(row.get("status") or "") == "supported"
        and str(row.get("alias") or "")
    ]
    if "lovekenig_tg" in supported:
        return "lovekenig_tg"
    if supported:
        return supported[0]
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


def run(expected_sha: str) -> dict[str, Any]:
    if len(expected_sha) != 40 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise ProductSmokeError("expected_sha_invalid")

    installer = load_installer(repo_root())
    token = existing_device_token(installer)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StreetStory-Live-Product-Canary/1",
    }
    meta = fixture_meta()
    photo = download_fixture(meta)
    photo_sha = hashlib.sha256(photo).hexdigest()
    session_id = ""
    stopped = False

    with httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        timeout=httpx.Timeout(connect=10, read=50, write=50, pool=10),
        follow_redirects=False,
    ) as client:
        health = _json(client.get("/healthz"), "health")
        if health.get("ok") is not True or str(health.get("source_sha") or "") != expected_sha:
            raise ProductSmokeError("public_source_sha_mismatch")

        tag = uuid.uuid4().hex[:12]
        created = _json(
            client.post(
                "/v1/stories",
                files={"photo": ("brandenburg-gate.jpg", photo, "image/jpeg")},
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
            cursor = int(initial.get("cursor") or 0)

            cursor, search_turn = send_tool_turn(
                client,
                story_id,
                session_id,
                cursor,
                expected_tool="search_web",
                text=(
                    "Это Бранденбургские ворота в Калининграде. Найди несколько проверяемых исторических "
                    "фактов и реальные источники. Используй функцию search_web, чтобы факты и URL были "
                    "сохранены в текущей теме. Не редактируй текст и не запускай визуал."
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
            ready = wait_visual(client, story_id)
            visual_receipt = validate_visual(client, ready, draft)

            destination = telegram_destination(client)
            scheduled_for = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).replace(microsecond=0).isoformat()
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
                    "license": meta.get("license"),
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
                    "publication_dispatched": False,
                },
                "session_stopped": True,
                "secrets_disclosed": False,
            }
            write_receipt(receipt)
            return receipt
        finally:
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
    args = parser.parse_args()
    try:
        receipt = run(args.expected_sha)
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
