#!/usr/bin/env bash
# Complete local engineering delivery; no provider/LAN/agent/release activation.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
cd "$ROOT"
ARCHIVE="${IMECE_PACKAGE_ARCHIVE:-ImeceIDE-linux-manual.tar.gz}"
( export LC_ALL=C; [[ "$ARCHIVE" =~ ^ImeceIDE-[a-zA-Z0-9.-]+\.tar\.gz$ ]] ) || { echo 'Invalid archive filename' >&2; exit 2; }
export PYTHONDONTWRITEBYTECODE=1
bash packaging/build.sh "$@"
"$PYTHON" packaging/deliver.py prepare --root "$ROOT" --bundle "$ROOT/dist/ImeceIDE" --platform linux
IMECE_HELPER_SMOKE_REPORT="$ROOT/dist/helper-smoke.json" "$PYTHON" packaging/helper-smoke.py
IMECE_PACKAGE_SMOKE_REPORT="$ROOT/dist/package-smoke.json" node packaging/smoke.mjs
"$PYTHON" packaging/deliver.py archive --root "$ROOT" --bundle "$ROOT/dist/ImeceIDE" --platform linux --smoke-report "$ROOT/dist/package-smoke.json" --helper-smoke-report "$ROOT/dist/helper-smoke.json" --output "$ROOT/dist/$ARCHIVE"
printf '%s\n' "Engineering archive: dist/$ARCHIVE (not a release)"
