#!/usr/bin/env python3
"""Install one exact Street Story Git revision on DevCoveer2.

This script is intentionally project-specific deployment source. It has no generic
command endpoint and prints only sanitized receipts. Secrets are read and written
inside this process and are never emitted to stdout/stderr.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

REPOSITORY = "onedayonemasterpiece/street-story"
BRANCH = "work/street-story-mvp-20260908"
SERVICE = "street-story.service"
PORT = 8188
VIBE_ALIAS = "lovekenig_tg"
VIBE_BASE_URL = "http://127.0.0.1:18765"

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASES_ROOT = Path("/home/dev/.local/share/street-story/releases")
STATE_ROOT = Path("/home/dev/.local/state/street-story")
DATA_ROOT = STATE_ROOT / "data"
PROVIDERS_ENV = STATE_ROOT / "providers.env"
SERVICE_ENV = STATE_ROOT / "service.env"
DEVICE_TOKEN_FILE = STATE_ROOT / "device-token.txt"
VIBE_TOKEN_FILE = STATE_ROOT / "vibepublish-token"
VIBE_PRINCIPAL_FILE = STATE_ROOT / "vibepublish-principal"
UNIT_ROOT = Path.home() / ".config/systemd/user"
UNIT_FILE = UNIT_ROOT / SERVICE

HOST_ENV = Path("/home/dev/.env")
VIBE_SOURCE = Path("/home/dev/projects/vibepublish")
VIBE_PY = Path("/home/dev/.local/opt/vibepublish/bin/python")
VIBE_DB = Path("/home/dev/.local/state/vibepublish/vibepublish.sqlite3")
VIBE_OWNER_TOKEN_FILE = Path("/home/dev/.local/state/vibepublish/owner-token.txt")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class DeployError(RuntimeError):
    pass


def _safe_output(value: str) -> str:
    value = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [redacted]", value)
    value = re.sub(
        r"(?i)((?:token|secret|password|api[_-]?key)[=:]\s*)[^\s]+",
        r"\1[redacted]",
        value,
    )
    return value[:2000]


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 120,
    sensitive: bool = False,
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"command unavailable: {argv[0]} ({type(exc).__name__})") from None
    if result.returncode:
        detail = "sensitive command failed" if sensitive else _safe_output(result.stdout or "")
        raise DeployError(f"{argv[0]} failed ({result.returncode}): {detail}") from None
    return result.stdout or ""


def require_mode(path: Path, mode: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeployError(f"required private file is unavailable: {path}") from exc
    if path.is_symlink() or stat.S_IMODE(info.st_mode) != mode:
        raise DeployError(f"unsafe permissions for private file: {path}")


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def parse_dotenv(path: Path) -> dict[str, str]:
    require_mode(path, 0o600)
    values: dict[str, str] = {}
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeployError(f"cannot read provider environment: {path}") from exc
    for raw in rows:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if ENV_KEY_RE.fullmatch(key) is None:
            continue
        try:
            parsed = shlex.split(f"x={value.strip()}", posix=True)
        except ValueError as exc:
            raise DeployError(f"invalid value syntax for provider key {key}") from exc
        if len(parsed) != 1 or not parsed[0].startswith("x="):
            raise DeployError(f"invalid value syntax for provider key {key}")
        values[key] = parsed[0][2:]
    return values


def render_env(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in values.items())


def exact_source(expected_sha: str) -> tuple[str, str]:
    if SHA_RE.fullmatch(expected_sha) is None:
        raise DeployError("expected SHA must be 40 lowercase hex characters")
    status = run(["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1"], timeout=30)
    if status.strip():
        raise DeployError("canonical Street Story checkout is not clean")
    remote_ref = f"refs/remotes/origin/{BRANCH}"
    run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{BRANCH}:{remote_ref}",
        ],
        timeout=180,
    )
    remote_sha = run(["git", "-C", str(REPO_ROOT), "rev-parse", remote_ref], timeout=30).strip()
    head_sha = run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], timeout=30).strip()
    if remote_sha != expected_sha or head_sha != expected_sha:
        raise DeployError(
            f"exact source gate failed: expected={expected_sha} head={head_sha} remote={remote_sha}"
        )
    tree_sha = run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", f"{expected_sha}^{{tree}}"],
        timeout=30,
    ).strip()
    return expected_sha, tree_sha


def materialize_release(sha: str, tree_sha: str) -> Path:
    RELEASES_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RELEASES_ROOT, 0o700)
    release = RELEASES_ROOT / sha
    manifest = release / ".street-story-release.json"
    expected = {"repository": REPOSITORY, "release_sha": sha, "tree_sha": tree_sha}
    if release.exists():
        if not release.is_dir() or release.is_symlink():
            raise DeployError("existing release path is unsafe")
        try:
            current = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise DeployError("existing release is incomplete or unverified") from exc
        if current != expected or not (release / "source/backend/street_story").is_dir():
            raise DeployError("existing release manifest/content mismatch")
        return release

    stage = Path(tempfile.mkdtemp(prefix=".street-story-release-", dir=RELEASES_ROOT))
    archive = stage / "source.tar"
    source = stage / "source"
    source.mkdir(mode=0o700)
    try:
        run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "archive",
                "--format=tar",
                "-o",
                str(archive),
                sha,
            ],
            timeout=180,
        )
        run(["tar", "-xf", str(archive), "-C", str(source)], timeout=120)
        archive.unlink(missing_ok=True)
        private_write(stage / ".street-story-release.json", json.dumps(expected, sort_keys=True))
        os.chmod(stage, 0o700)
        os.replace(stage, release)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return release


def _pip_driver() -> str:
    candidates = [
        Path("/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python"),
        Path("/home/dev/.local/opt/vibepublish/bin/python"),
    ]
    system_python = shutil.which("python3")
    if system_python:
        candidates.append(Path(system_python))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [str(candidate), "-m", "pip", "--version"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return str(candidate)
    raise DeployError("no installed pip driver is available for the Python 3.12 runtime")


def ensure_venv(release: Path) -> Path:
    source = release / "source"
    requirements = source / "backend/requirements.txt"
    if not requirements.is_file():
        raise DeployError("backend requirements are missing from exact release")
    venv = release / "venv"
    python = shutil.which("python3.12")
    if not python:
        raise DeployError("python3.12 is unavailable")

    target_python = venv / "bin/python"
    if not target_python.is_file():
        if venv.exists():
            shutil.rmtree(venv)
        run([python, "-m", "venv", "--without-pip", str(venv)], timeout=180)
    if not target_python.is_file():
        raise DeployError("Python 3.12 venv was not created")

    driver = _pip_driver()
    run(
        [
            driver,
            "-m",
            "pip",
            "--python",
            str(target_python),
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ],
        timeout=900,
    )
    run(
        [
            str(target_python),
            "-c",
            "import fastapi,httpx,pydantic,uvicorn",
        ],
        timeout=30,
    )
    run(
        [
            str(target_python),
            "-m",
            "compileall",
            "-q",
            str(source / "backend/street_story"),
        ],
        timeout=120,
    )
    return venv


def configure_provider_env() -> None:
    host = parse_dotenv(HOST_ENV)
    refs: list[str] = []
    for ordinal in range(1, 33):
        name = "GOOGLE_API_KEY" if ordinal == 1 else f"GOOGLE_API_KEY{ordinal}"
        if host.get(name):
            refs.append(name)
    if not refs:
        raise DeployError("registered Google API key pool is unavailable")
    quota_url = (
        host.get("GOOGLE_AI_LIMITER_SUPABASE_URL")
        or host.get("SUPABASE_URL")
        or ""
    ).strip()
    quota_key = (
        host.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY")
        or host.get("SUPABASE_SERVICE_KEY")
        or host.get("SUPABASE_KEY")
        or ""
    ).strip()
    if not quota_url or not quota_key:
        raise DeployError("shared Google AI limiter credentials are unavailable")
    values = {name: host[name] for name in refs}
    values.update(
        {
            "GEMINI_API_KEY_REFS": json.dumps(refs, separators=(",", ":")),
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_QUOTA_SUPABASE_URL": quota_url.rstrip("/"),
            "GEMINI_QUOTA_SUPABASE_KEY": quota_key,
        }
    )
    private_write(PROVIDERS_ENV, render_env(values))


def device_token() -> tuple[str, bool]:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    if DEVICE_TOKEN_FILE.exists():
        require_mode(DEVICE_TOKEN_FILE, 0o600)
        token = DEVICE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise DeployError("stored Street Story device token is invalid")
        created = False
    else:
        token = secrets.token_urlsafe(36)
        private_write(DEVICE_TOKEN_FILE, token)
        created = True
    gh = shutil.which("gh")
    if not gh:
        raise DeployError("GitHub CLI is unavailable for live-token synchronization")
    run(
        [gh, "secret", "set", "STREET_STORY_LIVE_TOKEN", "--repo", REPOSITORY],
        input_text=token,
        timeout=120,
        sensitive=True,
    )
    return token, created


def vibe_request(
    token: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    request_key: str | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if request_key:
        headers["Idempotency-Key"] = request_key
    request = urllib.request.Request(VIBE_BASE_URL + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        raise DeployError(f"VibePublish HTTP {exc.code} for {path}") from None
    except (OSError, urllib.error.URLError, ValueError, TypeError) as exc:
        raise DeployError(f"VibePublish request failed for {path}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise DeployError(f"VibePublish returned invalid JSON for {path}")
    return value


def owner_binding() -> tuple[str, str, str, str]:
    if not VIBE_DB.is_file():
        raise DeployError("current VibePublish state database is unavailable")
    db = sqlite3.connect(f"file:{VIBE_DB}?mode=ro", uri=True)
    try:
        rows = db.execute(
            """
            SELECT p.tenant_id, c.id, d.native_id, d.label
            FROM bindings b
            JOIN principals p ON p.id=b.principal_id
            JOIN destinations d ON d.id=b.destination_id
            JOIN connections c ON c.id=d.connection_id
            WHERE p.owner=1 AND b.alias=? AND b.active=1
              AND c.active=1 AND c.provider='telegram'
            """,
            (VIBE_ALIAS,),
        ).fetchall()
    finally:
        db.close()
    if len(rows) != 1 or not all(isinstance(value, str) and value for value in rows[0]):
        raise DeployError("current owner Telegram binding is not uniquely available")
    return tuple(rows[0])  # type: ignore[return-value]


def principal_binding_exists(principal: str) -> bool:
    db = sqlite3.connect(f"file:{VIBE_DB}?mode=ro", uri=True)
    try:
        row = db.execute(
            """
            SELECT 1
            FROM bindings b
            JOIN destinations d ON d.id=b.destination_id
            JOIN connections c ON c.id=d.connection_id
            WHERE b.principal_id=? AND b.alias=? AND b.active=1
              AND c.active=1 AND c.provider='telegram'
            LIMIT 1
            """,
            (principal, VIBE_ALIAS),
        ).fetchone()
    finally:
        db.close()
    return row is not None


def choose_principal(base: str) -> str:
    db = sqlite3.connect(f"file:{VIBE_DB}?mode=ro", uri=True)
    try:
        used = {
            str(row[0])
            for row in db.execute(
                "SELECT id FROM principals WHERE id=? OR id GLOB ?",
                (base, base + "-*"),
            )
        }
    finally:
        db.close()
    if base not in used:
        return base
    for ordinal in range(2, 100):
        candidate = f"{base}-{ordinal}"
        if candidate not in used:
            return candidate
    raise DeployError("no free Street Story VibePublish principal name")


def ensure_vibe_principal(sha: str) -> tuple[str, str]:
    for required in (VIBE_PY, VIBE_DB, VIBE_OWNER_TOKEN_FILE):
        if not required.is_file():
            raise DeployError(f"current VibePublish management runtime is unavailable: {required}")
    if VIBE_TOKEN_FILE.exists() or VIBE_PRINCIPAL_FILE.exists():
        require_mode(VIBE_TOKEN_FILE, 0o600)
        require_mode(VIBE_PRINCIPAL_FILE, 0o600)
        token = VIBE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        principal = VIBE_PRINCIPAL_FILE.read_text(encoding="utf-8").strip()
        if len(token) < 20 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", principal):
            raise DeployError("stored VibePublish principal state is invalid")
        vibe_request(token, "GET", "/v1/bootstrap")
    else:
        require_mode(VIBE_OWNER_TOKEN_FILE, 0o600)
        owner_token = VIBE_OWNER_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(owner_token) < 20:
            raise DeployError("VibePublish owner token is invalid")
        tenant, _connection, _native_id, _label = owner_binding()
        principal = choose_principal(f"street-story-runtime-{sha[:12]}")
        env = os.environ.copy()
        env["VIBEPUBLISH_SERVICE_TOKEN"] = owner_token
        raw = run(
            [
                str(VIBE_PY),
                "-m",
                "social_operations.cli",
                "--db",
                str(VIBE_DB),
                "principal",
                "--tenant",
                tenant,
                "--principal",
                principal,
            ],
            cwd=VIBE_SOURCE,
            env=env,
            timeout=60,
            sensitive=True,
        )
        try:
            token = str(json.loads(raw)["service_token"])
        except (ValueError, TypeError, KeyError) as exc:
            raise DeployError("VibePublish principal creation readback is invalid") from exc
        if len(token) < 20:
            raise DeployError("VibePublish principal creation returned an invalid token")
        private_write(VIBE_TOKEN_FILE, token)
        private_write(VIBE_PRINCIPAL_FILE, principal)

    if not principal_binding_exists(principal):
        require_mode(VIBE_OWNER_TOKEN_FILE, 0o600)
        owner_token = VIBE_OWNER_TOKEN_FILE.read_text(encoding="utf-8").strip()
        tenant, connection, native_id, label = owner_binding()
        del tenant
        env = os.environ.copy()
        env["VIBEPUBLISH_SERVICE_TOKEN"] = owner_token
        run(
            [
                str(VIBE_PY),
                "-m",
                "social_operations.cli",
                "--db",
                str(VIBE_DB),
                "bind",
                "--principal",
                principal,
                "--alias",
                VIBE_ALIAS,
                "--connection",
                connection,
                "--native-id",
                native_id,
                "--label",
                label,
            ],
            cwd=VIBE_SOURCE,
            env=env,
            timeout=60,
            sensitive=True,
        )
    return principal, token


def preview_preflight(token: str, sha: str) -> dict[str, str]:
    request_key = f"street-story-deploy-preview-{sha[:24]}"
    receipt = vibe_request(
        token,
        "POST",
        "/v1/publications",
        body={
            "to": [VIBE_ALIAS],
            "content": {"text": "Street Story deployment preflight. Preview only; do not dispatch."},
            "mode": "preview",
        },
        request_key=request_key,
    )
    operation_id = str(receipt.get("operation_id") or "")
    if not operation_id:
        raise DeployError("VibePublish preview returned no operation_id")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = vibe_request(token, "GET", f"/v1/operations/{operation_id}")
        receipts = status.get("receipts")
        current = None
        if isinstance(receipts, list):
            current = next(
                (
                    item
                    for item in receipts
                    if isinstance(item, dict) and item.get("operation_id") == operation_id
                ),
                None,
            )
        if current and current.get("operation_complete") is True:
            state = str(current.get("state") or "")
            if state not in {"needs_approval", "verified"}:
                raise DeployError(f"VibePublish preview ended in unexpected state {state or 'unknown'}")
            break
        time.sleep(1)
    else:
        raise DeployError("VibePublish preview preflight timed out")

    bootstrap = vibe_request(token, "GET", "/v1/bootstrap")
    caps = [
        row
        for row in bootstrap.get("capabilities", [])
        if isinstance(row, dict)
        and row.get("destination") == VIBE_ALIAS
        and row.get("operation") == "publish"
        and row.get("surface") == "post"
    ]
    if len(caps) != 1 or caps[0].get("status") != "supported":
        raise DeployError("VibePublish Telegram preview did not establish supported capability")
    return {
        "alias": VIBE_ALIAS,
        "status": "supported",
        "reason": str(caps[0].get("reason") or "")[:300],
    }


def write_service_env(device: str, vibe: str, sha: str) -> None:
    private_write(
        SERVICE_ENV,
        render_env(
            {
                "DATA_DIR": str(DATA_ROOT),
                "STREET_STORY_DEVICE_TOKEN": device,
                "VIBEPUBLISH_BASE_URL": VIBE_BASE_URL,
                "VIBEPUBLISH_BEARER_TOKEN": vibe,
                "STREET_STORY_DEPLOY_SHA": sha,
                "STREET_STORY_OSM_USER_AGENT": (
                    "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"
                ),
                "WORKER_POLL_SECONDS": "1",
                "PROCESSING_DELAYED_AFTER_SECONDS": "1800",
            }
        ),
    )


def systemd_env() -> dict[str, str]:
    uid = os.getuid()
    runtime = Path(f"/run/user/{uid}")
    bus = runtime / "bus"
    if not bus.exists():
        raise DeployError("user-systemd bus is unavailable")
    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def install_service(release: Path, venv: Path) -> None:
    source = release / "source"
    DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA_ROOT, 0o700)
    UNIT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit = "\n".join(
        (
            "[Unit]",
            "Description=Street Story backend (exact DevCoveer release)",
            "After=network-online.target vibepublish.service",
            "Wants=network-online.target",
            "Requires=vibepublish.service",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={source}",
            f"EnvironmentFile={PROVIDERS_ENV}",
            f"EnvironmentFile={SERVICE_ENV}",
            (
                f"ExecStart={venv}/bin/uvicorn street_story.app:app "
                f"--app-dir {source}/backend --host 127.0.0.1 --port {PORT} --workers 1"
            ),
            "Restart=on-failure",
            "RestartSec=3",
            "TimeoutStopSec=30",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            f"ReadWritePaths={STATE_ROOT}",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )
    private_write(UNIT_FILE, unit)
    env = systemd_env()
    run(["systemctl", "--user", "daemon-reload"], env=env, timeout=30)
    run(["systemctl", "--user", "enable", SERVICE], env=env, timeout=30)
    run(["systemctl", "--user", "restart", SERVICE], env=env, timeout=180)


def service_status() -> dict[str, str]:
    raw = run(
        [
            "systemctl",
            "--user",
            "show",
            SERVICE,
            "--property=ActiveState,SubState,MainPID,ExecMainStatus",
        ],
        env=systemd_env(),
        timeout=30,
    )
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    result = {
        "active": fields.get("ActiveState", "unknown"),
        "substate": fields.get("SubState", "unknown"),
        "main_pid": fields.get("MainPID", "0"),
        "exec_status": fields.get("ExecMainStatus", "unknown"),
    }
    if result["active"] != "active":
        raise DeployError(f"Street Story service is not active: {result['active']}/{result['substate']}")
    return result


def http_json(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError, TypeError) as exc:
        raise DeployError(f"local HTTP readback failed: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise DeployError("local HTTP readback returned invalid JSON")
    return value


def verify_runtime(expected_sha: str, device: str) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + 90
    health: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            health = http_json(f"http://127.0.0.1:{PORT}/healthz")
            if health.get("ok") is True and health.get("source_sha") == expected_sha:
                break
        except DeployError:
            pass
        time.sleep(1)
    else:
        raise DeployError("Street Story local exact-SHA health gate failed")

    capabilities = http_json(f"http://127.0.0.1:{PORT}/v1/capabilities", token=device, timeout=15)
    rows = capabilities.get("destinations")
    if not isinstance(rows, list):
        raise DeployError("Street Story capabilities projection is invalid")
    safe_rows = [
        {
            "alias": str(row.get("alias") or ""),
            "provider": str(row.get("provider") or ""),
            "status": str(row.get("status") or ""),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    if safe_rows != [{"alias": VIBE_ALIAS, "provider": "telegram", "status": "supported"}]:
        raise DeployError(f"unexpected Street Story destination projection: {safe_rows}")
    return health, {"destinations": safe_rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    expected_sha = args.expected_sha.strip().lower()

    sha, tree_sha = exact_source(expected_sha)
    release = materialize_release(sha, tree_sha)
    venv = ensure_venv(release)
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    configure_provider_env()
    device, token_created = device_token()
    principal, vibe_token = ensure_vibe_principal(sha)
    preflight = preview_preflight(vibe_token, sha)
    write_service_env(device, vibe_token, sha)
    install_service(release, venv)
    status = service_status()
    health, capabilities = verify_runtime(sha, device)

    final_status = run(["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1"], timeout=30)
    if final_status.strip():
        raise DeployError("deployment changed the canonical repository checkout")

    receipt = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "branch": BRANCH,
        "release_sha": sha,
        "tree_sha": tree_sha,
        "release_root": str(release),
        "listener": f"127.0.0.1:{PORT}",
        "service": status,
        "local_health": {
            "ok": health.get("ok") is True,
            "source_sha": health.get("source_sha"),
        },
        "capabilities": capabilities,
        "vibepublish": {
            "principal": principal,
            "preflight": preflight,
        },
        "github_live_token_synchronized": True,
        "device_token_created": token_created,
        "secrets_disclosed": False,
    }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeployError as exc:
        print(json.dumps({"status": "failed", "error": _safe_output(str(exc)), "secrets_disclosed": False}))
        raise SystemExit(2)
