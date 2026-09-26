"""Deterministic verification-plan detection for a workspace root.

Order of precedence:

  1. `.imece/verify.json` — explicit user-authored checks. Each entry is
     `{"id": ..., "title": ..., "argv": [...], "timeout_ms": ...}`; `argv`
     is always an explicit argument vector, NEVER a shell command string
     (see process_runtime.models.ProcessRequest, which is constructed with
     `shell=False` semantics throughout process_runtime.runner).
  2. Heuristics, in this fixed order. Several may apply at once (e.g. a
     monorepo with both a Python backend and a JS frontend) — every match
     becomes its own VerificationCheck in the same plan:
       - Python tests (a pytest config file, a `tests/` directory, or any
         `test_*.py` file at the workspace root) -> `python3 -m pytest -q`
         (`python` on Windows, matching the platform choice already made by
         runconfig._PY).
       - `package.json` with a real (non-default) `scripts.test` -> `npm test`.
       - `Cargo.toml` -> `cargo test`.
       - `go.mod` -> `go test ./...`.

Returns None if nothing is detected (neither an explicit file nor any
heuristic matched) — PipelineRunner treats that as "no deterministic
verification is possible for this workspace" (see its module docstring).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from process_runtime.models import ProcessRequest
from verification_runtime.models import VerificationCheck, VerificationPlan

from pipeline_runtime.errors import PipelineInputError

# Matches runconfig.py's platform choice exactly, so a detected verification
# command and a manually-run "project command" never disagree on which
# Python interpreter name is used.
_PY = "python" if os.name == "nt" else "python3"

_DEFAULT_NPM_TEST_SCRIPT = 'echo "Error: no test specified" && exit 1'
_VERIFY_JSON_REL = ".imece/verify.json"
_DEFAULT_TIMEOUT_MS = 300_000


def _new_plan_id() -> str:
    return f"verify_{uuid.uuid4()}"


def _load_explicit_plan(root: Path) -> VerificationPlan | None:
    verify_json = root / _VERIFY_JSON_REL
    if not verify_json.is_file():
        return None
    try:
        data = json.loads(verify_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineInputError(f"Could not parse {_VERIFY_JSON_REL}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise PipelineInputError(f"{_VERIFY_JSON_REL} must be a non-empty JSON list of checks.")

    checks: list[VerificationCheck] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise PipelineInputError(f"{_VERIFY_JSON_REL}[{index}] must be an object.")
        check_id = entry.get("id")
        title = entry.get("title")
        argv = entry.get("argv")
        timeout_ms = entry.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
        if not isinstance(check_id, str) or not check_id:
            raise PipelineInputError(f"{_VERIFY_JSON_REL}[{index}].id must be a non-empty string.")
        if not isinstance(title, str) or not title:
            raise PipelineInputError(f"{_VERIFY_JSON_REL}[{index}].title must be a non-empty string.")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise PipelineInputError(
                f"{_VERIFY_JSON_REL}[{index}].argv must be a non-empty JSON list of strings "
                "(a shell command string is never accepted)."
            )
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise PipelineInputError(f"{_VERIFY_JSON_REL}[{index}].timeout_ms must be an integer.")
        checks.append(
            VerificationCheck(
                check_id, title, ProcessRequest(argv=tuple(argv), timeout_ms=timeout_ms),
            )
        )
    return VerificationPlan(plan_id=_new_plan_id(), checks=tuple(checks))


def _has_pytest_config(root: Path) -> bool:
    if (root / "pytest.ini").is_file():
        return True
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            if "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            pass
    return False


def _has_python_tests(root: Path) -> bool:
    if _has_pytest_config(root):
        return True
    if (root / "tests").is_dir():
        return True
    try:
        next(root.glob("test_*.py"))
        return True
    except StopIteration:
        return False


def _has_real_npm_test_script(root: Path) -> bool:
    package_json = root / "package.json"
    if not package_json.is_file():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return False
    script = scripts.get("test")
    return bool(isinstance(script, str) and script.strip() and script.strip() != _DEFAULT_NPM_TEST_SCRIPT)


def _detect_heuristic_checks(root: Path) -> list[VerificationCheck]:
    checks: list[VerificationCheck] = []
    if _has_python_tests(root):
        checks.append(
            VerificationCheck(
                "python_pytest", "Python tests (pytest)",
                ProcessRequest(argv=(_PY, "-m", "pytest", "-q"), timeout_ms=_DEFAULT_TIMEOUT_MS),
            )
        )
    if _has_real_npm_test_script(root):
        checks.append(
            VerificationCheck(
                "npm_test", "JavaScript/TypeScript tests (npm test)",
                ProcessRequest(argv=("npm", "test"), timeout_ms=_DEFAULT_TIMEOUT_MS),
            )
        )
    if (root / "Cargo.toml").is_file():
        checks.append(
            VerificationCheck(
                "cargo_test", "Rust tests (cargo test)",
                ProcessRequest(argv=("cargo", "test"), timeout_ms=_DEFAULT_TIMEOUT_MS),
            )
        )
    if (root / "go.mod").is_file():
        checks.append(
            VerificationCheck(
                "go_test", "Go tests (go test)",
                ProcessRequest(argv=("go", "test", "./..."), timeout_ms=_DEFAULT_TIMEOUT_MS),
            )
        )
    return checks


def detect_verification_plan(workspace_root: str | Path) -> VerificationPlan | None:
    """Detect a deterministic VerificationPlan for `workspace_root`, or None
    if neither an explicit `.imece/verify.json` nor any heuristic matched."""
    root = Path(workspace_root)
    explicit = _load_explicit_plan(root)
    if explicit is not None:
        return explicit
    checks = _detect_heuristic_checks(root)
    if not checks:
        return None
    return VerificationPlan(plan_id=_new_plan_id(), checks=tuple(checks))
