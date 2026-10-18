#!/usr/bin/env bash
# Reuse an installed interpreter without copying a venv into this checkout.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${1:?Pass the installed Python executable, then its arguments}"
shift
[[ -x "$PYTHON_BIN" ]] || { echo 'Python executable is unavailable' >&2; exit 2; }
export PYTHONPATH="$ROOT/backend:$ROOT/backend/tools${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
if [[ -n "${CHECK_OUTPUT:-}" ]]; then
  [[ "$CHECK_OUTPUT" == /home/dev/artifacts/* && -d "$CHECK_OUTPUT" ]] || {
    echo 'CHECK_OUTPUT must be an existing central artifact directory' >&2; exit 2;
  }
  export DATA_DIR="$CHECK_OUTPUT/data"
fi
cd "$ROOT/backend"
exec "$PYTHON_BIN" "$@"
