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
TEST_VIBE_ALIAS = "street_story_e2e_20260928_tg"
VIBE_BASE_URL = "http://127.0.0.1:18765"
VIBE_HTTP_HOST = "mcp-vibepublish.kenigevents.ru"
CANONICAL_GOOGLE_AI_LIMITER_URL = "https://epyznmylqmchteykjsqj.supabase.co"
CANONICAL_GOOGLE_AI_LIMITER_CONTRACT = "google_ai_project_model_atomic_v1"
CANONICAL_GOOGLE_AI_QUOTA_SCOPE_DIMENSION = "google_cloud_project"

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
BRIDGE_PYTHON = Path("/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python")
AI_RESOURCE_CONTROL_VERSION = "0.1.14"
AI_RESOURCE_CONTROL_RELEASE_SHA = "a82a97147d697c3fbf0ba0748d6e49be196d0a7a"
AI_RESOURCE_CONTROL_WHEEL_SHA256 = "186273b4d49c1fb8b6060f885b4c7edbe8a7eaf2f76e23eafbaddb6d90597662"
AI_RESOURCE_CONTROL_REPO = Path("/home/dev/projects/ai-resource-control")
RESEARCH_ROOT = STATE_ROOT / 'research'
RESEARCH_DIRECTORY = RESEARCH_ROOT / 'opencode'
RESEARCH_QUALIFICATION = RESEARCH_ROOT / 'qualification.json'
RESEARCH_CA = RESEARCH_ROOT / 'russian-root-ca.crt'
VIBE_DB = Path("/home/dev/.local/state/vibepublish/vibepublish.sqlite3")
VIBE_OWNER_TOKEN_FILE = Path("/home/dev/.local/state/vibepublish/owner-token.txt")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class DeployError(RuntimeError):
    pass


class VibeHttpError(DeployError):
    def __init__(self, status: int, path: str, code: str | None = None):
        self.status = status
        self.path = path
        self.code = code
        suffix = f" ({code})" if code else ""
        super().__init__(f"VibePublish HTTP {status}{suffix} for {path}")


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


def _tracked_status() -> str:
    return run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ],
        timeout=30,
    )


def exact_source(expected_sha: str) -> tuple[str, str]:
    if SHA_RE.fullmatch(expected_sha) is None:
        raise DeployError("expected SHA must be 40 lowercase hex characters")
    status = _tracked_status()
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


def deployed_release_sha() -> str | None:
    """Read the actual unit, rather than trusting a historical current symlink."""
    directory = run(
        ["systemctl", "--user", "show", SERVICE, "-p", "WorkingDirectory", "--value"],
        env=systemd_env(),
        timeout=30,
    ).strip()
    path = Path(directory)
    if path.parent.parent == RELEASES_ROOT and path.name == "source" and SHA_RE.fullmatch(path.parent.name):
        return path.parent.name
    return None


def release_process_references() -> set[str]:
    """Keep environments referenced by running processes, including other workers."""
    pattern = re.compile(re.escape(str(RELEASES_ROOT)) + r"/([0-9a-f]{40})(?:/|\b)")
    used: set[str] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            # cmdline remains available for sandboxed browser processes whose maps/fds are private.
            command = (process / "cmdline").read_bytes().decode(errors="replace")
            used.update(pattern.findall(command))
            if process.stat().st_uid != os.getuid():
                continue
            for name in ("cwd", "exe"):
                with contextlib.suppress(OSError):
                    used.update(pattern.findall(os.readlink(process / name)))
            with contextlib.suppress(OSError):
                used.update(pattern.findall((process / "maps").read_text(errors="replace")))
            with contextlib.suppress(OSError):
                for descriptor in (process / "fd").iterdir():
                    with contextlib.suppress(OSError):
                        used.update(pattern.findall(os.readlink(descriptor)))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise DeployError("cannot verify deployment environment process references") from exc
    return used


def prune_release_environments(current_sha: str, previous_sha: str | None) -> dict[str, Any]:
    """Remove only rebuildable dependency environments after the live health gate.

    Keep the deployed release, its predecessor, and every process-referenced
    release. Historical exact source, manifests, data, and evidence stay intact.
    """
    protected = {current_sha, *([previous_sha] if previous_sha else [])}
    try:
        protected.update(release_process_references())
        protected = protect_environment_owners(protected)
    except (DeployError, OSError, ValueError, KeyError):
        return {"status": "skipped", "reason": "process_references_unverified", "removed": []}
    removed: list[str] = []
    skipped: list[str] = []
    failures: list[dict[str, Any]] = []
    for release in sorted(RELEASES_ROOT.iterdir()):
        if release.name in protected or not SHA_RE.fullmatch(release.name):
            continue
        if release.is_symlink() or not release.is_dir():
            continue
        try:
            manifest = json.loads((release / ".street-story-release.json").read_text())
        except (OSError, ValueError):
            skipped.append(release.name)
            continue
        if (
            manifest.get("repository") != REPOSITORY
            or manifest.get("release_sha") != release.name
            or not (release / "source/backend/street_story").is_dir()
        ):
            skipped.append(release.name)
            continue
        environment = release / "venv"
        if environment.is_symlink() or not environment.is_dir():
            continue
        # Refresh before each removal so a newly started rollback/worker is protected.
        try:
            if release.name in protect_environment_owners(protected | release_process_references()):
                continue
        except (DeployError, OSError, ValueError, KeyError):
            return {"status": "partial", "reason": "process_references_unverified", "removed": removed}
        try:
            shutil.rmtree(environment)
        except OSError as exc:
            failures.append({"release": release.name, "errno": exc.errno})
            continue
        removed.append(release.name)
    return {"status": "partial" if failures else "completed", "protected": sorted(protected),
            "removed": removed, "skipped": skipped, "failures": failures}


def activate_release_pointer(release: Path) -> None:
    current = RELEASES_ROOT.parent / "current"
    if current.exists() and not current.is_symlink():
        raise DeployError("current release pointer is not a symlink")
    stage = current.with_name(".street-story-current-" + secrets.token_hex(8))
    try:
        stage.symlink_to(release, target_is_directory=True)
        os.replace(stage, current)
    finally:
        stage.unlink(missing_ok=True)


def _python_312_runtime() -> str:
    candidates = [BRIDGE_PYTHON]
    system_python = shutil.which("python3.12")
    if system_python:
        candidates.append(Path(system_python))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [
                str(candidate),
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return str(candidate)
    raise DeployError("no healthy Python 3.12 runtime is available")



def install_ai_resource_control(target_python: Path) -> None:
    repo = AI_RESOURCE_CONTROL_REPO
    if not (repo / ".git").is_dir():
        raise DeployError("private ai-resource-control checkout is unavailable")
    run(["git", "-C", str(repo), "fetch", "--no-tags", "origin", "main"], timeout=180)
    run(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "-e",
            AI_RESOURCE_CONTROL_RELEASE_SHA + "^{commit}",
        ],
        timeout=30,
    )

    # This is a canonical runtime dependency cache, not a second source tree or
    # an OpenCode installation. Keep the private wheel out of the public repo.
    wheels = STATE_ROOT / 'private-wheels'
    wheels.mkdir(parents=True, exist_ok=True, mode=0o700)
    wheel = wheels / f'ai_resource_control-{AI_RESOURCE_CONTROL_VERSION}-py3-none-any.whl'
    if not wheel.exists():
        run(['gh', 'release', 'download', 'v' + AI_RESOURCE_CONTROL_VERSION,
             '--repo', 'onedayonemasterpiece/ai-resource-control', '--pattern', wheel.name,
             '--dir', str(wheels)], timeout=180)
    verify_private_resource_wheel(wheel)
    run([str(target_python), '-m', 'pip', 'install', '--disable-pip-version-check',
         '--no-deps', str(wheel)], timeout=180)


def verify_private_resource_wheel(wheel: Path) -> None:
    if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != AI_RESOURCE_CONTROL_WHEEL_SHA256:
        raise DeployError('private ai-resource-control wheel digest mismatch')

def dependency_fingerprint(release: Path) -> dict[str, Any]:
    """Exact declared inputs; a source SHA alone does not require another env."""
    source = release / 'source'
    requirements = source / 'backend/requirements.txt'
    lock_path = source / 'live-framework.lock.json'
    lock = json.loads(lock_path.read_text())
    archive = source / lock['archive']
    if (archive.is_symlink() or not archive.resolve().is_relative_to((source / 'vendor').resolve())
            or hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']):
        raise DeployError('Live dependency archive digest mismatch')
    return {'python': [3, 12], 'requirements_sha256': hashlib.sha256(requirements.read_bytes()).hexdigest(),
            'live_lock_sha256': hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            'live_archive_sha256': lock['sha256'], 'live_python_version': lock['python_version'],
            'resource_version': AI_RESOURCE_CONTROL_VERSION,
            'resource_wheel_sha256': AI_RESOURCE_CONTROL_WHEEL_SHA256}


def _verified_environment_owner(environment: Path) -> Path:
    target = environment.resolve(strict=True)
    owner = target.parent
    if (target.name != 'venv' or owner.parent != RELEASES_ROOT or owner.is_symlink()
            or not SHA_RE.fullmatch(owner.name) or not (target / 'bin/python').is_file()):
        raise DeployError('dependency environment owner is unverified')
    manifest = json.loads((owner / '.street-story-release.json').read_text())
    if (manifest.get('repository') != REPOSITORY or manifest.get('release_sha') != owner.name
            or not (owner / 'source/backend/street_story').is_dir()):
        raise DeployError('dependency environment release manifest mismatch')
    return owner


def attest_dependency_environment(environment: Path, fingerprint: dict[str, Any]) -> None:
    """Read installed archive provenance, interpreter version and dependency health."""
    program = '''
import importlib.metadata as metadata,json,sys
expected=json.loads(sys.argv[1])
assert list(sys.version_info[:2])==expected['python']
for name,version,digest in [
 ('live-interaction',expected['live_python_version'],expected['live_archive_sha256']),
 ('ai-resource-control',expected['resource_version'],expected['resource_wheel_sha256'])]:
 distribution=metadata.distribution(name)
 assert distribution.version==version
 provenance=json.loads(distribution.read_text('direct_url.json') or '{}')
 archive=provenance.get('archive_info',{})
 assert archive.get('hashes',{}).get('sha256')==digest or archive.get('hash')=='sha256='+digest
import ai_resource_control,fastapi,httpx,live_interaction,pydantic,uvicorn
'''
    python = str(environment / 'bin/python')
    run([python, '-c', program, json.dumps(fingerprint, sort_keys=True)], timeout=30)
    run([python, '-m', 'pip', 'check'], timeout=30)


def reuse_compatible_environment(release: Path, fingerprint: dict[str, Any]) -> Path | None:
    """Point directly to a verified physical env; never pip-install through a link."""
    environment = release / 'venv'
    candidates = [environment] if environment.exists() or environment.is_symlink() else []
    candidates.extend(item / 'venv' for item in sorted(RELEASES_ROOT.iterdir())
                      if item != release and SHA_RE.fullmatch(item.name) and not item.is_symlink())
    for candidate in candidates:
        try:
            owner = _verified_environment_owner(candidate)
            if dependency_fingerprint(owner) != fingerprint:
                continue
            attest_dependency_environment(owner / 'venv', fingerprint)
        except (DeployError, OSError, ValueError, KeyError, TypeError):
            continue
        if candidate == environment:
            return environment
        if environment.exists() or environment.is_symlink():
            # Existing noncompatible/incomplete content belongs to the normal installer.
            return None
        environment.symlink_to(owner / 'venv', target_is_directory=True)
        return environment
    if environment.is_symlink():
        raise DeployError('existing dependency symlink is incompatible; refusing to mutate its owner')
    return None


def protect_environment_owners(protected: set[str]) -> set[str]:
    """Current/rollback/process release aliases protect their resolved env owners."""
    owners = set(protected)
    for sha in protected:
        environment = RELEASES_ROOT / sha / 'venv'
        if environment.is_symlink():
            owners.add(_verified_environment_owner(environment).name)
    return owners


def ensure_venv(release: Path) -> Path:
    source = release / "source"
    requirements = source / "backend/requirements.txt"
    if not requirements.is_file():
        raise DeployError("backend requirements are missing from exact release")
    fingerprint = dependency_fingerprint(release)
    reused = reuse_compatible_environment(release, fingerprint)
    if reused is not None:
        return reused
    venv = release / "venv"
    python = _python_312_runtime()

    target_python = venv / "bin/python"
    if not target_python.is_file():
        if venv.exists():
            shutil.rmtree(venv)
        run([python, "-m", "venv", "--without-pip", str(venv)], timeout=180)
    if not target_python.is_file():
        raise DeployError("Python 3.12 venv was not created")

    pip_probe = subprocess.run(
        [str(target_python), "-m", "pip", "--version"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if pip_probe.returncode != 0:
        run([str(target_python), "-m", "ensurepip", "--upgrade"], timeout=180)
    run([str(target_python), str(source / 'scripts/install_live_framework.py')], timeout=180)
    run(
        [
            str(target_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ],
        timeout=900,
    )
    install_ai_resource_control(target_python)
    # Prepare the quiet article browser before restarting the live service.
    try:
        run([str(target_python), '-c',
             'from playwright.sync_api import sync_playwright; from street_story.article_media import browser_executable; '
             'p=sync_playwright().start(); browser_executable(p.chromium.executable_path); p.stop()'],
            env={**os.environ, 'PYTHONPATH': str(source / 'backend')}, timeout=30)
    except DeployError:
        run([str(target_python), '-m', 'playwright', 'install', 'chromium'], timeout=600)
    run(
        [
            str(target_python),
            "-c",
            "import ai_resource_control,fastapi,httpx,live_interaction,pydantic,uvicorn",
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


def verify_limiter_credential(url: str, service_key: str) -> bool:
    """Prove a credential belongs to the canonical limiter without mutating it."""
    endpoint = f"{url.rstrip('/')}/rest/v1/rpc/google_ai_limiter_capabilities"
    request = urllib.request.Request(
        endpoint,
        data=b"{}",
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("limiter_contract") == CANONICAL_GOOGLE_AI_LIMITER_CONTRACT
        and payload.get("quota_scope_dimension")
        == CANONICAL_GOOGLE_AI_QUOTA_SCOPE_DIMENSION
    )


def resolve_limiter_authority(host: Mapping[str, str]) -> tuple[str, str]:
    """Resolve only the verified canonical limiter; never trust generic Supabase URL."""
    dedicated_url = host.get("GOOGLE_AI_LIMITER_SUPABASE_URL", "").strip().rstrip("/")
    dedicated_key = host.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "").strip()
    if bool(dedicated_url) != bool(dedicated_key):
        raise DeployError("dedicated Google AI limiter configuration is incomplete")
    if dedicated_url:
        if dedicated_url != CANONICAL_GOOGLE_AI_LIMITER_URL:
            raise DeployError("dedicated Google AI limiter URL is not canonical")
        if not verify_limiter_credential(dedicated_url, dedicated_key):
            raise DeployError(
                "dedicated Google AI limiter credential failed canonical verification"
            )
        return dedicated_url, dedicated_key

    candidates: list[str] = []
    for name in (
        "PERSONALIZATION_SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_KEY",
    ):
        candidate = host.get(name, "").strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if verify_limiter_credential(CANONICAL_GOOGLE_AI_LIMITER_URL, candidate):
            return CANONICAL_GOOGLE_AI_LIMITER_URL, candidate
    raise DeployError("verified canonical Google AI limiter credential is unavailable")


def configure_provider_env() -> None:
    host = parse_dotenv(HOST_ENV)
    refs: list[str] = []
    for ordinal in range(1, 33):
        name = "GOOGLE_API_KEY" if ordinal == 1 else f"GOOGLE_API_KEY{ordinal}"
        if host.get(name):
            refs.append(name)
    if not refs:
        raise DeployError("registered Google API key pool is unavailable")
    quota_url, quota_key = resolve_limiter_authority(host)
    values = {name: host[name] for name in refs}
    values.update(
        {
            "GEMINI_API_KEY_REFS": json.dumps(refs, separators=(",", ":")),
            "GEMINI_MODEL": "gemini-3.1-flash-lite",
            "GEMINI_FALLBACK_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_WEB_SEARCH_TERTIARY_MODEL": "gemini-3.8-flash",
            "GEMINI_TRANSCRIPTION_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_TRANSCRIPTION_FALLBACK_MODEL": "gemini-3.1-flash-lite",
            "GEMINI_QUOTA_SUPABASE_URL": quota_url.rstrip("/"),
            "GEMINI_QUOTA_SUPABASE_KEY": quota_key,
            "GOOGLE_AI_LIMITER_SUPABASE_URL": quota_url.rstrip("/"),
            "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY": quota_key,
            "AI_RESOURCE_CONTROL_URL": quota_url.rstrip("/"),
            "AI_RESOURCE_CONTROL_SERVICE_KEY": quota_key,
            "AI_RESOURCE_KEY_ENVS": ",".join(refs),
        }
    )
    ledger_id = host.get("AI_RESOURCE_LEDGER_ID", "").strip()
    if ledger_id:
        values["AI_RESOURCE_LEDGER_ID"] = ledger_id
    giga_key = host.get('GIGACHAT_API_KEY') or host.get('STREET_STORY_GIGACHAT_KEY')
    if giga_key and RESEARCH_CA.is_file():
        values['STREET_STORY_GIGACHAT_KEY'] = giga_key
        values['STREET_STORY_GIGACHAT_CA'] = str(RESEARCH_CA)
        values['STREET_STORY_GIGACHAT_SCOPE'] = host.get('GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')
    private_write(PROVIDERS_ENV, render_env(values))


def verify_live_resource_control(venv: Path) -> dict[str, Any]:
    provider_env = parse_dotenv(PROVIDERS_ENV)
    control_url = (
        provider_env.get("AI_RESOURCE_CONTROL_URL", "").strip()
        or provider_env.get("GOOGLE_AI_LIMITER_SUPABASE_URL", "").strip()
    )
    control_key = (
        provider_env.get("AI_RESOURCE_CONTROL_SERVICE_KEY", "").strip()
        or provider_env.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "").strip()
    )
    resource_env = {
        "AI_RESOURCE_CONTROL_URL": control_url,
        "AI_RESOURCE_CONTROL_SERVICE_KEY": control_key,
    }
    expected_ledger = provider_env.get("AI_RESOURCE_LEDGER_ID", "").strip()
    if expected_ledger:
        resource_env["AI_RESOURCE_LEDGER_ID"] = expected_ledger
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        **resource_env,
    }
    program = """
import asyncio
import json
import os
from ai_resource_control import Config, Control

async def main():
    control = Control(Config.from_env("street-story"))
    try:
        capabilities = await control.capabilities()
        rows = await control.request(
            "GET",
            "google_ai_api_keys",
            params={
                "select": "id,quota_scope,is_active",
                "provider": "eq.google",
                "is_active": "eq.true",
                "limit": "1001",
            },
        )
        if not isinstance(rows, list) or len(rows) >= 1001:
            raise RuntimeError("invalid server registry")
        print(json.dumps({
            "contract": capabilities.get("contract"),
            "ledger_id": capabilities.get("ledger_id"),
            "candidate_count": len(rows),
            "acquire": capabilities.get("acquire"),
            "key_material": capabilities.get("key_material"),
            "key_delivery": capabilities.get("key_delivery"),
            "retention": capabilities.get("retention"),
            "local_provider_aliases": len([
                name for name in os.environ
                if name == "GOOGLE_API_KEY" or name.startswith("GOOGLE_API_KEY")
            ]),
        }, separators=(",", ":")))
    finally:
        await control.close()

asyncio.run(main())
"""
    raw = run([str(venv / "bin/python"), "-c", program], env=env, timeout=30)
    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DeployError("shared Live resource preflight returned invalid JSON") from exc
    if (
        not isinstance(result, dict)
        or result.get("contract") != "ai_resource_leases_v1"
        or not isinstance(result.get("ledger_id"), str)
        or not result["ledger_id"]
        or not isinstance(result.get("candidate_count"), int)
        or result["candidate_count"] < 1
        or result.get("acquire") != "server_registry_v2"
        or result.get("key_material") != "supabase_vault_canonical_v1"
        or result.get("key_delivery") != "lease_wrapped_aes256_etm_v1"
        or result.get("retention") != "live_only_27h_lazy_compaction_v1"
        or result.get("local_provider_aliases") != 0
    ):
        raise DeployError("shared Live resource preflight failed")
    if expected_ledger and result["ledger_id"] != expected_ledger:
        raise DeployError("shared Live resource ledger mismatch")
    return {
        "contract": result["contract"],
        "ledger_id": result["ledger_id"],
        "candidate_count": result["candidate_count"],
        "acquire": result["acquire"],
        "key_material": result["key_material"],
        "key_delivery": result["key_delivery"],
        "retention": result["retention"],
        "local_provider_aliases": 0,
    }


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
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Host": VIBE_HTTP_HOST}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if request_key:
        headers["Idempotency-Key"] = request_key
    request = urllib.request.Request(VIBE_BASE_URL + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        code = None
        try:
            raw = exc.read(16_384)
            error_payload = json.loads(raw)
            error_value = error_payload.get("error") if isinstance(error_payload, dict) else None
            candidate = error_value.get("code") if isinstance(error_value, dict) else error_value
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_:-]{1,80}", candidate):
                code = candidate
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        raise VibeHttpError(exc.code, path, code) from None
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
            SELECT DISTINCT p.tenant_id, c.id, d.native_id, d.label
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


def _create_vibe_principal(sha: str) -> tuple[str, str]:
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
        token = str(json.loads(raw)["service_token"]).strip()
    except (ValueError, TypeError, KeyError) as exc:
        raise DeployError("VibePublish principal creation readback is invalid") from exc
    if len(token) < 20:
        raise DeployError("VibePublish principal creation returned an invalid token")
    private_write(VIBE_TOKEN_FILE, token)
    private_write(VIBE_PRINCIPAL_FILE, principal)
    return principal, token


def _vibe_auth_failed(exc: DeployError) -> bool:
    return (
        isinstance(exc, VibeHttpError)
        and exc.status == 401
        and exc.path == "/v1/bootstrap"
        and exc.code == "unauthorized"
    )


def ensure_vibe_principal(sha: str) -> tuple[str, str]:
    for required in (VIBE_PY, VIBE_DB, VIBE_OWNER_TOKEN_FILE):
        if not required.is_file():
            raise DeployError(f"current VibePublish management runtime is unavailable: {required}")

    token_exists = VIBE_TOKEN_FILE.exists()
    principal_exists = VIBE_PRINCIPAL_FILE.exists()
    if token_exists != principal_exists:
        raise DeployError("stored VibePublish principal state is incomplete")

    if token_exists:
        require_mode(VIBE_TOKEN_FILE, 0o600)
        require_mode(VIBE_PRINCIPAL_FILE, 0o600)
        token = VIBE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        principal = VIBE_PRINCIPAL_FILE.read_text(encoding="utf-8").strip()
        if len(token) < 20 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", principal):
            raise DeployError("stored VibePublish principal state is invalid")
        try:
            vibe_request(token, "GET", "/v1/bootstrap")
        except DeployError as exc:
            if not _vibe_auth_failed(exc):
                raise
            principal, token = _create_vibe_principal(sha)
    else:
        principal, token = _create_vibe_principal(sha)

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

def preview_window(sha: str, now: float | None = None) -> tuple[str, str]:
    # VibePublish implicitly replays an identical publish intent for 24 hours.
    # Rotate both the explicit request key and the preview-only content marker
    # every 30 minutes so capability evidence can actually become fresh.
    observed = time.time() if now is None else now
    bucket = int(observed // 1800)
    request_key = f"street-story-deploy-preview-{sha[:20]}-{bucket}"
    marker = f"Street Story deployment preflight {bucket}. Preview only; do not dispatch."
    return request_key, marker


def preview_request_key(sha: str, now: float | None = None) -> str:
    return preview_window(sha, now)[0]


def preview_preflight(token: str, sha: str) -> dict[str, str]:
    request_key, preview_text = preview_window(sha)
    receipt = vibe_request(
        token,
        "POST",
        "/v1/publications",
        body={
            "to": [VIBE_ALIAS],
            "content": {"text": preview_text},
            "mode": "preview",
        },
        request_key=request_key,
    )
    operation_id = str(receipt.get("operation_id") or "")
    if not operation_id:
        raise DeployError("VibePublish preview returned no operation_id")
    deadline = time.monotonic() + 120
    current: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status = vibe_request(token, "GET", f"/v1/operations/{operation_id}")
        receipts = status.get("receipts")
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

    if not current or current.get("dry_run") is not True:
        raise DeployError("VibePublish preview did not prove dry-run semantics")
    if not str(current.get("worker_seen_at") or "").strip():
        raise DeployError("VibePublish preview was not claimed by a worker")
    deliveries = current.get("deliveries")
    matches = [
        row
        for row in deliveries
        if isinstance(row, dict)
        and row.get("destination") == VIBE_ALIAS
        and row.get("provider") == "telegram"
    ] if isinstance(deliveries, list) else []
    if len(matches) != 1:
        raise DeployError("VibePublish preview did not resolve the Telegram target")
    delivery = matches[0]
    if str(delivery.get("state") or "") not in {"needs_approval", "verified"}:
        raise DeployError("VibePublish Telegram preview delivery is not ready")
    if str(delivery.get("observed") or "") != "not_attempted":
        raise DeployError("VibePublish preview unexpectedly reached provider dispatch")
    return {
        "alias": VIBE_ALIAS,
        "status": "supported",
        "reason": "Preview completed with worker/target validation and no provider dispatch",
    }


def write_service_env(device: str, vibe: str, sha: str) -> None:
    private_write(
        SERVICE_ENV,
        render_env(
            {
                "DATA_DIR": str(DATA_ROOT),
                "STREET_STORY_DEVICE_TOKEN": device,
                "VIBEPUBLISH_BASE_URL": VIBE_BASE_URL,
                "VIBEPUBLISH_HTTP_HOST": VIBE_HTTP_HOST,
                "VIBEPUBLISH_BEARER_TOKEN": vibe,
                "STREET_STORY_DEPLOY_SHA": sha,
                "STREET_STORY_TEST_DESTINATION_ALIAS": TEST_VIBE_ALIAS,
                "STREET_STORY_OSM_USER_AGENT": (
                    "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"
                ),
                "WORKER_POLL_SECONDS": "1",
                "PROCESSING_DELAYED_AFTER_SECONDS": "1800",
                "STREET_STORY_RESEARCH_ENDPOINT": "http://127.0.0.1:4097",
                "STREET_STORY_RESEARCH_DIRECTORY": str(RESEARCH_DIRECTORY),
                "STREET_STORY_RESEARCH_MODEL": "mimo-v2.6-flash-free",
                "STREET_STORY_NATIVE_VISION_RESERVE": "true" if RESEARCH_QUALIFICATION.is_file() else "false",
            }
        ),
    )


def qualified_fact_pool_models(text: Mapping[str, Any]) -> list[str]:
    """Opt-in pool qualification; a catalog/recent response is insufficient."""
    if 'extractors' not in text:
        return ['mimo-v2.6-flash-free']
    entries = text['extractors']
    expected = {('gigachat', 'GigaChat-2'), ('opencode', 'mimo-v2.6-flash-free'),
                ('opencode', 'nemotron-3-ultra-free')}
    if not isinstance(entries, list) or len(entries) != 3:
        raise DeployError('fact extractor pool qualification incomplete')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise DeployError('fact extractor pool qualification incomplete')
        pair = (entry.get('provider_id'), entry.get('model_id'))
        if (pair not in expected or pair in seen or not all(entry.get(flag) is True for flag in (
                'semantic_contract_verified', 'source_subject_negative_verified',
                'planned_modality_verified', 'known_claim_reuse_verified'))
                or pair[0] == 'opencode' and entry.get('endpoint') != 'http://127.0.0.1:4097'
                or entry.get('directory') and entry['directory'] != str(RESEARCH_DIRECTORY)):
            raise DeployError('fact extractor pool qualification incomplete')
        seen.add(pair)
    return ['mimo-v2.6-flash-free', 'nemotron-3-ultra-free']


def validate_fact_semantic_pool(caches, evidence, text):
    """Optional background pool; absence preserves foreground Mira review."""
    proof = caches.get('fact-semantic-verification-v1')
    if proof is None:
        return
    routes = proof.get('routes') if isinstance(proof, dict) else None
    if not isinstance(routes, list) or not routes:
        raise DeployError('fact semantic qualification incomplete')
    proofs = {item['path']: item['sha256'] for item in evidence}
    qualified = {(item.get('provider_id'), item.get('model_id')) for item in text.get('extractors', [])}
    expected = {'mimo-v2.6-flash-free', 'nemotron-3-ultra-free'}
    flags = ('schema_verified', 'own_passages_verified', 'qualifier_negative_verified',
             'nearby_duplicate_verified', 'nearby_conflict_verified')
    seen = set()
    for route in routes:
        if not isinstance(route, dict):
            raise DeployError('fact semantic qualification incomplete')
        model = route.get('model_id')
        path = route.get('qualification_receipt')
        if (model not in expected or model in seen or ('opencode', model) not in qualified
                or route.get('provider_id') != 'opencode'
                or route.get('endpoint') != 'http://127.0.0.1:4097'
                or route.get('directory') != str(RESEARCH_DIRECTORY)
                or not all(route.get(flag) is True for flag in flags)
                or not route.get('qualification_sha256')
                or proofs.get(path) != route['qualification_sha256']):
            raise DeployError('fact semantic qualification incomplete')
        report = json.loads(Path(path).read_text())
        receipt = report.get('receipt') or {}
        if (report.get('qualified') is not True or report.get('phase') != 'completed'
                or report.get('model_id') != model or report.get('provider_id') != 'opencode'
                or report.get('endpoint') != route['endpoint']
                or not all(report.get(flag) is True for flag in flags)
                or receipt.get('phase') != 'completed' or receipt.get('model_id') != model):
            raise DeployError('fact semantic qualification receipt incomplete')
        seen.add(model)


def install_research_runtime(release: Path, venv: Path) -> dict[str, Any]:
    """Install only the small scoped profile on the existing OpenCode service.

    Qualification is retained operator evidence, never a model catalog flag.
    This helper performs read-only attestation and seeds verified route metadata;
    it dispatches no inference and creates no server or dependency tree.
    """
    require_mode(RESEARCH_QUALIFICATION, 0o600)
    qualification = json.loads(RESEARCH_QUALIFICATION.read_text())
    evidence = qualification.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise DeployError('research qualification evidence unavailable')
    for item in evidence:
        path = Path(item['path'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise DeployError('research qualification evidence digest mismatch')
    caches = qualification.get('caches') or {}
    native = caches.get('native-vision-verification-v1') or {}
    text = caches.get('research-text-verification-v1') or {}
    required = {'native-vision-verification-v1', 'research-text-verification-v1'}
    if (not required.issubset(caches) or set(caches) - required - {'headless-vision-verification-v1', 'research-vision-verification-v1', 'fact-semantic-verification-v1'}
            or native.get('model') != 'gpt-6-luna' or native.get('transport') != 'native_codex_app_server'
            or native.get('controls') != {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}
            or not native.get('common_acceptance_verified') or text.get('gigachat_model') != 'GigaChat-2'
            or not text.get('semantic_contract_verified')):
        raise DeployError('research semantic qualification incomplete')
    fact_models = qualified_fact_pool_models(text)
    validate_fact_semantic_pool(caches, evidence, text)
    direct = caches.get('headless-vision-verification-v1')
    if direct is not None:
        models = direct.get('models') if isinstance(direct, dict) else None
        if not isinstance(models, list) or not models:
            raise DeployError('direct vision qualification incomplete')
        proofs = {item['path']: item['sha256'] for item in evidence}
        for model in models:
            if (not isinstance(model, dict) or not isinstance(model.get('model'), str)
                    or model.get('transport') != 'gemini_generate_content'
                    or model.get('controls') != {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}
                    or model.get('common_acceptance_verified') is not True
                    or not model.get('qualification_sha256')
                    or proofs.get(model.get('qualification_receipt')) != model['qualification_sha256']):
                raise DeployError('direct vision qualification incomplete')
    optional_vision = caches.get('research-vision-verification-v1')
    if optional_vision is not None:
        proofs = {item['path']: item['sha256'] for item in evidence}
        if (not isinstance(optional_vision, dict)
                or optional_vision.get('model_id') != 'mimo-v2.6-flash-free'
                or optional_vision.get('provider_id') != 'opencode'
                or optional_vision.get('endpoint') != 'http://127.0.0.1:4097'
                or optional_vision.get('positive') != 'match' or optional_vision.get('negative') != 'mismatch'
                or optional_vision.get('pixel_transport_verified') is not True
                or optional_vision.get('common_acceptance_verified') is not True
                or optional_vision.get('image_transport') != 'inline_data_uri_v1'
                or optional_vision.get('image_attachments') != 2
                or not optional_vision.get('qualification_sha256')
                or proofs.get(optional_vision.get('qualification_receipt')) != optional_vision['qualification_sha256']):
            raise DeployError('OpenCode vision qualification incomplete')
    RESEARCH_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = release / 'source'
    guard = source / 'backend/deploy/research_guard.mjs'
    digest = hashlib.sha256(guard.read_bytes()).hexdigest()
    installed_guard = RESEARCH_DIRECTORY / ('research-guard-' + digest + '.mjs')
    if installed_guard.exists() and installed_guard.read_bytes() != guard.read_bytes():
        raise DeployError('research immutable guard changed')
    if not installed_guard.exists():
        private_write(installed_guard, guard.read_text())
    env = {**os.environ, 'PYTHONPATH': str(source / 'backend')}
    program = '''
import asyncio,json,sys
from pathlib import Path
from street_story.shared_devcoveer_research import SharedDevCoveerResearch,scoped_research_config
directory=Path(sys.argv[1]);model='mimo-v2.6-flash-free';models=json.loads(sys.argv[2])
config=scoped_research_config(model,directory=directory)
for selected in models:
 config['provider']['opencode']['models'].update(scoped_research_config(selected,directory=directory)['provider']['opencode']['models'])
profile=directory/'opencode.json'
if profile.exists():
 existing=json.loads(profile.read_text());disabled=existing.pop('mcp',{})
 if existing!=config or not isinstance(disabled,dict) or any(not isinstance(value,dict) or value.get('enabled') is not False for value in disabled.values()):
  raise RuntimeError('existing scoped profile differs; reconcile its active attempts before changing it')
if not profile.exists():
 profile.write_text(json.dumps(config));profile.chmod(0o600)
async def main():
 client=SharedDevCoveerResearch(str(directory),model_id=model)
 effective=await client._request(None,'GET','/config')
 inherited=effective.get('mcp') or {}
 if not isinstance(inherited,dict):
  raise RuntimeError('inherited MCP configuration is invalid')
 disabled={name:{'enabled':False} for name in inherited}
 if disabled and any(not isinstance(value,dict) or value.get('enabled') is not False for value in inherited.values()):
  # Directory-scoped config disables inherited integrations; inference inputs,
  # existing session addresses and shared global MCP settings remain intact.
  await client.shared_backend.request('PATCH','/config',directory=str(directory),payload={'mcp':disabled},timeout=30)
 result=await client._attest(None,'search')
 if len(models)>1:
  for selected in models:
   await SharedDevCoveerResearch(str(directory),model_id=selected)._attest(None,'facts')
 print(json.dumps({'endpoint':client.endpoint,'directory':client.directory,'guard_sha256':result['guard_sha256'],
  'tool_boundary_enforced':result['tool_boundary_enforced'],'search_call_limit':result['search_call_limit']}))
asyncio.run(main())
'''
    attestation = json.loads(run([str(venv / 'bin/python'), '-c', program, str(RESEARCH_DIRECTORY), json.dumps(fact_models)], env=env, timeout=90 if len(fact_models)>1 else 45))
    with sqlite3.connect(DATA_ROOT / 'street-story.sqlite3') as db:
        now = time.time()
        for key, value in caches.items():
            db.execute('INSERT OR REPLACE INTO cache(key,value_json,expires_at,created_at) VALUES(?,?,?,?)',
                       (key, json.dumps(value), now + 30 * 86400, now))
    return {**attestation, 'qualified_native_model': native['model'], 'qualified_text_model': text['gigachat_model'],
            **({'qualified_opencode_vision_model': optional_vision['model_id']} if optional_vision is not None else {}),
            **({'qualified_fact_extractors': [entry['model_id'] for entry in text['extractors']]}
               if 'extractors' in text else {}),
            'qualification_sha256': hashlib.sha256(RESEARCH_QUALIFICATION.read_bytes()).hexdigest(), 'new_inference': False}


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
            "Wants=network-online.target vibepublish.service",
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
            f"ReadWritePaths={STATE_ROOT} {Path('/home/dev/.codex').resolve()}",
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
    if (
        len(safe_rows) != 1
        or safe_rows[0]["alias"] != TEST_VIBE_ALIAS
        or safe_rows[0]["provider"] != "telegram"
        or safe_rows[0]["status"] not in {"supported", "needs_review"}
    ):
        raise DeployError(f"unexpected Street Story destination projection: {safe_rows}")
    return health, {"destinations": safe_rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    expected_sha = args.expected_sha.strip().lower()

    sha, tree_sha = exact_source(expected_sha)
    previous_sha = deployed_release_sha()
    release = materialize_release(sha, tree_sha)
    venv = ensure_venv(release)
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    configure_provider_env()
    resource_preflight = verify_live_resource_control(venv)
    research_preflight = install_research_runtime(release, venv)
    device, token_created = device_token()
    principal, vibe_token = ensure_vibe_principal(sha)
    preflight = preview_preflight(vibe_token, sha)
    write_service_env(device, vibe_token, sha)
    install_service(release, venv)
    status = service_status()
    health, capabilities = verify_runtime(sha, device)
    activate_release_pointer(release)

    # Cleanup runs only after exact-SHA health/capability verification. A failed
    # installation must retain the previous working dependency environment.
    environment_cleanup = prune_release_environments(sha, previous_sha)

    final_status = _tracked_status()
    if final_status.strip():
        raise DeployError("deployment changed the canonical repository checkout")

    receipt = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "branch": BRANCH,
        "release_sha": sha,
        "tree_sha": tree_sha,
        "release_root": str(release),
        "environment_cleanup": environment_cleanup,
        "dependency_environment": {"path": str(venv), "resolved_path": str(venv.resolve()),
                                   "reused": venv.is_symlink()},
        "listener": f"127.0.0.1:{PORT}",
        "service": status,
        "local_health": {
            "ok": health.get("ok") is True,
            "source_sha": health.get("source_sha"),
        },
        "capabilities": capabilities,
        "live_resource_control": resource_preflight,
        "research_preflight": research_preflight,
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
