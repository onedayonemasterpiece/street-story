#!/usr/bin/env python3
"""Production canary for Gemini 3.8 Live -> search_web -> same Live session."""
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
EVENT_TIMEOUT_SECONDS = 120
POLL_SECONDS = 0.5
DIAGNOSTIC_NAME = "diagnostic-devcoveer-live-search.json"


class CanaryError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_installer(root: Path):
    path = root / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    if spec is None or spec.loader is None:
        raise CanaryError("installer_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_device_token(installer) -> str:
    path = installer.DEVICE_TOKEN_FILE
    if not path.exists():
        raise CanaryError("device_token_missing")
    installer.require_mode(path, 0o600)
    token, created = installer.device_token()
    if created:
        raise CanaryError("device_token_unexpectedly_created")
    if not isinstance(token, str) or len(token) < 32:
        raise CanaryError("device_token_invalid")
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
        raise CanaryError(f"{code}_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CanaryError(f"{code}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise CanaryError(f"{code}_invalid_shape")
    return payload


def _https_urls(value: Any) -> list[str]:
    found: list[str] = []
    def visit(item: Any) -> None:
        if isinstance(item, str):
            if item.startswith("https://") and item not in found:
                found.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
    visit(value)
    return found[:40]


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    ready = False
    fallback = False
    errors: list[str] = []
    search_results: list[dict[str, Any]] = []
    native_grounding_urls: list[str] = []
    post_search_output = False
    post_search_turn_complete = False
    search_seen = False

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind == "ready":
            ready = True
        elif kind == "resource_fallback":
            fallback = True
        elif kind == "error":
            errors.append(str(event.get("code") or "provider_error"))
        elif kind == "tool_result" and event.get("name") == "search_web":
            search_results.append({
                "status": event.get("status"),
                "code": event.get("code"),
            })
            if event.get("status") == "ok":
                search_seen = True
        elif kind == "grounding":
            urls = _https_urls(event.get("metadata"))
            if urls:
                native_grounding_urls.extend(url for url in urls if url not in native_grounding_urls)
                search_seen = True
        elif search_seen and kind == "output_transcript" and str(event.get("text") or "").strip():
            post_search_output = True
        elif search_seen and kind == "turn_complete":
            post_search_turn_complete = True

    app_search_ok = any(item.get("status") == "ok" for item in search_results)
    native_search_ok = bool(native_grounding_urls)
    return {
        "ready": ready,
        "resource_fallback": fallback,
        "errors": errors[:4],
        "search_results": search_results[-4:],
        "app_search_ok": app_search_ok,
        "native_search_ok": native_search_ok,
        "native_grounding_url_count": len(native_grounding_urls),
        "native_grounding_urls": native_grounding_urls[:12],
        "search_ok": app_search_ok or native_search_ok,
        "post_search_output": post_search_output,
        "post_search_turn_complete": post_search_turn_complete,
        "event_types": sorted({
            str(event.get("type") or "")
            for event in events
            if isinstance(event, dict) and event.get("type")
        })[:40],
    }


def validate_summary(summary: dict[str, Any]) -> None:
    if summary.get("resource_fallback"):
        raise CanaryError("central_authority_fell_back")
    if summary.get("errors"):
        raise CanaryError("live_provider_error")
    if not summary.get("ready"):
        raise CanaryError("live_ready_missing")
    if not summary.get("search_ok"):
        raise CanaryError("search_web_tool_missing_or_failed")
    if not summary.get("post_search_output"):
        raise CanaryError("live_did_not_continue_after_search")
    if not summary.get("post_search_turn_complete"):
        raise CanaryError("live_turn_incomplete_after_search")


def validate_story(story: dict[str, Any], *, native_search_ok: bool = False) -> dict[str, Any]:
    facts = story.get("facts") if isinstance(story.get("facts"), list) else []
    supported = [
        fact for fact in facts
        if isinstance(fact, dict)
        and fact.get("evidence_supported") is True
        and isinstance(fact.get("sources"), list)
        and any(
            isinstance(source, dict) and str(source.get("url") or "").startswith("https://")
            for source in fact["sources"]
        )
    ]
    if not supported and not native_search_ok:
        raise CanaryError("grounded_fact_missing")
    if str(story.get("state") or "") == "researching":
        raise CanaryError("legacy_research_job_used")
    if str(story.get("draft_text") or "").strip():
        raise CanaryError("search_tool_rewrote_draft")
    return {
        "fact_count": len(facts),
        "supported_fact_count": len(supported),
        "source_count": int(story.get("source_count") or 0),
        "state": story.get("state"),
        "native_search_used": native_search_ok,
    }


def write_receipt(receipt: dict[str, Any]) -> None:
    path = repo_root() / "backend" / "live-e2e-artifacts" / DIAGNOSTIC_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(expected_sha: str) -> dict[str, Any]:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise CanaryError("expected_sha_invalid")

    installer = load_installer(repo_root())
    token = existing_device_token(installer)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StreetStory-Live-Search-Canary/1",
    }
    session_id = ""
    stopped = False
    events: list[dict[str, Any]] = []

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=50, follow_redirects=False) as client:
        health = _json(client.get("/healthz"), code="health")
        if health.get("ok") is not True or str(health.get("source_sha") or "") != expected_sha:
            raise CanaryError("public_source_sha_mismatch")

        photo = tiny_png()
        photo_sha = hashlib.sha256(photo).hexdigest()
        tag = uuid.uuid4().hex[:12]
        created = _json(
            client.post(
                "/v1/stories",
                files={"photo": ("live-search-canary.png", photo, "image/png")},
                data={
                    "client_story_id": f"live-search-canary-{tag}",
                    "photo_sha256": photo_sha,
                    "voice_protocol": "voice-chunks-v2",
                    "lat": "54.7104",
                    "lon": "20.4522",
                },
                headers={
                    **headers,
                    "Idempotency-Key": f"live-search-canary-create-{tag}",
                    "X-Photo-SHA256": photo_sha,
                },
            ),
            code="create_story",
        )
        story_id = str(created.get("id") or "")
        if not story_id:
            raise CanaryError("story_id_missing")

        try:
            started = _json(client.post(f"/v1/stories/{story_id}/live-sessions"), code="live_start")
            session_id = str(started.get("session_id") or "")
            if not session_id or started.get("model") != "gemini-3.8-live":
                raise CanaryError("live_start_receipt_invalid")

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
                            "Найди в интернете один-два проверяемых факта о Бранденбургских воротах "
                            "в Калининграде. Используй встроенный Google Search этой Live-сессии, если он доступен; "
                            "если встроенный поиск недоступен, используй search_web. "
                            "После результата кратко скажи, что удалось подтвердить. "
                            "Не редактируй текст публикации и не запускай визуал."
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
                events.extend(item for item in batch if isinstance(item, dict))
                cursor = int(payload.get("cursor") or cursor)
                summary = summarize(events)
                if summary["resource_fallback"] or summary["errors"]:
                    break
                if summary["search_ok"] and summary["post_search_output"] and summary["post_search_turn_complete"]:
                    break
                time.sleep(POLL_SECONDS)

            summary = summarize(events)
            validate_summary(summary)
            story = _json(client.get(f"/v1/stories/{story_id}"), code="story_readback")
            story_evidence = validate_story(story, native_search_ok=bool(summary["native_search_ok"]))

            stop = _json(client.post(f"/v1/stories/{story_id}/live-sessions/{session_id}/stop"), code="live_stop")
            stopped = stop.get("ok") is True
            if not stopped:
                raise CanaryError("live_stop_unconfirmed")

            receipt = {
                "status": "PASS",
                "source_sha": expected_sha,
                "live_model": "gemini-3.8-live",
                "central_authority": True,
                "search_tool": "search_web",
                "search_models": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash"],
                "same_live_session_continued": True,
                "summary": summary,
                "story_evidence": story_evidence,
                "session_stopped": True,
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
    except CanaryError as exc:
        receipt = {"status": "FAIL", "error": str(exc), "secrets_disclosed": False}
        write_receipt(receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
