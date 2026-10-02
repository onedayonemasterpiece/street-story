#!/usr/bin/env python3
"""One-time owner migration from ephemeral-signed Street Story builds.

Default mode is backup-only. Destructive migration requires --execute and a
pre-existing token file. The token is never placed on a command line or printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

PACKAGE = "com.onedayonemasterpiece.streetstory"
BACKEND_URL = "https://street-story.kenigevents.ru"
TARGET_TAG = "android-v415"
TARGET_VERSION = "0.1.415"
TARGET_VERSION_CODE = 415
TARGET_URL = (
    "https://github.com/onedayonemasterpiece/street-story/releases/download/"
    f"{TARGET_TAG}/street-story.apk"
)
TARGET_SHA256 = "700e4ea197e0cde67ee7e185641f6cef8a27535ff69fb337245574ba025aa665"
KNOWN_EPHEMERAL_VERSIONS = {"0.1.407", "0.1.410"}
DURABLE_PATHS = (
    "databases/street-story.db",
    "databases/street-story.db-wal",
    "databases/street-story.db-shm",
    "files/stories",
    "files/audio",
    "shared_prefs/street_story_config.xml",
    "shared_prefs/street_story_topics_v1.xml",
    "shared_prefs/street_story_draft_overrides.xml",
    "shared_prefs/street_story_research_projection_v1.xml",
)
SECRET_PREF = "shared_prefs/street_story_secrets.xml"
MAX_APK_BYTES = 250 * 1024 * 1024


class MigrationError(RuntimeError):
    pass


def run(args: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise MigrationError(f"command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}: {stderr[:500]}")
    return result


def text(args: list[str], *, check: bool = True) -> str:
    return run(args, check=check).stdout.decode("utf-8", "replace").strip()


def adb(adb_bin: str, serial: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run([adb_bin, "-s", serial, *args], check=check)


def adb_text(adb_bin: str, serial: str, *args: str, check: bool = True) -> str:
    return adb(adb_bin, serial, *args, check=check).stdout.decode("utf-8", "replace").strip()


def select_device(adb_bin: str, requested: str | None) -> str:
    rows = text([adb_bin, "devices"]).splitlines()[1:]
    devices = [row.split()[0] for row in rows if len(row.split()) >= 2 and row.split()[1] == "device"]
    if requested:
        if requested not in devices:
            raise MigrationError(f"requested ADB device is not ready: {requested}")
        return requested
    if len(devices) != 1:
        raise MigrationError(f"expected exactly one ready ADB device, found {len(devices)}; pass --serial")
    return devices[0]


def package_info(adb_bin: str, serial: str) -> tuple[str | None, int | None]:
    raw = adb_text(adb_bin, serial, "shell", "dumpsys", "package", PACKAGE, check=False)
    version_name = None
    version_code = None
    for line in raw.splitlines():
        value = line.strip()
        if value.startswith("versionName="):
            version_name = value.split("=", 1)[1].strip()
        elif value.startswith("versionCode="):
            match = re.search(r"versionCode=(\d+)", value)
            if match:
                version_code = int(match.group(1))
        if version_name is not None and version_code is not None:
            break
    return version_name, version_code


def run_as(adb_bin: str, serial: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return adb(adb_bin, serial, "shell", "run-as", PACKAGE, *args, check=check)


def exists_in_app(adb_bin: str, serial: str, path: str) -> bool:
    return run_as(adb_bin, serial, "test", "-e", path, check=False).returncode == 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_target(target: Path) -> None:
    if target.is_file() and sha256_file(target) == TARGET_SHA256:
        return
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(TARGET_URL, headers={"User-Agent": "StreetStorySigningMigration/1.0"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_APK_BYTES:
                raise MigrationError("target APK exceeds size limit")
            output.write(chunk)
    if total == 0 or sha256_file(partial) != TARGET_SHA256:
        partial.unlink(missing_ok=True)
        raise MigrationError("target APK SHA-256 mismatch")
    partial.replace(target)


def backend_health() -> None:
    request = urllib.request.Request(BACKEND_URL + "/healthz", headers={"User-Agent": "StreetStorySigningMigration/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read(64 * 1024))
    if response.status != 200 or body.get("ok") is not True:
        raise MigrationError("Street Story backend health check failed")


def validate_token_file(path: Path) -> bytes:
    if not path.is_file():
        raise MigrationError("token file does not exist")
    raw = path.read_bytes()
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise MigrationError("token file must contain UTF-8 text") from exc
    if not (32 <= len(token) <= 256) or any(ch.isspace() for ch in token):
        raise MigrationError("token file does not contain a valid Street Story device token")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise MigrationError("token file must not be readable by group/others (chmod 600)")
    return token.encode("utf-8")


def make_backup(adb_bin: str, serial: str, root: Path) -> tuple[Path, list[str]]:
    adb(adb_bin, serial, "shell", "am", "force-stop", PACKAGE)
    run_as(adb_bin, serial, "pwd")

    paths = [path for path in DURABLE_PATHS if exists_in_app(adb_bin, serial, path)]
    if "databases/street-story.db" not in paths:
        raise MigrationError("durable Street Story database is missing; refusing to uninstall")

    backup = root / "street-story-owner-data.tar"
    with backup.open("wb") as output:
        proc = subprocess.run(
            [adb_bin, "-s", serial, "exec-out", "run-as", PACKAGE, "tar", "-cf", "-", *paths],
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    os.chmod(backup, 0o600)
    if proc.returncode != 0 or backup.stat().st_size == 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise MigrationError(f"ADB backup failed: {stderr[:500]}")

    try:
        with tarfile.open(backup, "r") as archive:
            names = archive.getnames()
    except tarfile.TarError as exc:
        raise MigrationError("backup tar cannot be read back") from exc

    if "databases/street-story.db" not in names:
        raise MigrationError("verified backup does not contain street-story.db")
    if any(name == SECRET_PREF or name.endswith("/street_story_secrets.xml") for name in names):
        raise MigrationError("secret preferences unexpectedly entered the backup")

    (root / "BACKUP_SHA256.txt").write_text(f"{sha256_file(backup)}  {backup.name}\n", encoding="utf-8")
    (root / "BACKUP_CONTENTS.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    return backup, names


def restore_backup(adb_bin: str, serial: str, backup: Path) -> None:
    adb(adb_bin, serial, "shell", "am", "force-stop", PACKAGE)
    run_as(adb_bin, serial, "pwd")
    with backup.open("rb") as source:
        proc = subprocess.run(
            [adb_bin, "-s", serial, "shell", "run-as", PACKAGE, "tar", "-xf", "-"],
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise MigrationError(f"ADB restore failed: {stderr[:500]}")
    run_as(adb_bin, serial, "rm", "-f", SECRET_PREF, check=False)
    if not exists_in_app(adb_bin, serial, "databases/street-story.db"):
        raise MigrationError("restored database is not visible inside the new app sandbox")


def provision(adb_bin: str, serial: str, token: bytes) -> None:
    run_as(adb_bin, serial, "mkdir", "-p", "files")
    proc = subprocess.run(
        [
            adb_bin,
            "-s",
            serial,
            "shell",
            "run-as",
            PACKAGE,
            "sh",
            "-c",
            "cat > files/adb-device-token && chmod 600 files/adb-device-token",
        ],
        input=token,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise MigrationError("failed to stage device token inside the app sandbox")

    adb(
        adb_bin,
        serial,
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        f"{PACKAGE}/.DebugProvisioningActivity",
        "--es",
        "street_story_backend_url",
        BACKEND_URL,
        "--ez",
        "street_story_device_token_staged",
        "true",
    )
    staged_left = run_as(adb_bin, serial, "test", "-e", "files/adb-device-token", check=False).returncode == 0
    if staged_left:
        raise MigrationError("staged plaintext token was not consumed")

    adb(adb_bin, serial, "shell", "pm", "grant", PACKAGE, "android.permission.RECORD_AUDIO", check=False)
    adb(adb_bin, serial, "shell", "pm", "grant", PACKAGE, "android.permission.POST_NOTIFICATIONS", check=False)
    adb(adb_bin, serial, "shell", "am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity")


def masked_serial(serial: str) -> str:
    if len(serial) <= 6:
        return "***"
    return serial[:3] + "…" + serial[-3:]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up an ephemeral-signed Street Story owner install and optionally migrate it to canonical 0.1.415."
    )
    parser.add_argument("--serial", help="ADB serial; required only when more than one ready device is connected")
    parser.add_argument("--execute", action="store_true", help="perform uninstall/install/restore after the verified backup")
    parser.add_argument("--token-file", type=Path, help="0600 file containing the device token; required with --execute")
    parser.add_argument("--backup-dir", type=Path, help="directory to retain backup and verified target APK")
    args = parser.parse_args()

    adb_bin = shutil.which("adb")
    if not adb_bin:
        raise MigrationError("adb is not available on PATH")
    serial = select_device(adb_bin, args.serial)
    backend_health()

    current_name, current_code = package_info(adb_bin, serial)
    if current_name == TARGET_VERSION and current_code == TARGET_VERSION_CODE:
        print(json.dumps({"status": "already_canonical", "version": current_name, "device": masked_serial(serial)}))
        return 0
    if current_name not in KNOWN_EPHEMERAL_VERSIONS:
        raise MigrationError(
            f"expected an affected owner build {sorted(KNOWN_EPHEMERAL_VERSIONS)}, found {current_name or 'no package'}"
        )

    token = None
    if args.execute:
        if args.token_file is None:
            raise MigrationError("--execute requires --token-file before any uninstall is allowed")
        token = validate_token_file(args.token_file)

    root = args.backup_dir or Path.cwd() / (
        "street-story-migration-" + time.strftime("%Y%m%d-%H%M%S")
    )
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    backup, names = make_backup(adb_bin, serial, root)

    target_apk = root / "street-story-0.1.415.apk"
    download_target(target_apk)

    summary = {
        "status": "backup_verified",
        "device": masked_serial(serial),
        "installed_version": current_name,
        "installed_version_code": current_code,
        "backup": str(backup),
        "backup_sha256": sha256_file(backup),
        "backup_entries": len(names),
        "target_version": TARGET_VERSION,
        "target_apk_sha256": TARGET_SHA256,
    }
    print(json.dumps(summary, ensure_ascii=False))

    if not args.execute:
        print("No uninstall performed. Re-run with --execute --token-file <0600-file> after reviewing the backup.")
        return 0

    uninstall = adb_text(adb_bin, serial, "uninstall", PACKAGE)
    if "Success" not in uninstall:
        raise MigrationError("Android did not confirm uninstall")

    install = adb_text(adb_bin, serial, "install", str(target_apk))
    if "Success" not in install:
        raise MigrationError(f"canonical APK installation failed; verified backup remains at {backup}")

    new_name, new_code = package_info(adb_bin, serial)
    if new_name != TARGET_VERSION or new_code != TARGET_VERSION_CODE:
        raise MigrationError(f"unexpected installed target: version={new_name} code={new_code}")

    restore_backup(adb_bin, serial, backup)
    assert token is not None
    provision(adb_bin, serial, token)

    final_name, final_code = package_info(adb_bin, serial)
    print(
        json.dumps(
            {
                "status": "migration_complete",
                "device": masked_serial(serial),
                "version": final_name,
                "version_code": final_code,
                "backup": str(backup),
                "backend": BACKEND_URL,
                "staged_plaintext_removed": True,
                "next_updates": "in-app GitHub Release updater",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"MIGRATION_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
