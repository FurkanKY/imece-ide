"""Dataset + snapshot loading/validation for the S1b triage evaluation harness.

Split out of tools/evaluate_decision_triage.py for locality (review round 2):
everything that reads and validates EXTERNAL data lives here —

  - labelled verification-failure fixtures (schema v2, strict);
  - user-supplied prediction snapshots (schema v1, STRICT: exact question
    keys/types/label sets, finite numbers, strict bools, no coercion, no
    secret-looking metadata, and error messages that never echo raw values);
  - building + atomically writing the normalized snapshot document that
    `--snapshot-output` produces for live runs (so a live report becomes
    replayable/tunable offline later).

Nothing here performs I/O beyond reading the files it is given (and writing
only when write_snapshot_file is explicitly asked); no network, no keys.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from decision_runtime.errors import DecisionInputError
from decision_runtime.gate import _guard_pre_existing  # noqa: F401  (re-exported for parity tests)
from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer, ScoreAnswer
from decision_runtime.recorder import decision_result_payload
from decision_runtime.triage import (
    FailureKind,
    TriageAction,
    TriageFacts,
    build_triage_spec,
    extract_error_block,
)

TRIAGE_DECISION_ID = "verification_failure_triage"

FIXTURE_SCHEMA_VERSION = 2
SNAPSHOT_SCHEMA_VERSION = 1

_FAILURE_KIND_VALUES = {kind.value for kind in FailureKind}
_ACTION_VALUES = {action.value for action in TriageAction}
_BASELINE_STATUSES = (None, "pass", "fail", "timeout", "error")
_ORIGINS = ("synthetic", "real_capture")
_REAL_CAPTURE_KEYS = ("cmd", "source", "tool_version", "captured_at")

# The natural action per failure kind (docs/JEV-DESIGN.md Spike S1 action
# table) — used only for legacy-field tolerance in the loader.
KIND_TO_GATE_ACTION = {
    FailureKind.CODE_BUG.value: TriageAction.CONTINUE_FIX_LOOP.value,
    FailureKind.TEST_NEEDS_UPDATE.value: TriageAction.CONTINUE_FIX_LOOP.value,
    FailureKind.MISSING_DEPENDENCY.value: TriageAction.NEEDS_USER.value,
    FailureKind.ENVIRONMENT_OR_TOOLING.value: TriageAction.NEEDS_USER.value,
    FailureKind.FLAKY_OR_TIMEOUT.value: TriageAction.RERUN_VERIFICATION_ONCE.value,
    FailureKind.UNRELATED_PREEXISTING.value: TriageAction.MARK_PRE_EXISTING.value,
}

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_KEY_LIKE_SUBSTRINGS = ("sk-", "api_key", "apikey", "bearer ")
_MAX_METADATA_CHARS = 256
_MAX_NAME_CHARS = 128
_MAX_COMMAND_TOKENS = 32
# Argmax tolerance for snapshot choice answers: absorbed float/renormalization
# noise only (the same scale as the distribution-sum tolerance in
# decision_runtime.models) — NOT a licence for a near-top impostor.
_ARGMAX_TOLERANCE = 1e-6


class EvalDataError(Exception):
    """Invalid fixture set / snapshot (harness exit 4)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvalDataError(message)


def _safe_name(value: Any, *, field: str, context: str, max_chars: int = _MAX_NAME_CHARS) -> str:
    """A bounded identifier safe to echo in error messages (validated BEFORE
    any message could include it)."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_chars
        or _SAFE_NAME_RE.fullmatch(value) is None
    ):
        raise EvalDataError(f"{context}: '{field}' must be a bounded identifier of [A-Za-z0-9._-] (rejected)")
    return value


def _safe_metadata(value: Any, *, field: str, context: str, max_chars: int = _MAX_METADATA_CHARS) -> str:
    """Bounded, single-line metadata that never contains key-like material —
    validated before it can be echoed anywhere (snapshot_meta/report)."""
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise EvalDataError(f"{context}: '{field}' must be a non-empty string of at most {max_chars} characters")
    if any(ord(ch) < 32 for ch in value):
        raise EvalDataError(f"{context}: '{field}' contains control characters (refused)")
    if any(pattern in value.lower() for pattern in _KEY_LIKE_SUBSTRINGS):
        raise EvalDataError(f"{context}: '{field}' looks like it contains key material (refused)")
    return value


def _finite(value: Any, *, field: str, context: str) -> float:
    """A finite float — JSON NaN/Infinity literals are refused here so they can
    never slip through model validation (NaN defeats `abs(sum-1) > tol`)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise EvalDataError(f"{context}: '{field}' must be a finite number")
    return float(value)


# --------------------------- fixture loading ---------------------------


def _load_one_fixture(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalDataError(f"{path.name}: unreadable/invalid JSON ({exc.__class__.__name__})") from exc
    _require(isinstance(data, dict), f"{path.name}: fixture must be a JSON object")
    _require(
        data.get("schema_version") == FIXTURE_SCHEMA_VERSION,
        f"{path.name}: unsupported fixture schema_version (expected {FIXTURE_SCHEMA_VERSION})",
    )
    name = data.get("name")
    _require(isinstance(name, str) and name, f"{path.name}: missing 'name'")
    _require(path.stem == name, f"{path.name}: filename stem must equal 'name'")
    for field in ("tool", "language", "provenance"):
        _require(isinstance(data.get(field), str) and data[field], f"{path.name}: missing/empty '{field}'")
    _require(data.get("origin") in _ORIGINS, f"{path.name}: 'origin' must be one of {list(_ORIGINS)}")
    if data["origin"] == "real_capture":
        capture = data.get("real_capture")
        _require(isinstance(capture, dict), f"{path.name}: origin 'real_capture' requires a real_capture object")
        for key in _REAL_CAPTURE_KEYS:
            _require(
                isinstance(capture.get(key), str) and capture[key],
                f"{path.name}: real_capture.{key} must be a non-empty string",
            )
        blob = json.dumps(capture)
        _require(
            "sk-" not in blob and "api_key" not in blob.lower() and "apikey" not in blob.lower(),
            f"{path.name}: real_capture metadata must not contain key-like material",
        )
    else:
        _require(
            data.get("real_capture") in (None, {}),
            f"{path.name}: origin 'synthetic' must not carry real_capture metadata",
        )
    for field in ("stdout", "stderr"):
        _require(isinstance(data.get(field), str), f"{path.name}: '{field}' must be a string")
    exit_code = data.get("exit_code")
    _require(
        exit_code is None or (isinstance(exit_code, int) and not isinstance(exit_code, bool)),
        f"{path.name}: 'exit_code' must be an int or null",
    )
    _require(isinstance(data.get("timed_out"), bool), f"{path.name}: 'timed_out' must be a boolean")
    changed = data.get("changed_paths")
    _require(
        isinstance(changed, list) and all(isinstance(p, str) and p for p in changed),
        f"{path.name}: 'changed_paths' must be a list of non-empty strings",
    )
    command = data.get("command")
    _require(
        command is None
        or (
            isinstance(command, list)
            and all(isinstance(token, str) and token and len(token) <= 512 for token in command)
            and len(command) <= _MAX_COMMAND_TOKENS
        ),
        f"{path.name}: 'command' must be a list of at most {_MAX_COMMAND_TOKENS} non-empty strings",
    )
    baseline_status = data.get("baseline_status")
    _require(
        baseline_status in _BASELINE_STATUSES,
        f"{path.name}: 'baseline_status' must be one of null|pass|fail|timeout|error",
    )
    baseline_exit_code = data.get("baseline_exit_code")
    _require(
        baseline_exit_code is None
        or (isinstance(baseline_exit_code, int) and not isinstance(baseline_exit_code, bool)),
        f"{path.name}: 'baseline_exit_code' must be an int or null",
    )
    # Tolerate the legacy single-label field from the first fixture set: it
    # pinned rule behaviour only, so it feeds both expectations when the
    # explicit split is absent.
    legacy = data.get("expected_label")
    if legacy is not None:
        data.setdefault("expected_rule_kind", legacy)
        data.setdefault("gold_kind", legacy)
        data.setdefault("expected_rule_action", KIND_TO_GATE_ACTION.get(legacy, ""))
        data.setdefault("expected_gate_action", KIND_TO_GATE_ACTION.get(legacy, ""))
    rule_kind = data.get("expected_rule_kind")
    _require(
        rule_kind in _FAILURE_KIND_VALUES,
        f"{path.name}: 'expected_rule_kind' must be one of {sorted(_FAILURE_KIND_VALUES)}",
    )
    gold_kind = data.get("gold_kind")
    _require(
        gold_kind in _FAILURE_KIND_VALUES,
        f"{path.name}: 'gold_kind' must be one of {sorted(_FAILURE_KIND_VALUES)}",
    )
    data.setdefault("expected_gate_action", data.get("expected_rule_action"))
    for field in ("expected_rule_action", "expected_gate_action", "gold_action"):
        action = data.get(field)
        _require(action in _ACTION_VALUES, f"{path.name}: '{field}' must be one of {sorted(_ACTION_VALUES)}")
    return data


def load_fixtures(fixtures_dir: Path) -> list[dict[str, Any]]:
    """Load and validate every fixture; reject duplicate names (stable order)."""
    if not fixtures_dir.is_dir():
        raise EvalDataError(f"fixtures directory not found: {fixtures_dir}")
    fixtures = [_load_one_fixture(path) for path in sorted(fixtures_dir.glob("*.json"))]
    if not fixtures:
        raise EvalDataError(f"no fixtures found in {fixtures_dir}")
    names = [fixture["name"] for fixture in fixtures]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise EvalDataError(f"duplicate fixture name(s): {', '.join(duplicates)}")
    return fixtures


def fixture_to_facts(fixture: Mapping[str, Any]) -> TriageFacts:
    """Deterministic facts exactly as the real VerificationFailureGate builds
    them: the gate runs extract_error_block(process stdout, stderr) — NOT a
    raw concatenation — and passes the check's actual argv sequence."""
    command = fixture.get("command")
    if not command:
        command = (str(fixture["tool"]).split()[0],)
    return TriageFacts(
        check_id=str(fixture["name"]),
        command=tuple(command),
        exit_code=fixture["exit_code"],
        timed_out=fixture["timed_out"],
        error_block=extract_error_block(fixture["stdout"], fixture["stderr"]),
        changed_paths=tuple(fixture["changed_paths"]),
        baseline_status=fixture["baseline_status"],
        baseline_exit_code=fixture["baseline_exit_code"],
    )


# --------------------------- snapshot loading (strict) ---------------------------


def load_snapshot(snapshot_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse and structurally validate a user-supplied predictions snapshot.

    Strict by design (review round 2): top-level metadata is bounded, single-
    line and key-material-free; the question set must match the triage spec
    exactly; every prediction needs a safe fixture identifier, the exact
    decision id and question-set version, backend/model metadata consistent
    with the top level, strictly-typed latency/tokens/fallback fields — and
    nothing is ever coerced (str()/bool() of malformed values is refused).
    Error messages never echo raw snapshot values.
    """
    try:
        with open(snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalDataError(f"snapshot {snapshot_path}: unreadable/invalid JSON ({exc.__class__.__name__})") from exc
    context = f"snapshot {snapshot_path.name}"
    _require(isinstance(data, dict), f"{context}: must be a JSON object")
    _require(
        data.get("schema_version") == SNAPSHOT_SCHEMA_VERSION,
        f"{context}: schema_version must be {SNAPSHOT_SCHEMA_VERSION}",
    )
    for field in ("backend", "model_version", "captured_at", "source"):
        _safe_metadata(data.get(field), field=field, context=context)
    _safe_metadata(
        data.get("question_set_version"), field="question_set_version", context=context, max_chars=_MAX_NAME_CHARS,
    )
    _require(
        data["question_set_version"] == build_triage_spec(TRIAGE_DECISION_ID).question_set_version,
        f"{context}: question_set_version does not match the triage spec {TRIAGE_DECISION_ID} v1",
    )
    predictions = data.get("predictions")
    _require(
        isinstance(predictions, list) and predictions,
        f"{context}: 'predictions' must be a non-empty list",
    )
    seen: set[str] = set()
    for entry in predictions:
        _require(isinstance(entry, dict), f"{context}: each prediction must be an object")
        fixture_name = _safe_name(entry.get("fixture"), field="fixture", context=context)
        _require(fixture_name not in seen, f"{context}: duplicate prediction for fixture {fixture_name!r}")
        seen.add(fixture_name)
        _require(
            entry.get("decision_id") == TRIAGE_DECISION_ID,
            f"{context}: prediction for {fixture_name!r} has a decision_id other than {TRIAGE_DECISION_ID!r}",
        )
        _require(
            entry.get("question_set_version") == data["question_set_version"],
            f"{context}: prediction for {fixture_name!r} has a question_set_version "
            f"different from the snapshot's",
        )
        for field in ("backend", "model_version"):
            _safe_metadata(entry.get(field), field=field, context=f"{context} prediction {fixture_name!r}")
            _require(
                entry[field] == data[field],
                f"{context}: prediction for {fixture_name!r} has a '{field}' different "
                f"from the snapshot's top-level value",
            )
        _require(
            type(entry.get("fallback_used")) is bool,
            f"{context}: prediction for {fixture_name!r} needs a strict boolean 'fallback_used'",
        )
        for field in ("latency_ms", "prompt_tokens"):
            value = entry.get(field)
            _require(
                type(value) is int and value >= 0,
                f"{context}: prediction for {fixture_name!r} needs a non-negative integer '{field}'",
            )
    return data, predictions


def _validate_snapshot_coverage(
    entries: list[Mapping[str, Any]], fixtures: list[dict[str, Any]], snapshot_path: Path
) -> dict[str, Mapping[str, Any]]:
    by_name = {entry["fixture"]: entry for entry in entries}
    fixture_names = {fixture["name"] for fixture in fixtures}
    unknown = sorted(set(by_name) - fixture_names)
    if unknown:
        raise EvalDataError(f"snapshot {snapshot_path.name}: predictions for unknown fixture(s): {', '.join(unknown)}")
    missing = sorted(fixture_names - set(by_name))
    if missing:
        raise EvalDataError(f"snapshot {snapshot_path.name}: missing prediction(s) for fixture(s): {', '.join(missing)}")
    return by_name


def _reconstruct_answer(name: str, payload: Any, question: Any, context: str) -> ChoiceAnswer | ScoreAnswer | NoulAnswer:
    """Strictly rebuild one answer: exact kind, exact label set from the
    question's own rubric, finite numbers — no coercion, no value echo."""
    _require(isinstance(payload, dict), f"{context} answer {name!r}: must be an object")
    expected_kind = type(question).__name__.lower()
    kind = payload.get("kind")
    _require(
        isinstance(kind, str) and kind == expected_kind,
        f"{context} answer {name!r}: kind does not match the question type ({expected_kind} expected)",
    )
    if expected_kind == "choice":
        probabilities = payload.get("probabilities")
        _require(isinstance(probabilities, dict), f"{context} answer {name!r}: 'probabilities' must be an object")
        for value in probabilities.values():
            _finite(value, field="probability", context=f"{context} answer {name!r}")
        _require(
            set(probabilities) == set(question.criteria),
            f"{context} answer {name!r}: probability labels do not exactly match the question's criteria labels",
        )
        _require(
            isinstance(payload.get("choice"), str) and payload["choice"] in probabilities,
            f"{context} answer {name!r}: 'choice' must be one of the question's criteria labels",
        )
        # The chosen label must BE the distribution's argmax (ties allowed) —
        # same contract the Jev backend enforces on server answers; a chosen
        # label that is not the top of its own claimed distribution is
        # self-contradictory and rejected. Tolerance absorbs float/renorm noise.
        chosen_probability = float(probabilities[payload["choice"]])
        _require(
            chosen_probability + _ARGMAX_TOLERANCE >= max(float(p) for p in probabilities.values()),
            f"{context} answer {name!r}: 'choice' is not the distribution's argmax (refused)",
        )
        _finite(payload.get("confidence"), field="confidence", context=f"{context} answer {name!r}")
        try:
            return ChoiceAnswer(
                choice=payload["choice"],
                probabilities=dict(probabilities),
                confidence=payload["confidence"],
            )
        except (DecisionInputError, KeyError, TypeError) as exc:
            raise EvalDataError(f"{context} answer {name!r}: invalid choice answer ({exc.__class__.__name__})") from exc
    if expected_kind == "score":
        score = _finite(payload.get("score"), field="score", context=f"{context} answer {name!r}")
        _require(
            0.0 <= score <= question.max_score,
            f"{context} answer {name!r}: score falls outside the question's rubric levels",
        )
        probabilities = payload.get("probabilities")
        _require(isinstance(probabilities, dict), f"{context} answer {name!r}: 'probabilities' must be an object")
        expected_keys = {str(level) for level in range(question.max_score + 1)}
        for value in probabilities.values():
            _finite(value, field="probability", context=f"{context} answer {name!r}")
        _require(
            set(probabilities) == expected_keys,
            f"{context} answer {name!r}: probability level keys do not exactly match the question's rubric levels",
        )
        _finite(payload.get("confidence"), field="confidence", context=f"{context} answer {name!r}")
        try:
            return ScoreAnswer(score=score, probabilities=dict(probabilities), confidence=payload["confidence"])
        except (DecisionInputError, KeyError, TypeError) as exc:
            raise EvalDataError(f"{context} answer {name!r}: invalid score answer ({exc.__class__.__name__})") from exc
    noul = _finite(payload.get("noul"), field="noul", context=f"{context} answer {name!r}")
    _require(0.0 <= noul <= 1.0, f"{context} answer {name!r}: 'noul' must be in [0, 1]")
    return NoulAnswer(noul=noul)


def _reconstruct_result(entry: Mapping[str, Any], spec: Any) -> DecisionResult:
    """Rebuild a saved DecisionResult with FULL validation — exact question
    key set (no missing, no extra), strict types, rubric-conformant labels.
    Malformed entries are rejected, never coerced."""
    fixture_name = entry["fixture"]
    context = f"snapshot prediction for {fixture_name!r}"
    raw_answers = entry.get("answers")
    _require(isinstance(raw_answers, dict), f"{context}: 'answers' must be an object")
    missing = sorted(set(spec.questions) - set(raw_answers))
    extra = sorted(set(raw_answers) - set(spec.questions))
    _require(not missing, f"{context}: answers missing question(s) {missing}")
    _require(not extra, f"{context}: answers carry unexpected extra question key(s) (refused)")
    answers = {
        question_name: _reconstruct_answer(question_name, raw_answers[question_name], spec.questions[question_name], context)
        for question_name in spec.questions
    }
    try:
        return DecisionResult(
            decision_id=entry["decision_id"],
            question_set_version=entry["question_set_version"],
            answers=answers,
            backend=entry["backend"],
            model_version=entry["model_version"],
            latency_ms=entry["latency_ms"],
            prompt_tokens=entry["prompt_tokens"],
            fallback_used=entry["fallback_used"],
        )
    except (DecisionInputError, KeyError, TypeError) as exc:
        raise EvalDataError(f"{context}: invalid DecisionResult ({exc.__class__.__name__})") from exc


# --------------------------- snapshot document (for --snapshot-output) ---------------------------


_SNAPSHOT_SOURCE = "imece eval harness live capture (tools/evaluate_decision_triage.py)"


def build_snapshot_document(results_by_name: Mapping[str, DecisionResult], *, captured_at: str | None = None) -> dict[str, Any]:
    """The normalized, replayable snapshot document for a live run: ONLY
    DecisionResult fields (same shape as the canonical decision.made payload)
    plus provenance. Never raw state, never key material."""
    results = [results_by_name[name] for name in sorted(results_by_name)]
    _require(bool(results), "no results to snapshot")
    backends = {result.backend for result in results}
    models = {result.model_version for result in results}
    qsets = {result.question_set_version for result in results}
    if len(backends) != 1 or len(models) != 1 or len(qsets) != 1:
        raise EvalDataError("snapshot build requires consistent backend/model_version/question_set_version across results")
    entries = []
    for name, result in zip(sorted(results_by_name), results):
        entries.append({
            "fixture": name,
            "decision_id": result.decision_id,
            "question_set_version": result.question_set_version,
            "answers": dict(decision_result_payload(result)["answers"]),
            "backend": result.backend,
            "model_version": result.model_version,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.prompt_tokens,
            "fallback_used": result.fallback_used,
        })
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "backend": next(iter(backends)),
        "model_version": next(iter(models)),
        "question_set_version": next(iter(qsets)),
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": _SNAPSHOT_SOURCE,
        "predictions": entries,
    }


def write_snapshot_file(path: Path, document: dict[str, Any], *, overwrite: bool = False) -> None:
    """Write the snapshot document atomically. Default REFUSES an existing
    file (no silent overwrite); the parent directory must already exist. No
    other files are touched."""
    if not path.parent.is_dir():
        raise EvalDataError(f"snapshot output parent directory does not exist: {path.parent}")
    if path.exists() and not overwrite:
        raise EvalDataError(f"snapshot output file already exists (pass --overwrite to replace): {path.name}")
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".eval-snapshot-", dir=str(path.parent))
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            json.dump(document, fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
