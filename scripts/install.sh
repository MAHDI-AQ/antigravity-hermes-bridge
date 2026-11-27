#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PY_BIN="$cand"
    break
  fi
done

if [ -z "$PY_BIN" ]; then
  echo "Error: Python >= 3.10 is required." >&2
  exit 1
fi

exec "$PY_BIN" "$SCRIPT_DIR/install.py" "$@"
