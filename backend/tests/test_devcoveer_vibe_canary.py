from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "devcoveer_vibe_canary.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("devcoveer_vibe_canary", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operation_receipt_requires_exact_operation() -> None:
    module = load_module()
    installer = SimpleNamespace(
        vibe_request=lambda token, method, path: {
            "receipts": [
                {"operation_id": "other", "operation_complete": True},
                {"operation_id": "wanted", "operation_complete": True, "state": "scheduled"},
            ]
        }
    )
    assert module.operation_receipt(installer, "secret", "wanted")["state"] == "scheduled"


def test_telegram_delivery_is_exact_alias_and_provider() -> None:
    module = load_module()
    row = {
        "deliveries": [
            {"destination": "other", "provider": "telegram"},
            {
                "destination": "lovekenig_tg",
                "provider": "telegram",
                "state": "scheduled",
                "observed": "provider_scheduled",
            },
        ]
    }
    assert module.telegram_delivery(row, "lovekenig_tg")["observed"] == "provider_scheduled"


def test_bootstrap_supported_requires_exact_capability() -> None:
    module = load_module()
    installer = SimpleNamespace(
        VIBE_ALIAS="lovekenig_tg",
        vibe_request=lambda token, method, path: {
            "capabilities": [
                {
                    "destination": "lovekenig_tg",
                    "operation": "publish",
                    "surface": "post",
                    "status": "supported",
                }
            ]
        },
    )
    assert module.bootstrap_supported(installer, "secret") is True


def test_run_schedules_cancels_and_requires_supported(monkeypatch) -> None:
    module = load_module()
    calls: list[tuple[str, str, dict | None, str | None]] = []

    class Installer:
        VIBE_ALIAS = "lovekenig_tg"

        @staticmethod
        def ensure_vibe_principal(sha):
            assert sha == "a" * 40
            return "street-story-runtime", "secret"

        @staticmethod
        def vibe_request(token, method, path, *, body=None, request_key=None):
            assert token == "secret"
            calls.append((method, path, body, request_key))
            if method == "POST" and path == "/v1/publications":
                return {"operation_id": "op_pub", "resource_id": "pub_1", "revision": 1}
            if method == "GET" and path == "/v1/operations/op_pub":
                return {
                    "receipts": [{
                        "operation_id": "op_pub",
                        "operation_complete": True,
                        "state": "scheduled",
                        "deliveries": [{
                            "destination": "lovekenig_tg",
                            "provider": "telegram",
                            "state": "scheduled",
                            "observed": "provider_scheduled",
                        }],
                    }]
                }
            if method == "POST" and path == "/v1/publications/pub_1/commands":
                return {"operation_id": "op_cancel"}
            if method == "GET" and path == "/v1/operations/op_cancel":
                return {
                    "receipts": [{
                        "operation_id": "op_cancel",
                        "operation_complete": True,
                        "state": "cancelled",
                        "deliveries": [{
                            "destination": "lovekenig_tg",
                            "provider": "telegram",
                            "state": "cancelled",
                            "observed": "cancelled",
                        }],
                    }]
                }
            if method == "GET" and path == "/v1/bootstrap":
                return {
                    "capabilities": [{
                        "destination": "lovekenig_tg",
                        "operation": "publish",
                        "surface": "post",
                        "status": "supported",
                    }]
                }
            raise AssertionError((method, path))

    monkeypatch.setattr(module, "verify_public_health", lambda sha: None)
    monkeypatch.setattr(module, "load_installer", lambda root: Installer)
    receipt = module.run("a" * 40)

    assert receipt["status"] == "PASS"
    assert receipt["scheduled_observed"] is True
    assert receipt["cancelled_observed"] is True
    publish = next(call for call in calls if call[1] == "/v1/publications")
    assert publish[2]["mode"] == "execute"
    assert publish[2]["delivery"]["kind"] == "at"
    cancel = next(call for call in calls if call[1] == "/v1/publications/pub_1/commands")
    assert cancel[2] == {"expected_revision": 1, "change": {"kind": "cancel"}}


def test_cleanup_attempted_after_verified_schedule_failure(monkeypatch) -> None:
    module = load_module()
    cancel_called = False

    class Installer:
        VIBE_ALIAS = "lovekenig_tg"

        @staticmethod
        def ensure_vibe_principal(sha):
            return "street-story-runtime", "secret"

        @staticmethod
        def vibe_request(token, method, path, *, body=None, request_key=None):
            nonlocal cancel_called
            if method == "POST" and path == "/v1/publications":
                return {"operation_id": "op_pub", "resource_id": "pub_1", "revision": 1}
            if method == "GET" and path == "/v1/operations/op_pub":
                return {
                    "receipts": [{
                        "operation_id": "op_pub",
                        "operation_complete": True,
                        "state": "scheduled",
                        "deliveries": [{
                            "destination": "lovekenig_tg",
                            "provider": "telegram",
                            "state": "scheduled",
                            "observed": "provider_scheduled",
                        }],
                    }]
                }
            if method == "POST" and path == "/v1/publications/pub_1/commands":
                cancel_called = True
                return {"operation_id": "op_cancel"}
            if method == "GET" and path == "/v1/operations/op_cancel":
                return {
                    "receipts": [{
                        "operation_id": "op_cancel",
                        "operation_complete": True,
                        "state": "cancelled",
                        "deliveries": [{
                            "destination": "lovekenig_tg",
                            "provider": "telegram",
                            "state": "cancelled",
                            "observed": "cancelled",
                        }],
                    }]
                }
            if method == "GET" and path == "/v1/bootstrap":
                return {"capabilities": []}
            raise AssertionError((method, path))

    monkeypatch.setattr(module, "verify_public_health", lambda sha: None)
    monkeypatch.setattr(module, "load_installer", lambda root: Installer)

    with pytest.raises(module.CanaryError, match="runtime_capability_not_supported"):
        module.run("b" * 40)
    assert cancel_called is True
