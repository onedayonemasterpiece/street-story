#!/usr/bin/env python3
"""Install the same digest-verified immutable Live archive used by Android."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from prepare_live_framework import prepare


def install(root: Path | None = None):
    root = root or Path(__file__).resolve().parents[1]
    prepare(root)  # Checks archive digest, package version and safe native assets.
    lock = json.loads((root / 'live-framework.lock.json').read_text())
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
                    '--no-deps', str(root / lock['archive'])], check=True)


if __name__ == '__main__':
    install()
