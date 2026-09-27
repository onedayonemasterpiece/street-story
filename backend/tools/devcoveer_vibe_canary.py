#!/usr/bin/env python3
"""Establish Street Story runtime-principal Telegram scheduling capability safely.

The canary schedules one text-only Telegram item far in the future, requires
provider_scheduled readback, cancels that exact tracked publication, requires
provider cancellation readback, then verifies the runtime principal bootstrap
reports Telegram publish supported. No credential value is printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = "https://street-story.kenigevents.ru"
POLL_SECONDS = 0.5
POLL_TIMEOUT_SECONDS = 90


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


def verify_public_health(expected_sha: str) -> None:
    request = urllib.request.Request(
        BASE_URL + "/healthz",
        headers={"Accept": "application/json", "User-Agent": "StreetStory-Vibe-Canary/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise CanaryError("public_health_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise CanaryError("public_health_invalid")
    if str(payload.get("source_sha") or "") != expected_sha:
        raise CanaryError("public_source_sha_mismatch")


def operation_receipt(installer, token: str, operation_id: str) -> dict[str, Any]:
    status = installer.vibe_request(token, "GET", f"/v1/operations/{operation_id}")
    receipts = status.get("receipts")
    if not isinstance(receipts, list):
        raise CanaryError("operation_receipts_missing")
    row = next(
        (
            item
            for item in receipts
            if isinstance(item, dict) and item.get("operation_id") == operation_id
        ),
        None,
    )
    if not isinstance(row, dict):
        raise CanaryError("operation_receipt_missing")
    return row


def wait_complete(installer, token: str, operation_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        row = operation_receipt(installer, token, operation_id)
        if row.get("operation_complete") is True:
            return row
        time.sleep(POLL_SECONDS)
    raise CanaryError("operation_timeout")


def telegram_delivery(row: dict[str, Any], alias: str) -> dict[str, Any]:
    deliveries = row.get("deliveries")
    matches = [
        item
        for item in deliveries
        if isinstance(item, dict)
        and item.get("destination") == alias
        and item.get("provider") == "telegram"
    ] if isinstance(deliveries, list) else []
    if len(matches) != 1:
        raise CanaryError("telegram_delivery_missing")
    return matches[0]


def future_schedule() -> str:
    # Provider owns the queue; keep the canary safely away from immediate delivery.
    local_tz = timezone(timedelta(hours=2))
    return (datetime.now(timezone.utc) + timedelta(days=2)).astimezone(local_tz).replace(
        microsecond=0
    ).isoformat()


def bootstrap_supported(installer, token: str) -> bool:
    bootstrap = installer.vibe_request(token, "GET", "/v1/bootstrap")
    caps = [
        row
        for row in bootstrap.get("capabilities", [])
        if isinstance(row, dict)
        and row.get("destination") == installer.VIBE_ALIAS
        and row.get("operation") == "publish"
        and row.get("surface") == "post"
    ]
    return len(caps) == 1 and caps[0].get("status") == "supported"


def run(expected_deployed_sha: str) -> dict[str, Any]:
    if len(expected_deployed_sha) != 40 or any(
        char not in "0123456789abcdef" for char in expected_deployed_sha
    ):
        raise CanaryError("expected_sha_invalid")
    verify_public_health(expected_deployed_sha)
    installer = load_installer(repo_root())
    _principal, token = installer.ensure_vibe_principal(expected_deployed_sha)

    run_tag = uuid.uuid4().hex[:12]
    publish_key = f"street-story-runtime-canary-{run_tag}"
    cancel_key = f"street-story-runtime-canary-cancel-{run_tag}"
    publication_id: str | None = None
    revision: int | None = None
    cancelled = False
    scheduled_observed = False
    cleanup_error: str | None = None

    try:
        created = installer.vibe_request(
            token,
            "POST",
            "/v1/publications",
            body={
                "to": [installer.VIBE_ALIAS],
                "content": {
                    "text": "Street Story runtime-principal canary; scheduled queue test, cancelled immediately after readback."
                },
                "delivery": {"kind": "at", "at": future_schedule()},
                "mode": "execute",
            },
            request_key=publish_key,
        )
        operation_id = str(created.get("operation_id") or "")
        publication_id = str(created.get("resource_id") or "")
        raw_revision = created.get("revision")
        if not operation_id or not publication_id or not isinstance(raw_revision, int):
            raise CanaryError("publish_receipt_incomplete")
        revision = raw_revision

        scheduled = wait_complete(installer, token, operation_id)
        delivery = telegram_delivery(scheduled, installer.VIBE_ALIAS)
        if (
            str(scheduled.get("state") or "") != "scheduled"
            or str(delivery.get("state") or "") != "scheduled"
            or str(delivery.get("observed") or "") != "provider_scheduled"
        ):
            raise CanaryError("provider_schedule_not_verified")
        scheduled_observed = True

        cancel = installer.vibe_request(
            token,
            "POST",
            f"/v1/publications/{publication_id}/commands",
            body={"expected_revision": revision, "change": {"kind": "cancel"}},
            request_key=cancel_key,
        )
        cancel_operation = str(cancel.get("operation_id") or "")
        if not cancel_operation:
            raise CanaryError("cancel_receipt_incomplete")
        cancelled_row = wait_complete(installer, token, cancel_operation)
        cancel_delivery = telegram_delivery(cancelled_row, installer.VIBE_ALIAS)
        if (
            str(cancelled_row.get("state") or "") != "cancelled"
            or str(cancel_delivery.get("state") or "") != "cancelled"
            or str(cancel_delivery.get("observed") or "") != "cancelled"
        ):
            raise CanaryError("provider_cancel_not_verified")
        cancelled = True

        if not bootstrap_supported(installer, token):
            raise CanaryError("runtime_capability_not_supported")

        return {
            "status": "PASS",
            "deployed_sha": expected_deployed_sha,
            "scheduled_observed": True,
            "cancelled_observed": True,
            "runtime_capability": "supported",
            "secrets_disclosed": False,
        }
    finally:
        if publication_id and revision is not None and not cancelled:
            try:
                cleanup = installer.vibe_request(
                    token,
                    "POST",
                    f"/v1/publications/{publication_id}/commands",
                    body={"expected_revision": revision, "change": {"kind": "cancel"}},
                    request_key=cancel_key,
                )
                cleanup_operation = str(cleanup.get("operation_id") or "")
                if cleanup_operation:
                    row = wait_complete(installer, token, cleanup_operation)
                    delivery = telegram_delivery(row, installer.VIBE_ALIAS)
                    if (
                        str(row.get("state") or "") == "cancelled"
                        and str(delivery.get("observed") or "") == "cancelled"
                    ):
                        cancelled = True
            except Exception as exc:
                cleanup_error = type(exc).__name__
        if scheduled_observed and not cancelled:
            raise CanaryError(
                "canary_cleanup_failed" + (f":{cleanup_error}" if cleanup_error else "")
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-deployed-sha", required=True)
    args = parser.parse_args()
    try:
        receipt = run(args.expected_deployed_sha)
    except CanaryError as exc:
        print(json.dumps({
            "status": "FAIL",
            "error": str(exc),
            "secrets_disclosed": False,
        }, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
