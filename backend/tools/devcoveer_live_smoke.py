#!/usr/bin/env python3
"""Run the repository-owned Street Story smoke in an ephemeral Docker runner.

This is a DevCoveer acceptance rail for periods when GitHub-hosted Actions do not
allocate a job. It deliberately does not install host packages or discover
credentials. The already-provisioned Street Story device token is obtained only
through the deployment module's guarded helper and is passed to Docker through
the parent process environment, never argv or output.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from typing import Any

IMAGE = "python:3.12-slim-bookworm"
BASE_URL = "https://street-story.kenigevents.ru"
CONTAINER_PREFIX = "street-story-live-smoke-"
DEFAULT_DIAGNOSTIC = "diagnostic-devcoveer-smoke.json"
CONTAINER_TIMEOUT_SECONDS = 25 * 60


class SmokeRunnerError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_installer(root: Path):
    path = root / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    if spec is None or spec.loader is None:
        raise SmokeRunnerError("installer_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_device_token(installer) -> str:
    path = installer.DEVICE_TOKEN_FILE
    if not path.exists():
        raise SmokeRunnerError("device_token_missing")
    installer.require_mode(path, 0o600)
    token, created = installer.device_token()
    if created:
        raise SmokeRunnerError("device_token_unexpectedly_created")
    if not isinstance(token, str) or len(token) < 32:
        raise SmokeRunnerError("device_token_invalid")
    return token


def verify_local_head(root: Path, expected_sha: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
    )
    if result.returncode != 0 or result.stdout.strip() != expected_sha:
        raise SmokeRunnerError("local_source_sha_mismatch")


def verify_public_health(expected_sha: str) -> None:
    request = urllib.request.Request(
        BASE_URL + "/healthz",
        headers={"Accept": "application/json", "User-Agent": "StreetStory-DevCoveer-Smoke/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SmokeRunnerError("public_health_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SmokeRunnerError("public_health_invalid")
    if str(payload.get("source_sha") or "") != expected_sha:
        raise SmokeRunnerError("public_source_sha_mismatch")


def docker_command(
    *,
    root: Path,
    output_dir: Path,
    container_name: str,
    diagnostic_name: str,
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--mount",
        f"type=bind,src={root},dst=/src,readonly",
        "--mount",
        f"type=bind,src={output_dir},dst=/out",
        "--workdir",
        "/src/backend",
        "--env",
        "STREET_STORY_LIVE_TOKEN",
        "--env",
        f"STREET_STORY_LIVE_BASE_URL={BASE_URL}",
        "--env",
        "LIVE_E2E_MODE=smoke",
        "--env",
        f"STREET_STORY_LIVE_DIAGNOSTIC=/out/{diagnostic_name}",
        IMAGE,
        "sh",
        "-lc",
        (
            "set -eu; "
            "apt-get update >/dev/null; "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
            "espeak ffmpeg libimage-exiftool-perl ca-certificates >/dev/null; "
            "python -m pip install --disable-pip-version-check --no-cache-dir "
            "'httpx>=0.27,<1' >/dev/null; "
            "exec python /src/backend/tools/live_e2e.py"
        ),
    ]


def bounded_host_environment(token: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "STREET_STORY_LIVE_TOKEN": token,
    }
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host:
        environment["DOCKER_HOST"] = docker_host
    return environment


def cleanup_container(container_name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            **(
                {"DOCKER_HOST": os.environ["DOCKER_HOST"]}
                if os.environ.get("DOCKER_HOST", "").strip()
                else {}
            ),
        },
    )


def diagnostic_receipt(path: Path, expected_sha: str, exit_code: int) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeRunnerError("diagnostic_missing_or_invalid") from exc
    if not isinstance(payload, dict) or payload.get("mode") != "smoke":
        raise SmokeRunnerError("diagnostic_invalid")
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    ok_steps = [
        str(step.get("name"))
        for step in steps
        if isinstance(step, dict) and step.get("status") == "ok" and step.get("name")
    ]
    smoke_success = payload.get("smoke_success") is True
    result = str(payload.get("result") or "")
    if smoke_success and (result != "passed" or exit_code != 0):
        raise SmokeRunnerError("diagnostic_exit_mismatch")
    return {
        "status": "PASS" if smoke_success else "FAIL",
        "source_sha": expected_sha,
        "result": result or None,
        "failure_code": payload.get("failure_code"),
        "smoke_success": smoke_success,
        "milestones": ok_steps,
        "container_cleanup": True,
        "secrets_disclosed": False,
        "source_modified": False,
    }


def run(expected_sha: str, *, diagnostic_name: str = DEFAULT_DIAGNOSTIC) -> dict[str, Any]:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise SmokeRunnerError("expected_sha_invalid")
    if "/" in diagnostic_name or diagnostic_name in {"", ".", ".."}:
        raise SmokeRunnerError("diagnostic_name_invalid")
    root = repo_root()
    verify_local_head(root, expected_sha)
    verify_public_health(expected_sha)
    if shutil.which("docker") is None:
        raise SmokeRunnerError("docker_unavailable")

    output_dir = root / "backend" / "live-e2e-artifacts"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    diagnostic_path = output_dir / diagnostic_name
    installer = load_installer(root)
    token = existing_device_token(installer)
    container_name = CONTAINER_PREFIX + uuid.uuid4().hex[:12]
    command = docker_command(
        root=root,
        output_dir=output_dir,
        container_name=container_name,
        diagnostic_name=diagnostic_name,
    )
    try:
        process = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONTAINER_TIMEOUT_SECONDS,
            env=bounded_host_environment(token),
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeRunnerError("smoke_timeout") from exc
    finally:
        cleanup_container(container_name)

    # Never print child output: the durable diagnostic is the only accepted evidence.
    return diagnostic_receipt(diagnostic_path, expected_sha, process.returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--diagnostic-name", default=DEFAULT_DIAGNOSTIC)
    args = parser.parse_args()
    try:
        receipt = run(args.expected_sha, diagnostic_name=args.diagnostic_name)
    except SmokeRunnerError as exc:
        print(json.dumps({
            "status": "ERROR",
            "error": str(exc),
            "secrets_disclosed": False,
            "source_modified": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
