#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
SKIP_WEB_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --skip-web-build) SKIP_WEB_BUILD=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
if [[ ! -x "$PYTHON" ]]; then
  echo "Python interpreter is not executable: $PYTHON (set PYTHON=/path/to/python)" >&2
  exit 1
fi
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON" -c 'import PyInstaller'
if [[ "$SKIP_WEB_BUILD" -eq 0 ]]; then
  (cd web/ui && npm run build)
fi
"$PYTHON" packaging/check.py sources --root "$ROOT"
"$PYTHON" -m PyInstaller --noconfirm --clean packaging/ImeceIDE.spec
"$PYTHON" packaging/check.py bundle --root "$ROOT" --bundle "$ROOT/dist/ImeceIDE" --platform linux --write-manifest
