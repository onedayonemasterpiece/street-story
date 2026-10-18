from __future__ import annotations

import os
import re
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _sha(value: str) -> str | None:
    value = value.strip()
    return value.lower() if _SHA_RE.fullmatch(value) else None


def checkout_source_sha(repo_root: Path | None = None) -> str | None:
    """Return the exact checked-out Git commit without invoking git or exposing secrets."""
    override = _sha(os.getenv("STREET_STORY_DEPLOY_SHA", ""))
    if override:
        return override

    root = repo_root or Path(__file__).resolve().parents[2]
    git_entry = root / ".git"
    try:
        if git_entry.is_file():
            marker = git_entry.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                return None
            raw_git_dir = marker.split(":", 1)[1].strip()
            git_dir = Path(raw_git_dir)
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
        else:
            git_dir = git_entry

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        direct = _sha(head)
        if direct:
            return direct
        if not head.startswith("ref:"):
            return None

        ref = head.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.is_file():
            return _sha(loose.read_text(encoding="utf-8"))

        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                value, _, name = line.partition(" ")
                if name == ref:
                    return _sha(value)
    except OSError:
        return None
    return None
