from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "devcoveer_live_smoke.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("devcoveer_live_smoke", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_device_token_never_mints(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    token_file = tmp_path / "device-token.txt"
    token_file.write_text("x" * 40, encoding="utf-8")
    token_file.chmod(0o600)
    calls: list[tuple[str, object]] = []

    installer = SimpleNamespace(
        DEVICE_TOKEN_FILE=token_file,
        require_mode=lambda path, mode: calls.append(("mode", (path, mode))),
        device_token=lambda: ("x" * 40, False),
    )

    assert module.existing_device_token(installer) == "x" * 40
    assert calls == [("mode", (token_file, 0o600))]


def test_missing_device_token_stops_before_helper(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    called = False

    def device_token():
        nonlocal called
        called = True
        return ("x" * 40, True)

    installer = SimpleNamespace(
        DEVICE_TOKEN_FILE=tmp_path / "missing-token.txt",
        require_mode=lambda *_args: None,
        device_token=device_token,
    )

    with pytest.raises(module.SmokeRunnerError, match="device_token_missing"):
        module.existing_device_token(installer)
    assert called is False


def test_docker_command_passes_token_by_name_only(tmp_path: Path) -> None:
    module = load_module()
    command = module.docker_command(
        root=tmp_path / "repo",
        output_dir=tmp_path / "output",
        container_name="street-story-live-smoke-fixture",
        diagnostic_name="diag.json",
    )

    assert "--rm" in command
    assert "STREET_STORY_LIVE_TOKEN" in command
    assert not any("fixture-secret-value" in item for item in command)
    index = command.index("STREET_STORY_LIVE_TOKEN")
    assert command[index - 1] == "--env"
    assert "SAFE_TEST_DESTINATION_ALIAS" not in command
    assert any("espeak ffmpeg libimage-exiftool-perl" in item for item in command)
    assert any("/src/backend/tools/live_e2e.py" in item for item in command)


def test_bounded_host_environment_drops_unrelated_secrets(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/fixture")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-flow")
    environment = module.bounded_host_environment("fixture-token")

    assert environment["STREET_STORY_LIVE_TOKEN"] == "fixture-token"
    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == "/home/fixture"
    assert "UNRELATED_SECRET" not in environment


def test_diagnostic_receipt_is_bounded(tmp_path: Path) -> None:
    module = load_module()
    path = tmp_path / "diag.json"
    path.write_text(
        json.dumps({
            "mode": "smoke",
            "result": "passed",
            "smoke_success": True,
            "failure_code": None,
            "steps": [
                {"name": "voice_sync", "status": "ok"},
                {"name": "research", "status": "ok"},
                {"name": "ignored", "status": "observed"},
            ],
        }),
        encoding="utf-8",
    )

    receipt = module.diagnostic_receipt(path, "a" * 40, 0)

    assert receipt == {
        "status": "PASS",
        "source_sha": "a" * 40,
        "result": "passed",
        "failure_code": None,
        "smoke_success": True,
        "milestones": ["voice_sync", "research"],
        "container_cleanup": True,
        "secrets_disclosed": False,
        "source_modified": False,
    }


def test_success_diagnostic_rejects_nonzero_child_exit(tmp_path: Path) -> None:
    module = load_module()
    path = tmp_path / "diag.json"
    path.write_text(
        json.dumps({
            "mode": "smoke",
            "result": "passed",
            "smoke_success": True,
            "failure_code": None,
            "steps": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(module.SmokeRunnerError, match="diagnostic_exit_mismatch"):
        module.diagnostic_receipt(path, "b" * 40, 1)
