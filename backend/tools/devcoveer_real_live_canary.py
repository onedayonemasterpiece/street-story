#!/usr/bin/env python3
"""Real Gemini Live + ai-resource-control production canary for Street Story.

The canary creates one isolated story, opens the actual /live-sessions endpoint,
waits for the Gemini Live provider to become ready, sends one text turn, observes
provider output, and closes the session. A run that uses the emergency
resource_fallback is intentionally NOT accepted as central-authority evidence.
No provider key or device token is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import time
import uuid
import zlib
from typing import Any

import httpx

BASE_URL = "https://street-story.kenigevents.ru"
EVENT_TIMEOUT_SECONDS = 45
POLL_SECONDS = 0.5
DIAGNOSTIC_NAME = "diagnostic-devcoveer-real-live.json"


class LiveCanaryError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_installer(root: Path):
    path = root / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    if spec is None or spec.loader is None:
        raise LiveCanaryError("installer_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_device_token(installer) -> str:
    path = installer.DEVICE_TOKEN_FILE
    if not path.exists():
        raise LiveCanaryError("device_token_missing")
    installer.require_mode(path, 0o600)
    token, created = installer.device_token()
    if created:
        raise LiveCanaryError("device_token_unexpectedly_created")
    if not isinstance(token, str) or len(token) < 32:
        raise LiveCanaryError("device_token_invalid")
    return token


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def tiny_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\x20\x40\x60"
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def _json(response: httpx.Response, *, code: str) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise LiveCanaryError(f"{code}_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveCanaryError(f"{code}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise LiveCanaryError(f"{code}_invalid_shape")
    return payload


def event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    types = [str(item.get("type") or "") for item in events if isinstance(item, dict)]
    fallback = any(kind == "resource_fallback" for kind in types)
    errors = [
        str(item.get("code") or "provider_error")
        for item in events
        if isinstance(item, dict) and item.get("type") == "error"
    ]
    output_transcripts = [
        str(item.get("text") or "").strip()
        for item in events
        if isinstance(item, dict) and item.get("type") == "output_transcript"
        and str(item.get("text") or "").strip()
    ]
    audio_seen = any(kind == "audio" for kind in types)
    ready_seen = any(kind == "ready" for kind in types)
    turn_complete = any(kind == "turn_complete" for kind in types)
    return {
        "ready": ready_seen,
        "resource_fallback": fallback,
        "errors": errors[:4],
        "output_transcript_seen": bool(output_transcripts),
        "output_transcript_chars": sum(len(value) for value in output_transcripts),
        "audio_seen": audio_seen,
        "turn_complete": turn_complete,
        "event_types": sorted(set(kind for kind in types if kind))[:32],
    }


def validate_central_live(summary: dict[str, Any]) -> None:
    if summary.get("resource_fallback") is True:
        raise LiveCanaryError("central_authority_fell_back")
    if summary.get("errors"):
        raise LiveCanaryError("live_provider_error")
    if summary.get("ready") is not True:
        raise LiveCanaryError("live_ready_missing")
    if not (summary.get("output_transcript_seen") or summary.get("audio_seen")):
        raise LiveCanaryError("live_output_missing")
    if summary.get("turn_complete") is not True:
        raise LiveCanaryError("live_turn_incomplete")


def write_receipt(receipt: dict[str, Any]) -> None:
    path = repo_root() / "backend" / "live-e2e-artifacts" / DIAGNOSTIC_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(expected_sha: str) -> dict[str, Any]:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise LiveCanaryError("expected_sha_invalid")

    installer = load_installer(repo_root())
    token = existing_device_token(installer)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StreetStory-Real-Live-Canary/1",
    }
    story_id = ""
    session_id = ""
    events: list[dict[str, Any]] = []
    stopped = False

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=50, follow_redirects=False) as client:
        health = _json(client.get("/healthz"), code="health")
        if health.get("ok") is not True or str(health.get("source_sha") or "") != expected_sha:
            raise LiveCanaryError("public_source_sha_mismatch")

        photo = tiny_png()
        photo_sha = hashlib.sha256(photo).hexdigest()
        run_tag = uuid.uuid4().hex[:12]
        create = _json(
            client.post(
                "/v1/stories",
                files={"photo": ("live-canary.png", photo, "image/png")},
                data={
                    "client_story_id": f"real-live-canary-{run_tag}",
                    "photo_sha256": photo_sha,
                    "voice_protocol": "voice-chunks-v2",
                    "lat": "54.7104",
                    "lon": "20.4522",
                },
                headers={
                    **headers,
                    "Idempotency-Key": f"real-live-canary-create-{run_tag}",
                    "X-Photo-SHA256": photo_sha,
                },
            ),
            code="create_story",
        )
        story_id = str(create.get("id") or "")
        if not story_id:
            raise LiveCanaryError("story_id_missing")

        try:
            started = _json(
                client.post(f"/v1/stories/{story_id}/live-sessions"),
                code="live_start",
            )
            session_id = str(started.get("session_id") or "")
            if not session_id or started.get("model") != "gemini-3.8-live":
                raise LiveCanaryError("live_start_receipt_invalid")

            initial = _json(
                client.get(f"/v1/stories/{story_id}/live-sessions/{session_id}/events", params={"after": 0}),
                code="live_events_initial",
            )
            batch = initial.get("events") if isinstance(initial.get("events"), list) else []
            events.extend(item for item in batch if isinstance(item, dict))
            cursor = int(initial.get("cursor") or 0)

            _json(
                client.post(
                    f"/v1/stories/{story_id}/live-sessions/{session_id}/input",
                    json={
                        "text": (
                            "Это проверка связи. Не вызывай инструменты. "
                            "Ответь очень коротко по-русски: «Готово»."
                        )
                    },
                ),
                code="live_input",
            )

            deadline = time.monotonic() + EVENT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                payload = _json(
                    client.get(
                        f"/v1/stories/{story_id}/live-sessions/{session_id}/events",
                        params={"after": cursor},
                    ),
                    code="live_events",
                )
                batch = payload.get("events") if isinstance(payload.get("events"), list) else []
                for item in batch:
                    if isinstance(item, dict):
                        events.append(item)
                cursor = int(payload.get("cursor") or cursor)
                summary = event_summary(events)
                if summary["resource_fallback"] or summary["errors"]:
                    break
                if (
                    summary["ready"]
                    and summary["turn_complete"]
                    and (summary["output_transcript_seen"] or summary["audio_seen"])
                ):
                    break
                time.sleep(POLL_SECONDS)

            summary = event_summary(events)
            validate_central_live(summary)

            stopped_payload = _json(
                client.post(f"/v1/stories/{story_id}/live-sessions/{session_id}/stop"),
                code="live_stop",
            )
            stopped = stopped_payload.get("ok") is True
            if not stopped:
                raise LiveCanaryError("live_stop_unconfirmed")

            receipt = {
                "status": "PASS",
                "source_sha": expected_sha,
                "model": "gemini-3.8-live",
                "central_authority": True,
                "session_stopped": True,
                "summary": summary,
                "secrets_disclosed": False,
            }
            write_receipt(receipt)
            return receipt
        finally:
            if session_id and not stopped:
                try:
                    client.post(f"/v1/stories/{story_id}/live-sessions/{session_id}/stop", timeout=5)
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        receipt = run(args.expected_sha)
    except LiveCanaryError as exc:
        receipt = {
            "status": "FAIL",
            "error": str(exc),
            "secrets_disclosed": False,
        }
        write_receipt(receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
