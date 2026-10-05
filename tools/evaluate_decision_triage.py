#!/usr/bin/env python3
"""S1b evaluation harness for verification-failure triage (docs/JEV-DESIGN.md "Spike S1").

Modes (the JSON report always states which one ran):

  rule (default)  — replays each labelled fixture through the offline
                    RuleDecisionBackend. No typesafe-sdk import, no API key
                    read, no network — safe to run anywhere.
  snapshot        — replays a USER-SUPPLIED snapshot of saved DecisionResult
                    predictions (the format --snapshot-output writes; see
                    tools/eval_dataset.load_snapshot for the strict schema).
                    The harness never fabricates Jev predictions, latencies
                    or calibration; malformed snapshots are rejected.
  jev             — live mode. Requires ALL of: --backend jev AND
                    --allow-network AND TYPESAFE_API_KEY present in the
                    environment (this harness never reads .env files and
                    never prints the key). It delegates to the
                    JevDecisionBackend adapter (decision_runtime/
                    jev_backend.py), which owns sanitization; only the
                    adapter-returned DecisionResult enters the report — no
                    raw API output, no full state, no keys. A missing key or
                    adapter exits BEFORE any network call; per-fixture backend
                    errors are marked and exit 5 — they are never attributed
                    to the rule backend's predictions.

Release decision vs screening (review round 2):

  go_no_go        — the RELEASE decision is conservatively PENDING, always.
                    This harness can never produce the evidence a "go"
                    requires (a documented live holdout, a real-output
                    dataset, an actual small-LLM comparator, and a MEASURED
                    fix-attempt reduction — not the fixture proxy). It
                    reports the explicit missing_evidence list instead and
                    never emits "go".
  triage_screening — the LIVE numeric criteria from docs/JEV-DESIGN.md
                    (needs_user precision >= 0.9, p95 < 1 s) are reported in
                    their own section, for live runs only, with a
                    conservative minimum stop-support denominator (< 10 stops
                    = insufficient evidence even at precision 1.0).
                    Screening is never a release decision.

Metrics are computed against each fixture's GOLD semantic kind (gold_kind),
never against the algorithm's own labels; expected_rule_kind (what the
deterministic rule backend is expected to say) and expected_gate_action (the
pipeline action after the gate's MARK_PRE_EXISTING safety guard) are recorded
separately in every prediction row, so a rule that cannot know a semantic
class (e.g. test_needs_update) shows up as a low-confidence default instead
of being graded circularly against itself.

Exit codes: 0 ok · 2 config/usage · 3 live prerequisites unmet ·
4 dataset/snapshot validation · 5 live prediction errors.

The harness never mutates prod thresholds: a threshold scan (--act-threshold)
only REPORTS suggested values; --snapshot-output writes ONLY the explicit
user-supplied path (atomically, refusing existing files unless --overwrite).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.eval_dataset import (  # noqa: E402
    EvalDataError,
    FIXTURE_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    TRIAGE_DECISION_ID,
    build_snapshot_document,
    fixture_to_facts,
    load_fixtures,
    load_snapshot,
    write_snapshot_file,
    _reconstruct_result,
    _validate_snapshot_coverage,
)
from tools.eval_metrics import (  # noqa: E402
    accuracy,
    confusion,
    expected_calibration_error,
    multiclass_brier,
    percentile,
    per_class_precision_recall,
    round6,
    safe_ratio,
    stratified_split,
)

from decision_runtime.errors import DecisionBackendError, DecisionInputError  # noqa: E402
from decision_runtime.gate import _guard_pre_existing  # noqa: E402
from decision_runtime.models import DecisionResult  # noqa: E402
from decision_runtime.policy import DEFAULT_ACT_THRESHOLD, DecisionPolicy  # noqa: E402
from decision_runtime.remote_state import sanitize_process_output  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    QUESTION_SET_VERSION,
    FailureKind,
    RuleDecisionBackend,
    TriageAction,
    build_triage_spec,
    build_triage_state,
    decide_triage_action,
)

DEFAULT_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "verification_failures"

SCHEMA_VERSION = 2

NEEDS_USER_GOLD_KINDS = ("missing_dependency", "environment_or_tooling")
FIX_OR_TEST_GOLD_KINDS = ("code_bug", "test_needs_update")

_EXIT_OK = 0
_EXIT_CONFIG = 2
_EXIT_LIVE_PREREQ = 3
_EXIT_DATA = 4
_EXIT_LIVE_ERRORS = 5

_LIVE_ENV_KEY = "TYPESAFE_API_KEY"

_CANDIDATE_GRID = (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
_SPLIT_SALT = "s1b-v1"
_NEEDS_USER_PRECISION_TARGET = 0.9
_P95_LATENCY_TARGET_MS = 1000.0
# Conservative screening floor (review round 2): fewer stops than this can
# show precision 1.0 by luck alone — flagged insufficient, NOT a coverage
# threshold (docs/JEV-DESIGN.md defines no numeric coverage target).
_MIN_SCREENING_STOP_SUPPORT = 10

GATE_MARK_PRE_EXISTING_AUTHORIZED_BASELINES = ("fail",)
"""Ideal gate semantics (docs/JEV-DESIGN.md action table): MARK_PRE_EXISTING is
authorized only when the baseline rerun itself FAILED (status "fail").
Baseline "timeout"/"error" mean the baseline RUN was inconclusive, not that the
failure is pre-existing. decision_runtime.gate._guard_pre_existing (S1b backend
slice) downgrades such MARK_PRE_EXISTING outcomes to continue_fix_loop; this
harness mirrors that guard so its actions match pipeline behaviour, and REPORTS
the downgraded classifications in action_safety_audit."""


class ConfigError(Exception):
    """Bad CLI usage / configuration (exit 2)."""


class LivePrereqError(Exception):
    """Live mode prerequisites unmet — no key, or no adapter (exit 3)."""


# --------------------------- prediction runs ---------------------------


def _prediction_row(fixture: Mapping[str, Any], result: DecisionResult, policy: DecisionPolicy) -> dict[str, Any]:
    """One report row — DecisionResult fields plus the fixture's gold labels.

    Never contains raw backend output, the full state, or any key material:
    only the adapter's own validated DecisionResult fields are mirrored.
    `predicted_rule_action` is the raw decide_triage_action mapping;
    `predicted_action` is the GATE-level action the pipeline actually takes
    (the same MARK_PRE_EXISTING guard VerificationFailureGate applies)."""
    outcome = decide_triage_action(result, policy)
    gate_outcome = _guard_pre_existing(outcome, fixture["baseline_status"])
    gold_kind = fixture["gold_kind"]
    probabilities = result.answers["failure_kind"].probabilities
    return {
        "name": fixture["name"],
        "language": fixture["language"],
        "tool": fixture["tool"],
        "origin": fixture["origin"],
        "baseline_status": fixture["baseline_status"],
        "gold_kind": gold_kind,
        "gold_action": fixture["gold_action"],
        "expected_rule_kind": fixture["expected_rule_kind"],
        "expected_rule_action": fixture["expected_rule_action"],
        "expected_gate_action": fixture["expected_gate_action"],
        "predicted_kind": result.answers["failure_kind"].choice,
        "predicted_confidence": round6(result.confidence_of("failure_kind")),
        "failure_kind_probabilities": {label: round6(p) for label, p in sorted(probabilities.items())},
        "predicted_rule_action": outcome.action.value,
        "predicted_action": gate_outcome.action.value,
        "predicted_action_matches_gold_action": gate_outcome.action.value == fixture["gold_action"],
        "kind_correct_vs_gold": result.answers["failure_kind"].choice == gold_kind,
        "backend": result.backend,
        "model_version": result.model_version,
        "fallback_used": result.fallback_used,
        "latency_ms": result.latency_ms,
        "prompt_tokens": result.prompt_tokens,
    }


def predict_subset(
    fixtures: list[dict[str, Any]],
    *,
    backend: str,
    policy: DecisionPolicy,
    snapshot_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, DecisionResult]]:
    """Evaluate a (sub)set of fixtures with one backend.

    Returns (rows, errors, results_by_name) — results_by_name carries the
    validated DecisionResult per fixture (used by --snapshot-output)."""
    spec = build_triage_spec(TRIAGE_DECISION_ID)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    results_by_name: dict[str, DecisionResult] = {}
    if backend == "rule":
        rule_backend = RuleDecisionBackend()
        for fixture in fixtures:
            state = build_triage_state(fixture_to_facts(fixture))
            try:
                result = rule_backend.decide(spec, state)
            except Exception as exc:  # pragma: no cover — deterministic backend
                errors.append({"fixture": fixture["name"], "error_class": type(exc).__name__})
                continue
            results_by_name[fixture["name"]] = result
            rows.append(_prediction_row(fixture, result, policy))
        return rows, errors, results_by_name
    if backend == "snapshot":
        assert snapshot_by_name is not None
        for fixture in fixtures:
            result = _reconstruct_result(snapshot_by_name[fixture["name"]], spec)
            results_by_name[fixture["name"]] = result
            rows.append(_prediction_row(fixture, result, policy))
        return rows, [], results_by_name
    raise ConfigError(f"predict_subset: unsupported backend {backend!r}")  # pragma: no cover


# --------------------------- live jev mode ---------------------------


def _load_jev_backend():
    """Import the Jev adapter (owned by the backend slice). Never imports
    typesafe-sdk here — that dependency belongs to the adapter module."""
    try:
        from decision_runtime.jev_backend import JevDecisionBackend
    except ImportError as exc:
        raise LivePrereqError(
            "JevDecisionBackend adapter not available (decision_runtime.jev_backend); "
            "S1b live evaluation needs the backend slice. Offline modes are unaffected."
        ) from exc
    return JevDecisionBackend()


def _sanitized_fixture_streams(fixture: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """Redact the FULL raw fixture output/command BEFORE any extraction or
    cropping (the production gate's own order, review-round-3 P1: a long
    quoted credential or private key straddling a later crop boundary is
    redacted as a whole first, so no fragment can survive the cut). Raises
    DecisionBackendError (fail closed) when a text cannot be proven clean —
    the fixture is then a per-fixture prediction_error, never sent remotely
    and never attributed to rule predictions. The fixture dict itself is
    never mutated; the caller uses an immutable copy with the safe fields
    replaced."""
    command = list(fixture.get("command") or (str(fixture["tool"]).split()[0],))
    return sanitize_process_output(fixture["stdout"], fixture["stderr"], command)


def predict_live(
    fixtures: list[dict[str, Any]],
    policy: DecisionPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, DecisionResult]]:
    """Live Jev run. Prerequisites are checked BEFORE any network call; full
    fixture output is redacted BEFORE cropping (fail closed — an
    unsanitizable fixture becomes a prediction_error with no remote call and
    no rule-backend attribution); the adapter owns the rest of the
    sanitization and only its DecisionResult fields enter the report."""
    if not os.environ.get(_LIVE_ENV_KEY):
        raise LivePrereqError(
            f"{_LIVE_ENV_KEY} is not set in the environment (the harness reads only "
            "os.environ — never .env files) and never prints the key."
        )
    backend = _load_jev_backend()
    spec = build_triage_spec(TRIAGE_DECISION_ID)
    rows, errors = [], []
    results_by_name: dict[str, DecisionResult] = {}
    for fixture in fixtures:
        try:
            clean_stdout, clean_stderr, clean_command = _sanitized_fixture_streams(fixture)
        except DecisionBackendError:
            errors.append({"fixture": fixture["name"], "error_class": "DecisionBackendError"})
            continue
        # Immutable copy: only the redacted safe fields change; the original
        # fixture (labels/gold/output data) stays untouched.
        sanitized = {
            **fixture,
            "stdout": clean_stdout,
            "stderr": clean_stderr,
            "command": list(clean_command),
        }
        state = build_triage_state(fixture_to_facts(sanitized))
        try:
            result = backend.decide(spec, state)
        except Exception as exc:
            errors.append({"fixture": fixture["name"], "error_class": type(exc).__name__})
            continue
        results_by_name[fixture["name"]] = result
        rows.append(_prediction_row(fixture, result, policy))
    return rows, errors, results_by_name


# --------------------------- metrics ---------------------------


def needs_user_precision_coverage(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """NEEDS_USER precision (stop-and-ask correctness) and coverage (gold
    environmental classes reached), both vs GOLD semantic kinds. Denominator 0
    is reported as precision/coverage None plus the denominator counts."""
    stop_requests = [row for row in rows if row["predicted_action"] == TriageAction.NEEDS_USER.value]
    gold_env = [row for row in rows if row["gold_kind"] in NEEDS_USER_GOLD_KINDS]
    true_stops = [row for row in stop_requests if row["gold_kind"] in NEEDS_USER_GOLD_KINDS]
    stopped_env = [row for row in gold_env if row["predicted_action"] == TriageAction.NEEDS_USER.value]
    return {
        "precision": round6(safe_ratio(len(true_stops), len(stop_requests))),
        "precision_denominator": len(stop_requests),
        "coverage": round6(safe_ratio(len(stopped_env), len(gold_env))),
        "coverage_denominator": len(gold_env),
        "note": "gold semantic classes " + " | ".join(NEEDS_USER_GOLD_KINDS)
        + "; a wrong stop (needs_user on code_bug/test_needs_update) is worse than a wasted attempt",
    }


def environmental_proxy(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Wasted-fix-avoidance PROXY on the gold environmental set.

    Clearly labelled: derived from labelled fixture ACTIONS, NOT measured real
    fix attempts (no pipeline runs happen in this harness)."""
    gold_env = [row for row in rows if row["gold_kind"] in NEEDS_USER_GOLD_KINDS]
    avoided = [row for row in gold_env if row["predicted_action"] == TriageAction.NEEDS_USER.value]
    missed = [row for row in gold_env if row["predicted_action"] != TriageAction.NEEDS_USER.value]
    gold_fix = [row for row in rows if row["gold_kind"] in FIX_OR_TEST_GOLD_KINDS]
    wrongly_stopped = [row for row in gold_fix if row["predicted_action"] == TriageAction.NEEDS_USER.value]
    reruns = [row for row in rows if row["predicted_action"] == TriageAction.RERUN_VERIFICATION_ONCE.value]
    return {
        "proxy_only_not_measured_real_fix_attempts": True,
        "gold_classes": list(NEEDS_USER_GOLD_KINDS),
        "gold_environmental_count": len(gold_env),
        "avoided_count": len(avoided),
        "avoided_ratio": round6(safe_ratio(len(avoided), len(gold_env))),
        "missed_count": len(missed),
        "wrongly_stopped_count": len(wrongly_stopped),
        "rerun_once_count": len(reruns),
        "note": "avoided = gold environmental failure routed to needs_user instead of the fix loop; "
        "PROXY from labelled fixtures, NOT measured real fix attempts",
    }


def action_safety_audit(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """MARK_PRE_EXISTING is authorized only for baseline 'fail' (ideal gate
    semantics, enforced by decision_runtime.gate._guard_pre_existing since the
    S1b backend slice). Baseline 'timeout'/'error' are inconclusive: backends
    may still CLASSIFY unrelated_preexisting there (recorded as
    predicted_rule_action), but the guard downgrades the pipeline action to
    continue_fix_loop — flagged here, NOT attributed to gold."""
    flagged = [
        row["name"] for row in rows
        if row["predicted_rule_action"] == TriageAction.MARK_PRE_EXISTING.value
        and row["baseline_status"] not in GATE_MARK_PRE_EXISTING_AUTHORIZED_BASELINES
    ]
    row_by_name = {row["name"]: row for row in rows}
    downgraded = [
        name for name in flagged
        if row_by_name[name]["predicted_action"] == TriageAction.CONTINUE_FIX_LOOP.value
    ]
    marked = sum(1 for row in rows if row["predicted_rule_action"] == TriageAction.MARK_PRE_EXISTING.value)
    final_marked = sum(1 for row in rows if row["predicted_action"] == TriageAction.MARK_PRE_EXISTING.value)
    return {
        "authorized_baselines": list(GATE_MARK_PRE_EXISTING_AUTHORIZED_BASELINES),
        "mark_pre_existing_rule_count": marked,
        "mark_pre_existing_gate_count": final_marked,
        "downgraded_by_gate_guard_count": len(downgraded),
        "inconclusive_baseline_fixture_names": flagged,
        "note": "ideal gate semantics: MARK_PRE_EXISTING only for baseline 'fail'; baseline "
        "'timeout'/'error' are inconclusive. The gate guard (decision_runtime.gate."
        "_guard_pre_existing, S1b backend slice) downgrades those to continue_fix_loop and this "
        "harness mirrors it — flagged here for visibility, NOT attributed to gold.",
    }


def latency_token_fallback(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    latencies = [row["latency_ms"] for row in rows]
    tokens = [row["prompt_tokens"] for row in rows]
    fallbacks = sum(1 for row in rows if row["fallback_used"])
    return {
        "latency_ms": {
            "p50": round6(percentile(latencies, 50)),
            "p95": round6(percentile(latencies, 95)),
            "n": len(latencies),
            "note": "observed values only — the only nondeterministic fields in the report",
        },
        "prompt_tokens": {
            "p50": round6(percentile(tokens, 50)),
            "p95": round6(percentile(tokens, 95)),
            "total": sum(tokens),
            "note": "as reported by the backend (the rule backend emits 0)",
        },
        "fallback_ratio": round6(safe_ratio(fallbacks, len(rows))),
    }


def calibration(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """ECE over top-choice confidence vs correctness, plus the multiclass
    DISTRIBUTION Brier score — deliberately two different statistics (docs/
    JEV-DESIGN.md: confidence is a shape statistic, not a probability)."""
    pairs = [(row["predicted_confidence"], bool(row["kind_correct_vs_gold"])) for row in rows]
    ece = expected_calibration_error(pairs)
    for bin_entry in ece["bins"]:
        bin_entry["mean_confidence"] = round6(bin_entry["mean_confidence"])
        bin_entry["accuracy"] = round6(bin_entry["accuracy"])
    ece["value"] = round6(ece["value"])
    brier = multiclass_brier([(row["failure_kind_probabilities"], row["gold_kind"]) for row in rows])
    brier["mean"] = round6(brier["mean"])
    return {
        "ece": ece,
        "brier_multiclass": brier,
        "note": "ECE: top-choice confidence vs correctness; Brier: multiclass distribution "
        "sum_k (p_k - y_k)^2 vs one-hot gold — the two are deliberately not mixed",
    }


# --------------------------- threshold scan ---------------------------


def _select_threshold(candidate_rows: list[dict[str, Any]]) -> tuple[float, bool]:
    """The documented selection rule (review round 2, corrected ordering):

    - qualifiers (tune needs_user precision >= 0.9): MAXIMIZE coverage first;
      ties -> the SMALLER act_threshold.
    - non-qualifiers: maximize precision, then coverage, then smaller
      threshold ("fallback -> max precision").

    Returns (selected_act_threshold, a_qualifier_was_found). Pure ranking —
    nothing is persisted and no prod threshold is mutated."""
    target = _NEEDS_USER_PRECISION_TARGET

    def _key(entry: Mapping[str, Any]):
        precision = entry["tune_needs_user_precision"]
        coverage = entry["tune_needs_user_coverage"] or 0.0
        p = precision if precision is not None else 0.0
        if p >= target:
            return (1, coverage, p, -entry["act_threshold"])
        return (0, p, coverage, -entry["act_threshold"])

    best = max(candidate_rows, key=_key)
    precision = best["tune_needs_user_precision"]
    return best["act_threshold"], precision is not None and precision >= target


def _threshold_scan(
    fixtures: list[dict[str, Any]],
    *,
    backend: str,
    policy_act_threshold: float,
    snapshot_by_name: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Deterministic stratified tune/holdout threshold scan.

    The split is derived from gold semantic kinds + sha256(salt:name) so the
    same fixture can never land in both halves. Candidates are evaluated on
    the TUNE half only; holdout numbers are then REPORTED at the default and
    the chosen threshold. Nothing is persisted — the harness never writes
    files (except an explicit --snapshot-output) and never mutates prod
    config."""
    tune, holdout = stratified_split(
        fixtures, label_key=lambda f: f["gold_kind"], name_key=lambda f: f["name"], salt=_SPLIT_SALT,
    )
    candidates = sorted(set(_CANDIDATE_GRID) | {DEFAULT_ACT_THRESHOLD, policy_act_threshold})
    candidate_rows = []
    for candidate in candidates:
        rows, _, _results = predict_subset(
            tune, backend=backend, policy=DecisionPolicy(act_threshold=candidate), snapshot_by_name=snapshot_by_name,
        )
        stats = needs_user_precision_coverage(rows)
        candidate_rows.append({
            "act_threshold": round6(candidate),
            "tune_needs_user_precision": stats["precision"],
            "tune_needs_user_coverage": stats["coverage"],
        })

    selected, qualifier_found = _select_threshold(candidate_rows)

    def _holdout_at(threshold: float) -> dict[str, Any]:
        rows, _, _results = predict_subset(
            holdout, backend=backend, policy=DecisionPolicy(act_threshold=threshold), snapshot_by_name=snapshot_by_name,
        )
        stats = needs_user_precision_coverage(rows)
        return {"act_threshold": round6(threshold), "precision": stats["precision"], "coverage": stats["coverage"]}

    return {
        "enabled": True,
        "split": {
            "salt": _SPLIT_SALT,
            "stratified_by": "gold_kind",
            "tune_count": len(tune),
            "holdout_count": len(holdout),
            "tune_names": sorted(item["name"] for item in tune),
            "holdout_names": sorted(item["name"] for item in holdout),
            "note": "disjoint by construction (sha256(salt:name) order within each gold-kind stratum); "
            "no same-sample leak between tune and holdout",
        },
        "selection_rule": (
            f"qualifiers (tune needs_user precision >= {_NEEDS_USER_PRECISION_TARGET}): max coverage, "
            "ties -> smaller threshold; non-qualifiers: max precision, then coverage, then smaller threshold"
        ),
        "candidates": candidate_rows,
        "selected_act_threshold": selected,
        "qualifier_found_on_tune": qualifier_found,
        "holdout": {
            "default": _holdout_at(DEFAULT_ACT_THRESHOLD),
            "selected": _holdout_at(selected),
        },
        "note": "reported only — the harness never writes files (except an explicit --snapshot-output) "
        "and never mutates prod thresholds (DecisionPolicy defaults stay 0.9/0.5 unless explicitly "
        "passed on the CLI)",
    }


# --------------------------- screening + release decision ---------------------------


def triage_screening(
    mode_backend: str,
    rows: list[Mapping[str, Any]],
    errors: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """The live numeric criteria from docs/JEV-DESIGN.md — in their OWN
    section, for live runs only, NEVER a release decision. A conservative
    minimum stop-support denominator (< 10) flags small samples insufficient
    even at precision 1.0 (a single correct stop among 99 continue_fix_loop
    must not look like success). Coverage is reported with NO numeric target —
    docs/JEV-DESIGN.md defines none."""
    screening: dict[str, Any] = {
        "applies_to": "live jev backend runs only",
        "targets": {
            "needs_user_precision_min": _NEEDS_USER_PRECISION_TARGET,
            "p95_latency_ms_max": _P95_LATENCY_TARGET_MS,
            "min_stop_support": _MIN_SCREENING_STOP_SUPPORT,
            "coverage_target": None,
        },
        "status": None,
        "observed": None,
        "note": "SCREENING ONLY — meeting these targets is necessary but NOT sufficient for a "
        "release decision; see go_no_go.missing_evidence. The min_stop_support floor is a "
        "conservative anti-small-sample guard (e.g. 1 correct stop among 99 continue is "
        "insufficient evidence), not a coverage threshold.",
    }
    if mode_backend != "jev":
        screening["status"] = "not_applicable_offline"
        return screening
    if errors or not rows:
        screening["status"] = "not_applicable_errors"
        return screening
    stats = needs_user_precision_coverage(rows)
    observed = {
        "needs_user_precision": stats["precision"],
        "stop_support": stats["precision_denominator"],
        "p95_latency_ms": round6(percentile([row["latency_ms"] for row in rows], 95)),
        "needs_user_coverage": stats["coverage"],
        "coverage_note": "reported only — docs/JEV-DESIGN.md defines no numeric coverage target",
    }
    screening["observed"] = observed
    precision = observed["needs_user_precision"]
    p95 = observed["p95_latency_ms"]
    support = observed["stop_support"]
    if support < _MIN_SCREENING_STOP_SUPPORT:
        screening["status"] = "insufficient_stop_support"
        return screening
    if precision is not None and precision >= _NEEDS_USER_PRECISION_TARGET and p95 is not None and p95 < _P95_LATENCY_TARGET_MS:
        screening["status"] = "meets_screening_targets"
        return screening
    screening["status"] = "misses_screening_targets"
    return screening


def release_decision(
    mode_backend: str,
    dataset: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    errors: list[Mapping[str, Any]],
    *,
    scan: Mapping[str, Any] | None,
    snapshot_meta: Mapping[str, Any] | None,
    llm_rows: list[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """The RELEASE decision — conservatively PENDING, always (review round 2).

    A fixture run (even a perfect live one over synthetic data) must never
    produce a "go": shipping requires evidence this harness cannot produce.
    The explicit missing_evidence list says exactly what is absent; the list
    shrinks as real evidence accumulates but can never become empty here
    (measured fix-attempt reduction needs instrumented pipeline runs)."""
    missing: list[str] = []
    if mode_backend != "jev":
        missing.append("live_jev_backend_run")
    if mode_backend == "jev" and errors:
        missing.append("complete_live_run_without_errors")
    if dataset["real_captures"]["count"] == 0:
        missing.append("real_output_dataset")
    holdout_on_live = (
        scan is not None
        and snapshot_meta is not None
        and snapshot_meta.get("backend") == "jev"
    )
    if not holdout_on_live:
        missing.append("holdout_evaluation_on_live_outputs")
    if llm_rows is None:
        missing.append("llm_comparator")
    if mode_backend == "jev" and not errors and rows:
        stats = needs_user_precision_coverage(rows)
        if stats["precision_denominator"] < _MIN_SCREENING_STOP_SUPPORT:
            missing.append("adequate_stop_support_for_screening")
    # This harness can never attest a MEASURED reduction (it runs no pipeline
    # and measures no real fix attempts — the fixture proxy is labelled as such).
    missing.append("measured_fix_attempt_reduction")
    return {
        "status": "pending",
        "reason": "conservatively pending: a labelled-fixture run (offline rule, snapshot replay, "
        "or even a perfect live pass) is not the evidence a release decision requires",
        "missing_evidence": missing,
        "evidence_notes": {
            "live_jev_backend_run": "run --backend jev --allow-network with TYPESAFE_API_KEY set (user-run, never in CI)",
            "real_output_dataset": "fixtures with origin real_capture (reproducible cmd/tool version), not only synthetic scenarios",
            "holdout_evaluation_on_live_outputs": "threshold scan over a replayed snapshot of LIVE outputs "
            "(--snapshot-output during a live run, then --backend snapshot --act-threshold ...)",
            "llm_comparator": "an imported small-LLM comparison snapshot (--llm-snapshot), never generated here",
            "adequate_stop_support_for_screening": f"at least {_MIN_SCREENING_STOP_SUPPORT} stop decisions in the live run "
            "(a tiny denominator can show precision 1.0 by luck)",
            "measured_fix_attempt_reduction": "MEASURED fix-attempt reduction from instrumented pipeline runs "
            "with vs without triage — never derivable from the labelled-fixture proxy",
        },
    }


# --------------------------- report ---------------------------


def dataset_summary(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    def _counts(key: str) -> dict[str, int]:
        counter: dict[str, int] = {}
        for fixture in fixtures:
            value = str(fixture[key])
            counter[value] = counter.get(value, 0) + 1
        return dict(sorted(counter.items()))

    real = [fixture for fixture in fixtures if fixture["origin"] == "real_capture"]
    return {
        "fixture_count": len(fixtures),
        "origin_counts": _counts("origin"),
        "language_counts": _counts("language"),
        "tool_counts": _counts("tool"),
        "gold_kind_counts": _counts("gold_kind"),
        "expected_rule_kind_counts": _counts("expected_rule_kind"),
        "real_captures": {
            "count": len(real),
            "fixture_names": sorted(fixture["name"] for fixture in real),
            "note": (
                "each real fixture's provenance/real_capture records a reproducible cmd, "
                "the producing tool version and the capture date"
                if real
                else "no real captures in this fixture set — all fixtures are synthetic "
                "hand-written scenarios; the report says so instead of implying otherwise"
            ),
        },
    }


def build_report(
    *,
    mode_backend: str,
    policy: DecisionPolicy,
    policy_source: str,
    fixtures: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    snapshot_meta: dict[str, Any] | None = None,
    llm_rows: list[dict[str, Any]] | None = None,
    scan: dict[str, Any] | None = None,
    snapshot_output: str | None = None,
) -> dict[str, Any]:
    gold_labels = sorted({fixture["gold_kind"] for fixture in fixtures} | {row["predicted_kind"] for row in rows})
    acc, denominator = accuracy(rows, "gold_kind", "predicted_kind")
    per_class = per_class_precision_recall(rows, gold_labels, "gold_kind", "predicted_kind")
    for entry in per_class.values():
        entry["precision"] = round6(entry["precision"])
        entry["recall"] = round6(entry["recall"])
    dataset = dataset_summary(fixtures)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "tools/evaluate_decision_triage.py",
        "mode": {
            "backend": mode_backend,
            "offline": mode_backend in ("rule", "snapshot"),
            "network_allowed": mode_backend == "jev",
            **({"snapshot_meta": snapshot_meta} if snapshot_meta else {}),
            **({"snapshot_output": snapshot_output} if snapshot_output else {}),
        },
        "settings": {
            "question_set_version": QUESTION_SET_VERSION,
            "act_threshold": round6(policy.act_threshold),
            "escalate_threshold": round6(policy.escalate_threshold),
            "policy_source": policy_source,
            "note": "reported settings only — the harness never writes files (except an explicit "
            "--snapshot-output) and never mutates prod thresholds",
        },
        "dataset": dataset,
        "predictions": sorted(rows, key=lambda row: row["name"]),
        "prediction_errors": sorted(errors, key=lambda error: error["fixture"]),
        "metrics": {
            "kind_accuracy": round6(acc),
            "kind_accuracy_denominator": denominator,
            "confusion_gold_vs_predicted": confusion(rows, "gold_kind", "predicted_kind"),
            "per_class": per_class,
            "needs_user": needs_user_precision_coverage(rows),
            "environmental_wasted_fix_avoidance": environmental_proxy(rows),
            "calibration": calibration(rows),
            "latency_tokens_fallback": latency_token_fallback(rows),
        },
        "action_safety_audit": action_safety_audit(rows),
        "triage_screening": triage_screening(mode_backend, rows, errors),
        "llm_comparison": (
            {
                "note": "imported snapshot; values are the snapshot's own, never generated by this harness",
                "kind_accuracy": round6(accuracy(llm_rows, "gold_kind", "predicted_kind")[0]),
                "fixture_count": len(llm_rows),
            }
            if llm_rows is not None
            else None
        ),
        "threshold_scan": scan,
        # ALWAYS pending — the release decision is never "go" from a fixture run.
        "go_no_go": release_decision(
            mode_backend, dataset, rows, errors, scan=scan, snapshot_meta=snapshot_meta, llm_rows=llm_rows,
        ),
    }


# --------------------------- CLI ---------------------------


def _policy_from_args(args: argparse.Namespace) -> tuple[DecisionPolicy, str]:
    if args.act_threshold is None:
        return DecisionPolicy(), "default"
    try:
        policy = DecisionPolicy(act_threshold=args.act_threshold)
    except DecisionInputError as exc:
        raise ConfigError(f"invalid --act-threshold: {exc}") from exc
    return policy, f"cli:--act-threshold={round6(args.act_threshold)}"


def _llm_rows(fixtures: list[dict[str, Any]], llm_snapshot_path: Path) -> list[dict[str, Any]]:
    """Imported LLM comparison snapshot — goes through the SAME strict
    reconstruction as every other snapshot (no ad-hoc parsing): values are
    the snapshot's own, validated, never generated by this harness."""
    meta, entries = load_snapshot(llm_snapshot_path)
    by_name = _validate_snapshot_coverage(entries, fixtures, llm_snapshot_path)
    spec = build_triage_spec(TRIAGE_DECISION_ID)
    gold_by_name = {fixture["name"]: fixture["gold_kind"] for fixture in fixtures}
    rows = []
    for name in sorted(by_name):
        result = _reconstruct_result(by_name[name], spec)
        rows.append({
            "name": name,
            "gold_kind": gold_by_name[name],
            "predicted_kind": result.answers["failure_kind"].choice,
        })
    return rows


def _save_snapshot_output(
    results_by_name: Mapping[str, DecisionResult], path: Path, *, overwrite: bool
) -> None:
    """Write the normalized replayable snapshot for a live run — explicit user
    path only, atomic, refuses existing files unless --overwrite."""
    document = build_snapshot_document(results_by_name)
    try:
        write_snapshot_file(path, document, overwrite=overwrite)
    except EvalDataError as exc:
        raise ConfigError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluate_decision_triage",
        description="S1b evaluation harness for verification-failure triage "
        "(offline by default; JSON report on stdout).",
    )
    parser.add_argument(
        "--fixtures-dir",
        default=str(DEFAULT_FIXTURES_DIR),
        help=f"labelled fixture directory (default: {DEFAULT_FIXTURES_DIR})",
    )
    parser.add_argument(
        "--backend",
        choices=("rule", "snapshot", "jev"),
        default="rule",
        help="rule = offline RuleDecisionBackend (default); snapshot = replay saved predictions; "
        "jev = live API run (requires --allow-network AND TYPESAFE_API_KEY)",
    )
    parser.add_argument("--snapshot", help="path to a saved-predictions snapshot (required with --backend snapshot)")
    parser.add_argument(
        "--llm-snapshot",
        help="optional path to an imported LLM-comparison snapshot (reported as imported, never generated)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="opt-in for --backend jev live mode; without it jev mode is rejected",
    )
    parser.add_argument(
        "--act-threshold",
        type=float,
        default=None,
        help="evaluate at this act threshold (DecisionPolicy) and run the stratified tune/holdout "
        "threshold scan; reported only, prod thresholds are never mutated",
    )
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        default=None,
        help="live mode only: write the run's normalized, replayable predictions snapshot to this "
        "explicit path (schema v1, no raw state, no keys); refuses to overwrite unless --overwrite",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow --snapshot-output to replace an existing file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.allow_network and args.backend != "jev":
            raise ConfigError("--allow-network is only meaningful with --backend jev")
        if args.backend == "jev" and not args.allow_network:
            raise ConfigError("live jev mode requires BOTH --backend jev AND --allow-network")
        if args.backend == "snapshot" and not args.snapshot:
            raise ConfigError("--backend snapshot requires --snapshot PATH")
        if args.backend == "rule" and args.snapshot:
            raise ConfigError("--snapshot is only used with --backend snapshot")
        if args.snapshot_output and args.backend != "jev":
            raise ConfigError("--snapshot-output is only available for live jev runs (the rule backend "
                              "is reproducible offline; replayable snapshots are for capturing live output)")
        if args.overwrite and not args.snapshot_output:
            raise ConfigError("--overwrite is only meaningful with --snapshot-output")
        if args.act_threshold is not None and args.backend == "jev":
            raise ConfigError(
                "the threshold scan is supported for offline modes only (rule/snapshot) — "
                "on jev it would multiply live API calls by the candidate count; save a snapshot "
                "with --snapshot-output and scan it offline instead"
            )

        policy, policy_source = _policy_from_args(args)
        fixtures = load_fixtures(Path(args.fixtures_dir))

        snapshot_by_name: dict[str, Mapping[str, Any]] | None = None
        snapshot_meta: dict[str, Any] | None = None
        if args.backend == "snapshot":
            snapshot_path = Path(args.snapshot)
            meta, entries = load_snapshot(snapshot_path)
            snapshot_by_name = _validate_snapshot_coverage(entries, fixtures, snapshot_path)
            snapshot_meta = {
                "backend": meta["backend"],
                "model_version": meta["model_version"],
                "question_set_version": meta["question_set_version"],
                "captured_at": meta["captured_at"],
                "source": meta["source"],
                "path": str(snapshot_path),
            }

        if args.backend == "jev":
            rows, errors, results_by_name = predict_live(fixtures, policy)
        else:
            rows, errors, results_by_name = predict_subset(
                fixtures, backend=args.backend, policy=policy, snapshot_by_name=snapshot_by_name,
            )

        snapshot_output = None
        if args.snapshot_output:
            if errors:
                # A partial/errored live run must not produce a snapshot that
                # could never replay (coverage validation would reject it).
                print(
                    f"note: --snapshot-output skipped — {len(errors)} prediction(s) errored; "
                    "snapshots are only written for complete runs",
                    file=sys.stderr,
                )
            else:
                _save_snapshot_output(results_by_name, args.snapshot_output, overwrite=args.overwrite)
                snapshot_output = str(args.snapshot_output)

        scan = None
        if args.act_threshold is not None:
            scan = _threshold_scan(
                fixtures, backend=args.backend, policy_act_threshold=policy.act_threshold,
                snapshot_by_name=snapshot_by_name,
            )

        llm_rows = _llm_rows(fixtures, Path(args.llm_snapshot)) if args.llm_snapshot else None

        report = build_report(
            mode_backend=args.backend,
            policy=policy,
            policy_source=policy_source,
            fixtures=fixtures,
            rows=rows,
            errors=errors,
            snapshot_meta=snapshot_meta,
            llm_rows=llm_rows,
            scan=scan,
            snapshot_output=snapshot_output,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG
    except EvalDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_DATA
    except LivePrereqError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_LIVE_PREREQ

    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False))
    if errors:
        print(f"error: {len(errors)} prediction(s) errored — see prediction_errors in the report", file=sys.stderr)
        return _EXIT_LIVE_ERRORS
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
