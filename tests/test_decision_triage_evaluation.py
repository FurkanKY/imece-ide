"""Tests for the S1b evaluation harness (tools/evaluate_decision_triage.py).

Review round 2 coverage:
  - metric helpers on small known matrices (safe division, ECE, multiclass
    distribution Brier, deterministic disjoint stratified split);
  - threshold-scan selection rule (coverage-primary among qualifying
    candidates, precision-primary fallback, smaller-threshold tie-break);
  - snapshot STRICT validation (exact question keys/types/label sets, finite
    numbers, strict bools, backend/model consistency, no coercion, no secret
    echo in errors) for both --backend snapshot and the --llm-snapshot path;
  - live-mode opt-in (default offline; jev needs BOTH flags AND a key; no
    network in tests — live plumbing via monkeypatched local backends);
  - release decision ALWAYS pending (never "go") with an explicit
    missing_evidence list, incl. one-stop/99-continue cannot look like
    success, live-perfect is not a go, errored/empty stays pending;
  - triage_screening: live-only numeric criteria with the conservative
    min-stop-support floor;
  - --snapshot-output: explicit path, parent must exist, refuses existing
    unless --overwrite, atomic, normalized fields only, replayable;
  - CLI behaviour when invoked directly (exit conditions, JSON report
    determinism except observed latency, clean offline imports).

No test touches the network or uses a real key, and no test hardcodes /tmp
paths (pytest tmp_path only).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer, ScoreAnswer  # noqa: E402
from decision_runtime.policy import DecisionPolicy  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    QUESTION_SET_VERSION,
    RuleDecisionBackend,
    build_triage_spec,
    build_triage_state,
)

TOOL_PATH = REPO_ROOT / "tools" / "evaluate_decision_triage.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "verification_failures"

PENDING = "pending"
TRIAGE_DECISION_ID = "verification_failure_triage"
ALL_KINDS = {
    "code_bug", "test_needs_update", "missing_dependency",
    "environment_or_tooling", "flaky_or_timeout", "unrelated_preexisting",
}
KIND_LABELS = sorted(ALL_KINDS)


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("evaluate_decision_triage", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixtures(harness):
    return harness.load_fixtures(FIXTURES_DIR)


def _rule_rows(harness, fixtures, policy=None):
    rows, errors, results = harness.predict_subset(fixtures, backend="rule", policy=policy or DecisionPolicy())
    return rows, errors, results


@pytest.fixture(scope="module")
def rule_report(harness, fixtures):
    rows, errors, _results = _rule_rows(harness, fixtures)
    return harness.build_report(
        mode_backend="rule", policy=DecisionPolicy(), policy_source="default",
        fixtures=fixtures, rows=rows, errors=errors,
    )


def _live_result(name: str, *, kind: str, confidence: float, latency_ms: int = 123,
                 tokens: int = 456, backend: str = "jev-test", model_version: str = "jev-test-v1",
                 fallback_used: bool = False) -> DecisionResult:
    """A synthetic-but-valid DecisionResult for plumbing tests (the harness
    itself never fabricates; these are explicitly labelled test-only results)."""
    remainder = (1.0 - confidence) / (len(KIND_LABELS) - 1)
    probabilities = {label: (confidence if label == kind else remainder) for label in KIND_LABELS}
    return DecisionResult(
        decision_id=TRIAGE_DECISION_ID,
        question_set_version=QUESTION_SET_VERSION,
        answers={
            "failure_kind": ChoiceAnswer(choice=kind, probabilities=probabilities, confidence=confidence),
            "caused_by_change": NoulAnswer(noul=0.9),
            "fixable_by_agent": ScoreAnswer(score=2.0, probabilities={"0": 0.1, "1": 0.2, "2": 0.7}, confidence=0.9),
        },
        backend=backend,
        model_version=model_version,
        latency_ms=latency_ms,
        prompt_tokens=tokens,
        fallback_used=fallback_used,
    )


class _ScriptedLocalBackend:
    """Local stand-in for the (future/real) JevDecisionBackend adapter — no network."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def decide(self, spec, state):
        self.calls.append((spec, state))
        entry = self._script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _snapshot_document(entries: list[dict], *, backend: str = "rule", model_version: str = "rule-v1",
                       question_set_version: str = QUESTION_SET_VERSION) -> dict:
    return {
        "schema_version": 1,
        "backend": backend,
        "model_version": model_version,
        "question_set_version": question_set_version,
        "captured_at": "2026-10-02T12:00:00Z",
        "source": "test-generated snapshot from RuleDecisionBackend outputs (honest replay fixture)",
        "predictions": entries,
    }


def _rule_snapshot_entries(harness, fixtures: list[dict]) -> list[dict]:
    """Build snapshot entries from the rule backend's REAL outputs — an honest
    replay fixture for testing the snapshot path (never presented as Jev)."""
    backend = RuleDecisionBackend()
    spec = build_triage_spec(TRIAGE_DECISION_ID)
    entries = []
    for fixture in fixtures:
        result = backend.decide(spec, build_triage_state(harness.fixture_to_facts(fixture)))
        payload_kind = result.answers["failure_kind"]
        payload_noul = result.answers["caused_by_change"]
        payload_score = result.answers["fixable_by_agent"]
        entries.append({
            "fixture": fixture["name"],
            "decision_id": result.decision_id,
            "question_set_version": result.question_set_version,
            "answers": {
                "failure_kind": {
                    "kind": "choice",
                    "choice": payload_kind.choice,
                    "probabilities": dict(payload_kind.probabilities),
                    "confidence": payload_kind.confidence,
                },
                "caused_by_change": {"kind": "noul", "noul": payload_noul.noul},
                "fixable_by_agent": {
                    "kind": "score",
                    "score": payload_score.score,
                    "probabilities": dict(payload_score.probabilities),
                    "confidence": payload_score.confidence,
                },
            },
            "backend": result.backend,
            "model_version": result.model_version,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.prompt_tokens,
            "fallback_used": result.fallback_used,
        })
    return entries


# ============================ metric helpers ============================


class TestEvalMetrics:
    def test_safe_division_by_zero_is_none(self):
        from tools.eval_metrics import safe_ratio
        assert safe_ratio(1, 0) is None
        assert safe_ratio(0, 0) is None
        assert safe_ratio(3, 4) == 0.75

    def test_percentile_linear_interpolation(self):
        from tools.eval_metrics import percentile
        assert percentile([], 50) is None
        assert percentile([7.0], 95) == 7.0
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert percentile(values, 50) == pytest.approx(5.5)
        assert percentile(values, 95) == pytest.approx(9.55)

    def test_accuracy_confusion_and_per_class_on_known_matrix(self):
        from tools.eval_metrics import accuracy, confusion, per_class_precision_recall
        items = [
            {"gold": "a", "pred": "a"},
            {"gold": "a", "pred": "b"},
            {"gold": "c", "pred": "a"},
            {"gold": "a", "pred": "a"},
        ]
        assert confusion(items, "gold", "pred") == {"a": {"a": 2, "b": 1}, "c": {"a": 1}}
        assert accuracy(items, "gold", "pred") == (0.5, 4)
        assert accuracy([], "gold", "pred") == (None, 0)
        per_class = per_class_precision_recall(items, ["a", "b", "c"], "gold", "pred")
        assert per_class["a"] == {"support": 3, "predicted_as": 3, "true_positives": 2,
                                  "precision": pytest.approx(2 / 3), "recall": pytest.approx(2 / 3)}
        # safe division: b was predicted once with 0 correct -> precision 0.0,
        # but no gold-b -> recall None; c never predicted -> precision None,
        # recall 0.0 (one gold item, missed)
        assert per_class["b"]["precision"] == 0.0
        assert per_class["b"]["recall"] is None
        assert per_class["c"]["precision"] is None
        assert per_class["c"]["recall"] == 0.0

    def test_ece_known_values(self):
        from tools.eval_metrics import expected_calibration_error
        # 1.0-correct lands in the [0.9, 1.0] bin (|1.0-1.0| = 0), 0.5-wrong in
        # the [0.5, 0.6] bin (|0.0-0.5| = 0.5); weighted by 1/2 each -> 0.25
        ece = expected_calibration_error([(1.0, True), (0.5, False)])
        assert ece["n"] == 2
        assert ece["value"] == pytest.approx(0.25)
        # perfect calibration WITHIN its bins: one confident-correct item only
        assert expected_calibration_error([(1.0, True)])["value"] == pytest.approx(0.0)
        # separated-but-binned: (0.9, True) -> bin [0.9,1.0] diff 0.1; (0.1, False)
        # -> bin [0.1,0.2] diff 0.1; weighted 1/2 each -> 0.1
        separated = expected_calibration_error([(0.9, True), (0.1, False)])
        assert separated["value"] == pytest.approx(0.1)
        assert expected_calibration_error([])["value"] is None

    def test_brier_multiclass_distribution_not_top_label(self):
        from tools.eval_metrics import multiclass_brier
        perfect = multiclass_brier([({"a": 1.0, "b": 0.0}, "a")])
        assert perfect["mean"] == pytest.approx(0.0)
        uniform = multiclass_brier([({"a": 0.5, "b": 0.5}, "a")])
        assert uniform["mean"] == pytest.approx(0.5)  # (0.5-1)^2 + (0.5-0)^2
        # gold missing from the distribution counts p=0 for the true class
        absent = multiclass_brier([({"a": 0.5, "b": 0.5}, "c")])
        assert absent["mean"] == pytest.approx(1.5)
        combined = multiclass_brier([({"a": 1.0, "b": 0.0}, "a"), ({"a": 0.5, "b": 0.5}, "a"), ({"a": 0.5, "b": 0.5}, "c")])
        assert combined["mean"] == pytest.approx((0.0 + 0.5 + 1.5) / 3)
        assert multiclass_brier([])["mean"] is None

    def test_stratified_split_is_deterministic_disjoint_and_stratified(self, fixtures):
        from tools.eval_metrics import stratified_split
        label_key = lambda f: f["gold_kind"]  # noqa: E731
        name_key = lambda f: f["name"]  # noqa: E731
        tune1, holdout1 = stratified_split(fixtures, label_key=label_key, name_key=name_key)
        tune2, holdout2 = stratified_split(fixtures, label_key=label_key, name_key=name_key)
        names1 = [f["name"] for f in tune1] + [f["name"] for f in holdout1]
        names2 = [f["name"] for f in tune2] + [f["name"] for f in holdout2]
        assert names1 == names2  # deterministic across calls
        assert len(names1) == len(set(names1)) == len(fixtures)  # disjoint + complete: no same-sample leak
        assert not ({f["name"] for f in tune1} & {f["name"] for f in holdout1})
        # every gold class with >= 2 members appears in BOTH halves
        for kind in sorted(ALL_KINDS):
            members = [f for f in fixtures if f["gold_kind"] == kind]
            if len(members) < 2:
                continue
            assert len([f for f in tune1 if f["gold_kind"] == kind]) >= 1, kind
            assert len([f for f in holdout1 if f["gold_kind"] == kind]) >= 1, kind


# ============================ threshold-scan selection ============================


class TestThresholdSelection:
    def _candidate(self, threshold, precision, coverage):
        return {"act_threshold": threshold, "tune_needs_user_precision": precision, "tune_needs_user_coverage": coverage}

    def test_coverage_primary_among_qualifiers_counterexample(self, harness):
        """Review counterexample: 97% precision @ 10% coverage vs 92% @ 80%
        — the qualifying candidate with HIGHER COVERAGE must win, even though
        the other has higher precision."""
        candidates = [
            self._candidate(0.8, 0.97, 0.10),
            self._candidate(0.7, 0.92, 0.80),
        ]
        selected, qualifier_found = harness._select_threshold(candidates)
        assert selected == 0.7
        assert qualifier_found is True

    def test_qualifier_beats_higher_precision_non_qualifier(self, harness):
        candidates = [
            self._candidate(0.8, 0.99, 0.05),  # non-qualifier? no: 0.99 >= 0.9 qualifies too
            self._candidate(0.7, 0.91, 0.60),
        ]
        selected, _ = harness._select_threshold(candidates)
        assert selected == 0.7  # both qualify -> coverage decides

    def test_non_qualifiers_fall_back_to_precision_then_coverage(self, harness):
        candidates = [
            self._candidate(0.5, 0.70, 0.90),
            self._candidate(0.6, 0.80, 0.10),
            self._candidate(0.65, 0.80, 0.50),
        ]
        selected, qualifier_found = harness._select_threshold(candidates)
        assert selected == 0.65  # precision tie 0.80 -> coverage 0.50 beats 0.10
        assert qualifier_found is False

    def test_tie_break_prefers_the_smaller_threshold(self, harness):
        candidates = [
            self._candidate(0.9, 0.95, 0.70),
            self._candidate(0.7, 0.95, 0.70),
        ]
        selected, _ = harness._select_threshold(candidates)
        assert selected == 0.7

    def test_no_candidates_at_all_defaults_safely(self, harness):
        selected, qualifier_found = harness._select_threshold(
            [self._candidate(0.9, None, None)],
        )
        assert selected == 0.9
        assert qualifier_found is False


# ============================ dataset metrics (gold, not circular) ============================


class TestRuleReport:
    def test_report_schema_and_mode(self, rule_report):
        report = rule_report
        assert report["schema_version"] == 2
        assert report["generated_by"] == "tools/evaluate_decision_triage.py"
        assert report["mode"] == {"backend": "rule", "offline": True, "network_allowed": False}
        assert report["settings"]["act_threshold"] == 0.9
        assert report["settings"]["escalate_threshold"] == 0.5
        assert report["settings"]["question_set_version"] == QUESTION_SET_VERSION

    def test_dataset_counts_match_fixture_files(self, rule_report, fixtures):
        dataset = rule_report["dataset"]
        assert dataset["fixture_count"] == len(fixtures) == len(list(FIXTURES_DIR.glob("*.json")))
        assert sum(dataset["origin_counts"].values()) == len(fixtures)
        assert set(dataset["origin_counts"]) <= {"synthetic", "real_capture"}
        assert sum(dataset["gold_kind_counts"].values()) == len(fixtures)
        assert set(dataset["gold_kind_counts"]) == ALL_KINDS
        assert "test_needs_update" not in dataset["expected_rule_kind_counts"]

    def test_real_captures_are_counted_and_honest(self, rule_report, fixtures):
        expected_reals = sorted(f["name"] for f in fixtures if f["origin"] == "real_capture")
        captures = rule_report["dataset"]["real_captures"]
        assert captures["count"] == len(expected_reals)
        assert captures["fixture_names"] == expected_reals
        note = captures["note"]
        if expected_reals:
            assert "no real captures" not in note
        else:
            assert "no real captures" in note

    def test_kind_metrics_are_computed_against_gold_not_algorithm_labels(self, rule_report):
        """Validation-circularity guard: per_class support counts come from
        gold_kind (semantic truth), which is NOT the classifier's own label
        distribution — e.g. test_needs_update has gold support but no rule
        prediction source."""
        per_class = rule_report["metrics"]["per_class"]
        assert per_class["test_needs_update"]["support"] > 0
        assert per_class["test_needs_update"]["predicted_as"] == 0  # rules can never predict it
        assert per_class["test_needs_update"]["recall"] == 0.0
        dataset = rule_report["dataset"]
        for kind, counts in per_class.items():
            assert counts["support"] == dataset["gold_kind_counts"][kind]

    def test_predictions_carry_gold_and_rule_expectations_separately(self, rule_report):
        rows = rule_report["predictions"]
        assert len(rows) == rule_report["dataset"]["fixture_count"]
        divergent = [row for row in rows if row["gold_kind"] != row["expected_rule_kind"]]
        assert divergent, "the semantic-gap fixtures must exist in the report"
        for row in divergent:
            assert row["kind_correct_vs_gold"] == (row["predicted_kind"] == row["gold_kind"])
        # no raw output / state / key material leaks into prediction rows
        forbidden = {"stdout", "stderr", "error_block", "state", "changed_paths", "api_key"}
        assert not forbidden & set(rows[0])

    def test_predicted_action_is_gate_level_and_rule_action_is_raw(self, rule_report):
        """Rows carry BOTH levels: predicted_rule_action (decide_triage_action)
        and predicted_action (after the gate's MARK_PRE_EXISTING guard)."""
        rows = rule_report["predictions"]
        assert all(row["predicted_action"] != "mark_pre_existing" or row["baseline_status"] == "fail"
                   for row in rows), "the guard must veto mark_pre_existing on inconclusive baselines"
        assert any(row["predicted_rule_action"] == "mark_pre_existing"
                   and row["predicted_action"] == "continue_fix_loop" for row in rows), (
            "the guard downgrade scenario must be visible in the data"
        )

    def test_needs_user_precision_coverage_math(self, rule_report):
        rows = rule_report["predictions"]
        stops = [row for row in rows if row["predicted_action"] == "needs_user"]
        gold_env = [row for row in rows if row["gold_kind"] in ("missing_dependency", "environment_or_tooling")]
        true_stops = [row for row in stops if row["gold_kind"] in ("missing_dependency", "environment_or_tooling")]
        stopped_env = [row for row in gold_env if row["predicted_action"] == "needs_user"]
        needs_user = rule_report["metrics"]["needs_user"]
        assert needs_user["precision_denominator"] == len(stops)
        assert needs_user["coverage_denominator"] == len(gold_env) > 0
        assert needs_user["precision"] == round(len(true_stops) / len(stops), 6) if stops else needs_user["precision"] is None
        assert needs_user["coverage"] == round(len(stopped_env) / len(gold_env), 6)

    def test_environmental_proxy_is_labelled_not_measured(self, rule_report):
        proxy = rule_report["metrics"]["environmental_wasted_fix_avoidance"]
        assert proxy["proxy_only_not_measured_real_fix_attempts"] is True
        assert "NOT measured real fix attempts" in proxy["note"]
        assert proxy["gold_environmental_count"] == proxy["avoided_count"] + proxy["missed_count"]

    def test_action_safety_audit_flags_inconclusive_baselines(self, rule_report):
        audit = rule_report["action_safety_audit"]
        rows = rule_report["predictions"]
        flagged_expected = sorted(
            row["name"] for row in rows
            if row["predicted_rule_action"] == "mark_pre_existing"
            and row["baseline_status"] != "fail"
        )
        assert audit["inconclusive_baseline_fixture_names"] == flagged_expected
        raw_marked = [row["name"] for row in rows if row["predicted_rule_action"] == "mark_pre_existing"]
        assert audit["mark_pre_existing_rule_count"] == len(raw_marked)
        # the gate guard downgrades EVERY inconclusive mark to continue_fix_loop
        assert audit["downgraded_by_gate_guard_count"] == len(flagged_expected)
        assert audit["mark_pre_existing_gate_count"] == (
            audit["mark_pre_existing_rule_count"] - audit["downgraded_by_gate_guard_count"]
        )
        assert audit["authorized_baselines"] == ["fail"]
        # this scenario genuinely exists in the fixture set (contradictory
        # remote/baseline safety cases), so the audit has something to show:
        assert audit["mark_pre_existing_rule_count"] > 0
        assert audit["downgraded_by_gate_guard_count"] > 0

    def test_calibration_sections_present(self, rule_report):
        calibration = rule_report["metrics"]["calibration"]
        assert calibration["ece"]["n"] == len(rule_report["predictions"])
        assert calibration["ece"]["value"] is not None
        assert len(calibration["ece"]["bins"]) == 10
        brier = calibration["brier_multiclass"]
        assert brier["n"] == len(rule_report["predictions"])
        assert 0.0 <= brier["mean"] <= 6.0  # 6-class worst case
        assert "not mixed" in calibration["note"]

    def test_rule_mode_latency_tokens_are_observed_not_fabricated(self, rule_report):
        latency = rule_report["metrics"]["latency_tokens_fallback"]
        assert latency["latency_ms"]["n"] == len(rule_report["predictions"])
        assert latency["latency_ms"]["p50"] is not None
        assert latency["latency_ms"]["p95"] >= latency["latency_ms"]["p50"]
        assert latency["prompt_tokens"]["total"] == 0  # the rule backend emits no tokens
        assert latency["fallback_ratio"] == 0.0

    def test_report_json_is_serializable_and_key_free(self, rule_report):
        text = json.dumps(rule_report, sort_keys=True, ensure_ascii=False)
        assert "sk-" not in text
        # no secret-shaped JSON keys (prose may legitimately name the env var)
        assert "api_key\":" not in text.lower() and "apikey\":" not in text.lower()
        assert "password" not in text.lower()

    def test_safe_division_when_no_needs_user_predictions(self, harness, fixtures):
        """A threshold so high nothing acts -> needs_user precision/coverage None (not 0)."""
        policy = DecisionPolicy(act_threshold=1.0)
        rows, _errors, _results = _rule_rows(harness, fixtures, policy)
        stats = harness.needs_user_precision_coverage(rows)
        assert stats["precision"] is None
        assert stats["precision_denominator"] == 0
        assert stats["coverage"] == 0.0  # coverage still computable (0 stops over N gold)
        assert stats["coverage_denominator"] > 0


# ============================ release decision + screening ============================


class TestReleaseDecisionAndScreening:
    def test_rule_mode_release_is_pending_with_missing_evidence(self, rule_report):
        go_no_go = rule_report["go_no_go"]
        assert go_no_go["status"] == PENDING
        missing = go_no_go["missing_evidence"]
        assert "live_jev_backend_run" in missing
        assert "holdout_evaluation_on_live_outputs" in missing
        assert "llm_comparator" in missing
        assert "measured_fix_attempt_reduction" in missing  # never attestable by this harness
        assert "real_output_dataset" not in missing  # the fixture set has 5 real captures
        assert "meets" not in json.dumps(go_no_go)  # a pending decision never implies a pass

    def test_screening_not_applicable_offline(self, rule_report):
        screening = rule_report["triage_screening"]
        assert screening["status"] == "not_applicable_offline"
        assert screening["applies_to"] == "live jev backend runs only"
        assert "NOT sufficient" in screening["note"]

    def test_one_stop_99_continue_cannot_look_like_success(self, harness, fixtures, monkeypatch, capsys):
        """The exact false-ship scenario from the review: precision 1.0 from a
        SINGLE correct stop while everything else continues — screening must
        flag insufficient stop support, and the release decision stays pending."""
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")

        def make_result(fixture):
            if fixture["name"] == "pytest_missing_module_requests":
                return _live_result(fixture["name"], kind="missing_dependency", confidence=0.95)
            return _live_result(fixture["name"], kind="code_bug", confidence=0.95)

        scripted = [make_result(fixture) for fixture in fixtures]
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _ScriptedLocalBackend(scripted))
        assert harness.main(["--backend", "jev", "--allow-network"]) == 0
        report = json.loads(capsys.readouterr().out)
        screening = report["triage_screening"]
        assert screening["observed"]["stop_support"] == 1
        assert screening["observed"]["needs_user_precision"] == 1.0
        assert screening["status"] == "insufficient_stop_support"
        go_no_go = report["go_no_go"]
        assert go_no_go["status"] == PENDING
        assert "adequate_stop_support_for_screening" in go_no_go["missing_evidence"]
        assert "measured_fix_attempt_reduction" in go_no_go["missing_evidence"]

    def test_live_perfect_run_screening_meets_but_release_never_go(self, harness, fixtures, monkeypatch, capsys):
        """A PERFECT live run over the (mostly synthetic) fixture set meets the
        docs' numeric screening targets — and the release decision STILL says
        pending, because screening is not evidence."""
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        scripted = [
            _live_result(fixture["name"], kind=fixture["gold_kind"], confidence=0.95)
            for fixture in fixtures
        ]
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _ScriptedLocalBackend(scripted))
        assert harness.main(["--backend", "jev", "--allow-network"]) == 0
        report = json.loads(capsys.readouterr().out)
        screening = report["triage_screening"]
        assert screening["status"] == "meets_screening_targets"
        assert screening["observed"]["needs_user_precision"] == 1.0
        assert screening["observed"]["stop_support"] >= 10
        assert screening["observed"]["p95_latency_ms"] <= 1000.0
        go_no_go = report["go_no_go"]
        assert go_no_go["status"] == PENDING  # never "go" — synthetic-fixture data is not evidence
        missing = go_no_go["missing_evidence"]
        assert "holdout_evaluation_on_live_outputs" in missing
        assert "llm_comparator" in missing
        assert "measured_fix_attempt_reduction" in missing
        assert "real_output_dataset" not in missing  # 5 real captures exist
        assert "live_jev_backend_run" not in missing
        assert "adequate_stop_support_for_screening" not in missing

    def test_live_wrong_predictions_screening_misses_and_release_pending(self, harness, fixtures, monkeypatch, capsys):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        scripted = [
            _live_result(fixture["name"], kind=fixture["gold_kind"], confidence=0.95, latency_ms=1500)
            for fixture in fixtures
        ]
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _ScriptedLocalBackend(scripted))
        assert harness.main(["--backend", "jev", "--allow-network"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["triage_screening"]["status"] == "misses_screening_targets"  # p95 1500 > 1000
        assert report["go_no_go"]["status"] == PENDING

    def test_live_errors_screening_not_applicable_and_release_pending(self, harness, fixtures, monkeypatch, capsys):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")

        class _FailingBackend:
            def decide(self, spec, state):
                raise RuntimeError("boom")

        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _FailingBackend())
        assert harness.main(["--backend", "jev", "--allow-network"]) == 5
        report = json.loads(capsys.readouterr().out)
        assert report["triage_screening"]["status"] == "not_applicable_errors"
        go_no_go = report["go_no_go"]
        assert go_no_go["status"] == PENDING
        assert "complete_live_run_without_errors" in go_no_go["missing_evidence"]
        assert "measured_fix_attempt_reduction" in go_no_go["missing_evidence"]

    def test_release_decision_never_says_go_across_modes(self, harness, fixtures):
        """Parametrized guard: the release decision is pending in every mode,
        including a hypothetically perfect live run."""
        rows, errors, _results = _rule_rows(harness, fixtures)
        dataset = harness.dataset_summary(fixtures)
        base = dict(scan=None, snapshot_meta=None, llm_rows=None)
        rule_decision = harness.release_decision("rule", dataset, rows, errors, **base)
        assert rule_decision["status"] == PENDING
        perfect = [_live_result(f["name"], kind=f["gold_kind"], confidence=0.95) for f in fixtures]
        perfect_rows = [harness._prediction_row(f, r, DecisionPolicy()) for f, r in zip(fixtures, perfect)]
        live_decision = harness.release_decision("jev", dataset, perfect_rows, [], **base)
        assert live_decision["status"] == PENDING
        errored_decision = harness.release_decision("jev", dataset, [], [{"fixture": "x", "error_class": "RuntimeError"}], **base)
        assert errored_decision["status"] == PENDING


# ============================ threshold scan ============================


class TestThresholdScan:
    def test_scan_split_is_deterministic_and_disjoint(self, harness, fixtures, capsys):
        rc = harness.main(["--act-threshold", "0.85"])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        scan = report["threshold_scan"]
        assert scan["enabled"] is True
        split = scan["split"]
        tune, holdout = set(split["tune_names"]), set(split["holdout_names"])
        assert split["tune_count"] + split["holdout_count"] == len(fixtures)
        assert not (tune & holdout), "same sample must never be in tune and holdout"
        assert len(tune) > 0 and len(holdout) > 0
        assert scan["candidates"][0]["act_threshold"] == 0.5
        thresholds = [candidate["act_threshold"] for candidate in scan["candidates"]]
        assert 0.85 in thresholds and 0.9 in thresholds
        assert scan["selected_act_threshold"] in thresholds
        assert scan["holdout"]["default"]["act_threshold"] == 0.9
        assert scan["holdout"]["selected"]["act_threshold"] == scan["selected_act_threshold"]
        assert report["settings"]["act_threshold"] == 0.85
        assert report["settings"]["policy_source"].startswith("cli")
        # the corrected selection rule is documented in the report itself
        assert "max coverage" in scan["selection_rule"]

    def test_scan_is_reported_only_and_never_mutates_prod(self, harness, fixtures):
        """No file writes anywhere: the harness has no write paths; assert the
        policy defaults are untouched after a scan and that the report says so."""
        assert DecisionPolicy().act_threshold == 0.9  # prod default unchanged
        scan = harness._threshold_scan(
            fixtures, backend="rule", policy_act_threshold=0.85, snapshot_by_name=None,
        )
        assert "never mutates prod thresholds" in scan["note"]
        assert DecisionPolicy().act_threshold == 0.9  # still unchanged after the scan

    def test_scan_honest_about_unmet_target(self, harness, fixtures):
        """The rule backend cannot reach the 0.9 needs_user precision target on
        the adversarial gold set — the scan must SAY that, not fake a pass."""
        scan = harness._threshold_scan(
            fixtures, backend="rule", policy_act_threshold=0.9, snapshot_by_name=None,
        )
        assert scan["qualifier_found_on_tune"] is False

    def test_scan_is_rejected_for_live_mode(self, harness):
        assert harness.main(["--backend", "jev", "--allow-network", "--act-threshold", "0.85"]) == 2

    def test_scan_replays_snapshot_without_network(self, harness, fixtures, tmp_path, capsys):
        snapshot_path = tmp_path / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(_snapshot_document(_rule_snapshot_entries(harness, fixtures))), encoding="utf-8",
        )
        rc = harness.main(["--backend", "snapshot", "--snapshot", str(snapshot_path), "--act-threshold", "0.85"])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["threshold_scan"]["enabled"] is True
        assert report["mode"]["offline"] is True
        assert report["mode"]["network_allowed"] is False


# ============================ snapshot strict validation ============================


class TestSnapshotMode:
    @pytest.fixture()
    def snapshot_file(self, harness, fixtures, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(_snapshot_document(_rule_snapshot_entries(harness, fixtures))), encoding="utf-8")
        return path

    def _main_rc(self, harness, snapshot_path):
        return harness.main(["--backend", "snapshot", "--snapshot", str(snapshot_path)])

    def _mutated(self, snapshot_file: Path, mutate) -> Path:
        document = json.loads(snapshot_file.read_text(encoding="utf-8"))
        mutate(document)
        snapshot_file.write_text(json.dumps(document), encoding="utf-8")
        return snapshot_file

    def test_valid_snapshot_roundtrips(self, harness, fixtures, snapshot_file, capsys):
        rc = self._main_rc(harness, snapshot_file)
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["mode"]["backend"] == "snapshot"
        assert report["mode"]["offline"] is True
        assert report["mode"]["snapshot_meta"]["backend"] == "rule"  # honest: this snapshot IS rule data
        assert report["go_no_go"]["status"] == PENDING  # snapshot replay is never "truly live"
        assert len(report["predictions"]) == len(fixtures)

    def test_missing_snapshot_file_is_data_error(self, harness, tmp_path):
        assert self._main_rc(harness, tmp_path / "nope.json") == 4

    def test_snapshot_missing_fixture_prediction_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"].pop())
        assert self._main_rc(harness, path) == 4

    def test_snapshot_duplicate_fixture_entry_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"].append(dict(d["predictions"][0])))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_unknown_fixture_name_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].update(fixture="not_a_real_fixture"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_unsafe_fixture_name_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].update(fixture="bad name; drop table"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_top_question_set_version_mismatch_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d.update(question_set_version="verification_failure_triage.v0"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_entry_question_set_version_mismatch_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["question_set_version"] = "verification_failure_triage.v0"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_entry_question_set_version_missing_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].pop("question_set_version"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_wrong_decision_id_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].update(decision_id="some_other_decision"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_entry_backend_mismatch_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["backend"] = "some-other-backend"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_entry_model_version_mismatch_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["model_version"] = "other-model"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_entry_backend_missing_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].pop("backend"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_fallback_used_string_false_is_rejected_not_coerced(self, harness, fixtures, snapshot_file):
        """bool("false") is True in Python — the strict loader must reject the
        string instead of coercing it."""
        def mutate(document):
            document["predictions"][0]["fallback_used"] = "false"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_fallback_used_missing_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].pop("fallback_used"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_fallback_used_int_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].update(fallback_used=0))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_latency_missing_rejected_never_fabricated(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].pop("latency_ms"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_tokens_missing_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].pop("prompt_tokens"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_latency_float_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0].update(latency_ms=12.5))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_malformed_probabilities_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(
            snapshot_file, lambda d: d["predictions"][0]["answers"]["failure_kind"].update(probabilities={"code_bug": 0.5}),
        )
        assert self._main_rc(harness, path) == 4

    def test_snapshot_probabilities_wrong_label_set_rejected(self, harness, fixtures, snapshot_file):
        """Exact label-set check: an unknown or missing label is refused even
        when the distribution sums to 1."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["failure_kind"]
            answer["probabilities"] = {"code_bug": 1.0, "nonexistent_kind": 0.0}
            answer["choice"] = "code_bug"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_unknown_choice_label_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            answer = document["predictions"][0]["answers"]["failure_kind"]
            answer["choice"] = "nonexistent_kind"
            answer["probabilities"] = {**answer["probabilities"], "nonexistent_kind": answer["probabilities"]["code_bug"]}
            answer["probabilities"]["code_bug"] = 0.0

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_score_infinity_rejected(self, harness, fixtures, snapshot_file):
        """JSON Infinity/NaN literals parse in Python — finiteness is checked
        explicitly so they can never slip through model validation."""
        def mutate(document):
            document["predictions"][0]["answers"]["fixable_by_agent"]["score"] = float("inf")

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_score_nan_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["answers"]["fixable_by_agent"]["score"] = float("nan")

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_score_outside_rubric_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["answers"]["fixable_by_agent"]["score"] = 7.5

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_score_probability_labels_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            answer = document["predictions"][0]["answers"]["fixable_by_agent"]
            answer["probabilities"] = {"0": 1.0, "1": 0.0}  # missing level "2"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_noul_nan_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["answers"]["caused_by_change"]["noul"] = float("nan")

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_answers_missing_question_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d["predictions"][0]["answers"].pop("caused_by_change"))
        assert self._main_rc(harness, path) == 4

    def test_snapshot_extra_answer_question_rejected(self, harness, fixtures, snapshot_file):
        """Extra question keys are discarded by lazy loaders — here they are
        refused (the strict schema has no silent truncation)."""
        def mutate(document):
            document["predictions"][0]["answers"]["extra_question"] = {"kind": "noul", "noul": 0.5}

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_answer_kind_mismatch_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["predictions"][0]["answers"]["caused_by_change"] = {
                "kind": "choice", "choice": "code_bug",
                "probabilities": {"code_bug": 1.0}, "confidence": 0.9,
            }

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_key_like_metadata_rejected_without_echoing_secret(self, harness, fixtures, snapshot_file, capsys):
        """A snapshot whose metadata carries key material is refused AND the
        error text must never contain the secret."""
        def mutate(document):
            document["backend"] = "sk-really-looks-like-a-key"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        assert "sk-really-looks-like-a-key" not in capsys.readouterr().err

    def test_snapshot_key_like_source_rejected_without_echoing_secret(self, harness, fixtures, snapshot_file, capsys):
        secret = "TYPESAFE_API_KEY=hunter2"

        def mutate(document):
            document["source"] = f"oops {secret}"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr().err
        assert "hunter2" not in captured
        assert secret not in captured

    def test_snapshot_control_char_metadata_rejected(self, harness, fixtures, snapshot_file):
        def mutate(document):
            document["model_version"] = "model\ninjected-log-line"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4

    def test_snapshot_unknown_top_level_metadata_never_echoed(self, harness, fixtures, snapshot_file, capsys):
        """Arbitrary extra top-level keys are ignored — and never echoed into
        the report (so a stashed secret cannot leak through snapshot_meta)."""
        leak = "some-unrelated-secret-value"

        def mutate(document):
            document["user_note"] = leak

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 0
        report = json.loads(capsys.readouterr().out)
        assert leak not in json.dumps(report)

    def test_snapshot_schema_version_rejected(self, harness, fixtures, snapshot_file):
        path = self._mutated(snapshot_file, lambda d: d.update(schema_version=99))
        assert self._main_rc(harness, path) == 4

    # S1b bug regressions: model-level validation failures inside
    # _reconstruct_answer/_reconstruct_result must surface as EvalDataError
    # (exit 4, safe error text) — never as a NameError from the missing
    # DecisionInputError import in the except tuples.

    def test_snapshot_choice_probability_sum_rejected(self, harness, snapshot_file, capsys):
        """Exact full label set, valid argmax and confidence — but the
        distribution sums to 0.9: model validation must reject (exit 4)."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["failure_kind"]
            answer["probabilities"] = {label: 0.15 for label in KIND_LABELS}

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "NameError" not in captured.err

    def test_snapshot_choice_probability_out_of_range_rejected(self, harness, snapshot_file, capsys):
        """Exact full label set summing to exactly 1.0 — but one probability
        is >1 and its balancing one is <0: unit-interval validation rejects."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["failure_kind"]
            probabilities = {label: 0.0 for label in KIND_LABELS}
            probabilities["code_bug"] = 1.5
            probabilities["environment_or_tooling"] = -0.5
            answer["probabilities"] = probabilities
            answer["choice"] = "code_bug"

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "NameError" not in captured.err

    def test_snapshot_choice_confidence_out_of_range_rejected(self, harness, snapshot_file, capsys):
        """Valid full-set distribution — confidence 1.5 must be rejected by
        the model, not crash the loader."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["failure_kind"]
            probabilities = {label: 0.0 for label in KIND_LABELS}
            probabilities["code_bug"] = 1.0
            answer["probabilities"] = probabilities
            answer["choice"] = "code_bug"
            answer["confidence"] = 1.5

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "NameError" not in captured.err

    def test_snapshot_score_probability_sum_rejected(self, harness, snapshot_file, capsys):
        """Correct full level keys {"0","1","2"} — but the distribution sums
        to 1.2: model validation must reject (exit 4)."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["fixable_by_agent"]
            answer["probabilities"] = {"0": 0.4, "1": 0.4, "2": 0.4}

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "NameError" not in captured.err

    def test_snapshot_score_probability_out_of_range_rejected(self, harness, snapshot_file, capsys):
        """Correct level keys summing to exactly 1.0 — but one level is >1
        and another <0: unit-interval validation rejects."""
        def mutate(document):
            answer = document["predictions"][0]["answers"]["fixable_by_agent"]
            answer["probabilities"] = {"0": 1.5, "1": -0.5, "2": 0.0}

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "NameError" not in captured.err

    def test_snapshot_score_confidence_out_of_range_rejected(self, harness, snapshot_file, capsys):
        def mutate(document):
            answer = document["predictions"][0]["answers"]["fixable_by_agent"]
            answer["probabilities"] = {"0": 0.1, "1": 0.2, "2": 0.7}
            answer["confidence"] = 1.5

        path = self._mutated(snapshot_file, mutate)
        assert self._main_rc(harness, path) == 4
        captured = capsys.readouterr()
        assert "NameError" not in captured.err

    def test_reconstruct_result_invalid_metadata_rejected_not_nameerror(self, harness, fixtures):
        """Direct seam: DecisionResult-level metadata validation (a negative
        latency_ms) must surface as EvalDataError — not a NameError raised
        while evaluating the except tuple's missing DecisionInputError."""
        entry = _rule_snapshot_entries(harness, fixtures)[0]
        entry["latency_ms"] = -1
        with pytest.raises(harness.EvalDataError) as excinfo:
            harness._reconstruct_result(entry, build_triage_spec(TRIAGE_DECISION_ID))
        assert "NameError" not in str(excinfo.value)


# ============================ live opt-in (no network in tests) ============================


class TestLiveOptIn:
    def test_default_mode_is_offline_rule(self, harness, capsys):
        assert harness.main([]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["mode"]["backend"] == "rule"
        assert report["mode"]["offline"] is True
        assert report["mode"]["network_allowed"] is False

    def test_jev_without_allow_network_is_config_error(self, harness):
        assert harness.main(["--backend", "jev"]) == 2

    def test_allow_network_without_jev_is_config_error(self, harness):
        assert harness.main(["--allow-network"]) == 2

    def test_snapshot_output_only_for_live_mode(self, harness):
        assert harness.main(["--snapshot-output", "x.json"]) == 2  # rule mode
        assert harness.main(["--backend", "snapshot", "--snapshot", "missing.json",
                             "--snapshot-output", "x.json"]) == 2

    def test_overwrite_only_with_snapshot_output(self, harness):
        assert harness.main(["--overwrite"]) == 2

    def test_jev_without_key_never_imports_or_calls_anything(self, harness, monkeypatch, capsys):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        called = []

        def _boom():
            called.append(True)
            raise AssertionError("adapter must not be loaded before the key check")

        monkeypatch.setattr(harness, "_load_jev_backend", _boom)
        assert harness.main(["--backend", "jev", "--allow-network"]) == 3
        assert called == []
        captured = capsys.readouterr()
        assert "TYPESAFE_API_KEY" in captured.err
        assert captured.out == ""  # no report printed for a prereq failure

    def test_jev_with_key_but_missing_adapter_is_live_prereq(self, harness, monkeypatch, capsys):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")  # not a real secret
        monkeypatch.setattr(
            harness, "_load_jev_backend",
            lambda: (_ for _ in ()).throw(harness.LivePrereqError("adapter missing")),
        )
        assert harness.main(["--backend", "jev", "--allow-network"]) == 3
        assert capsys.readouterr().out == ""

    def test_jev_plumbing_uses_adapter_results(self, harness, fixtures, monkeypatch, capsys):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        scripted = [
            _live_result(fixture["name"], kind=fixture["gold_kind"], confidence=0.95)
            for fixture in fixtures
        ]
        backend = _ScriptedLocalBackend(scripted)
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: backend)
        assert harness.main(["--backend", "jev", "--allow-network"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["mode"]["backend"] == "jev"
        assert report["mode"]["offline"] is False
        assert report["mode"]["network_allowed"] is True
        assert len(backend.calls) == len(fixtures)
        assert all(spec.decision_id == TRIAGE_DECISION_ID for spec, _ in backend.calls)
        assert all(row["backend"] == "jev-test" for row in report["predictions"])
        # no raw state in the report
        assert "error_block" not in report["predictions"][0]
        # latencies/tokens come from the (test) backend's results, not invented by the harness
        assert {row["latency_ms"] for row in report["predictions"]} == {123}
        assert {row["prompt_tokens"] for row in report["predictions"]} == {456}

    def test_no_typesafe_or_dotenv_import_in_offline_default(self):
        """The CLI, invoked directly, must import the repository cleanly and
        NEVER import typesafe-sdk or a .env reader in the offline default."""
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(REPO_ROOT / 'tools')!r});"
            "import evaluate_decision_triage as h;"
            "rc = h.main([]);"
            "bad = [m for m in sys.modules if 'typesafe' in m.lower() or 'dotenv' in m.lower() or m == 'httpx'];"
            "print('RC', rc, 'BAD', bad)"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "RC 0 BAD []" in result.stdout


# ============================ --snapshot-output (live capture) ============================


class TestSnapshotOutput:
    def _live_main(self, harness, fixtures, monkeypatch, out_path, *extra):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        scripted = [
            _live_result(fixture["name"], kind=fixture["gold_kind"], confidence=0.95)
            for fixture in fixtures
        ]
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _ScriptedLocalBackend(scripted))
        return harness.main(["--backend", "jev", "--allow-network", "--snapshot-output", str(out_path), *extra])

    def test_live_run_saves_replayable_snapshot(self, harness, fixtures, monkeypatch, tmp_path, capsys):
        out_path = tmp_path / "live-snapshot.json"
        assert self._live_main(harness, fixtures, monkeypatch, out_path) == 0
        document = json.loads(out_path.read_text(encoding="utf-8"))
        assert document["schema_version"] == 1
        assert document["backend"] == "jev-test"
        assert document["model_version"] == "jev-test-v1"
        assert document["question_set_version"] == QUESTION_SET_VERSION
        assert document["captured_at"]  # controlled provenance timestamp
        assert "harness-test-fake-key" not in json.dumps(document)  # no key material
        entry = document["predictions"][0]
        assert set(entry) == {
            "fixture", "decision_id", "question_set_version", "answers",
            "backend", "model_version", "latency_ms", "prompt_tokens", "fallback_used",
        }
        assert set(entry["answers"]) == {"failure_kind", "caused_by_change", "fixable_by_agent"}
        assert "error_block" not in json.dumps(document) and "state" not in entry
        # report mentions the output path
        report = json.loads(capsys.readouterr().out)
        assert report["mode"]["snapshot_output"] == str(out_path)
        # THE WORKFLOW CONNECTION: the saved file replays through snapshot mode
        replay_rc = harness.main(["--backend", "snapshot", "--snapshot", str(out_path)])
        assert replay_rc == 0

    def test_snapshot_output_refuses_existing_file(self, harness, fixtures, monkeypatch, tmp_path):
        out_path = tmp_path / "existing.json"
        out_path.write_text('{"previous": true}', encoding="utf-8")
        assert self._live_main(harness, fixtures, monkeypatch, out_path) == 2
        assert json.loads(out_path.read_text(encoding="utf-8")) == {"previous": True}

    def test_snapshot_output_overwrite_flag_replaces(self, harness, fixtures, monkeypatch, tmp_path):
        out_path = tmp_path / "existing.json"
        out_path.write_text('{"previous": true}', encoding="utf-8")
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        scripted = [_live_result(f["name"], kind=f["gold_kind"], confidence=0.95) for f in fixtures]
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _ScriptedLocalBackend(scripted))
        assert harness.main(["--backend", "jev", "--allow-network", "--snapshot-output",
                             str(out_path), "--overwrite"]) == 0
        document = json.loads(out_path.read_text(encoding="utf-8"))
        assert "predictions" in document

    def test_snapshot_output_parent_must_exist(self, harness, fixtures, monkeypatch, tmp_path):
        out_path = tmp_path / "missing-dir" / "snapshot.json"
        assert self._live_main(harness, fixtures, monkeypatch, out_path) == 2
        assert not out_path.exists()

    def test_snapshot_output_not_written_when_predictions_error(self, harness, fixtures, monkeypatch, tmp_path):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")

        class _FailingBackend:
            def decide(self, spec, state):
                raise RuntimeError("boom")

        monkeypatch.setattr(harness, "_load_jev_backend", lambda: _FailingBackend())
        out_path = tmp_path / "should-not-exist.json"
        assert harness.main(["--backend", "jev", "--allow-network", "--snapshot-output", str(out_path)]) == 5
        assert not out_path.exists(), "partial/errored runs must not produce a replayable snapshot"


# ============================ live redaction (P1: sanitize BEFORE crop) ============================


class TestLiveRedaction:
    """predict_live must redact the FULL fixture output BEFORE extraction/
    cropping: a long quoted credential or private key straddling the 4000-char
    crop boundary loses its assignment prefix in a raw crop, so a surviving
    fragment could never be redacted downstream. The production gate calls
    sanitize_process_output first; the eval's live path must too."""

    SECRET = "correct-horse-" + "x" * 5000 + "-tailtoken99"

    def _fixtures_with_stdout(self, harness, fixtures, poisoned_stdout):
        """A copy of the fixture list with ONE fixture's stdout poisoned —
        the original fixture dicts are never mutated."""
        base = next(f for f in fixtures if f["name"] == "pytest_assertion_error")
        poisoned = {**base, "stdout": poisoned_stdout}
        assert "stdout" in base and poisoned is not base
        return [poisoned], base

    def _scripted(self, fixtures):
        return [
            _live_result(f["name"], kind=f["gold_kind"], confidence=0.9, latency_ms=100, tokens=10)
            for f in fixtures
        ]

    def test_long_quoted_assignment_redacted_before_crop(self, harness, fixtures, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        # Assignment at the END (inside the extractor's last-40-lines tail),
        # secret ~5000 chars so the 4000-char crop cuts INSIDE the value —
        # under a crop-first order the surviving fragment could never be
        # redacted (the `PASSWORD="` prefix is gone); with sanitize-first the
        # whole value is [REDACTED] before any cropping. stderr emptied so the
        # extracted tail is exactly the poisoned region.
        stdout = "filler line\n" * 300 + 'PASSWORD="' + self.SECRET + '"'
        base = next(f for f in fixtures if f["name"] == "pytest_assertion_error")
        poisoned = {**base, "stdout": stdout, "stderr": ""}
        original = base
        backend = _ScriptedLocalBackend(self._scripted([poisoned]))
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: backend)
        rows, errors, results = harness.predict_live([poisoned], DecisionPolicy())
        assert errors == []
        assert len(backend.calls) == 1
        (_spec, state), = backend.calls
        received = json.dumps(state)
        assert "tailtoken99" not in received      # no tail fragment
        assert "correct-horse" not in received    # no head fragment
        assert "x" * 200 not in received          # no mid fragment
        assert "[REDACTED]" in state["error_block"]
        # the INPUT fixture data is unmutated
        assert original["stdout"] == next(
            f for f in fixtures if f["name"] == original["name"]
        )["stdout"]
        assert poisoned["stdout"].count("tailtoken99") == 1  # input copy still has it (originals untouched)
        assert results[original["name"]] is not None

    def test_private_key_block_redacted_before_crop(self, harness, fixtures, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        pem_body = "".join(f"keyline{i:04d}abcdefghijklmnop\n" for i in range(120))
        stdout = (
            "-----BEGIN PRIVATE KEY-----\n" + pem_body + "-----END PRIVATE KEY-----\n"
            + ("filler line\n" * 300)
        )
        poisoned, original = self._fixtures_with_stdout(harness, fixtures, stdout)
        backend = _ScriptedLocalBackend(self._scripted(poisoned))
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: backend)
        rows, errors, _results = harness.predict_live(poisoned, DecisionPolicy())
        assert errors == []
        (_spec, state), = backend.calls
        received = json.dumps(state)
        assert "PRIVATE KEY" not in received
        assert "keyline0000" not in received and "keyline0119" not in received

    def test_unterminated_quoted_assignment_rejected_no_remote_call_no_raw(self, harness, fixtures, monkeypatch, capsys):
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        stdout = "filler line\n" * 300 + 'PASSWORD="' + self.SECRET  # never closes
        poisoned, _original = self._fixtures_with_stdout(harness, fixtures, stdout)
        backend = _ScriptedLocalBackend(self._scripted(poisoned))
        monkeypatch.setattr(harness, "_load_jev_backend", lambda: backend)
        rows, errors, results = harness.predict_live(poisoned, DecisionPolicy())
        assert rows == [] and results == {}
        assert len(errors) == 1
        assert errors[0]["fixture"] == poisoned[0]["name"]
        assert errors[0]["error_class"] == "DecisionBackendError"
        # NO remote call happened for the rejected fixture, and no rules
        # attribution: no rule prediction row was produced either.
        assert backend.calls == []
        # the error record carries no raw text
        assert "tailtoken99" not in json.dumps(errors)
        assert "PASSWORD" not in json.dumps(errors)

    def test_cli_live_malformed_assignment_exits_5_without_secret_output(self, harness, tmp_path, monkeypatch, capsys):
        """CLI level: a fixtures dir where one fixture carries an unterminated
        quoted credential — exit 5, per-fixture prediction_error, and neither
        stdout report nor stderr mentions the secret."""
        secret = self.SECRET
        poisoned_dir = tmp_path / "fixtures"
        poisoned_dir.mkdir()
        base = next(f for f in harness.load_fixtures(FIXTURES_DIR) if f["name"] == "pytest_assertion_error")
        poisoned = {**base, "stdout": "filler\n" * 50 + 'PASSWORD="' + secret}
        (poisoned_dir / f"{poisoned['name']}.json").write_text(json.dumps(poisoned), encoding="utf-8")
        monkeypatch.setenv("TYPESAFE_API_KEY", "harness-test-fake-key")
        monkeypatch.setattr(
            harness, "_load_jev_backend",
            lambda: _ScriptedLocalBackend([_live_result(poisoned["name"], kind="code_bug", confidence=0.9)]),
        )
        rc = harness.main(["--fixtures-dir", str(poisoned_dir), "--backend", "jev", "--allow-network"])
        assert rc == 5
        captured = capsys.readouterr()
        assert "tailtoken99" not in captured.out and "tailtoken99" not in captured.err
        report = json.loads(captured.out)
        assert report["prediction_errors"][0]["error_class"] == "DecisionBackendError"
        assert report["predictions"] == []
        assert report["go_no_go"]["status"] == PENDING

    def test_rule_and_snapshot_paths_are_not_sanitized(self, harness, fixtures, monkeypatch):
        """The offline rule path replays the ORIGINAL text unchanged (no
        redaction, no rejection) — sanitization is a remote-path concern."""
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        base = next(f for f in fixtures if f["name"] == "pytest_assertion_error")
        poisoned = {**base, "stdout": 'PASSWORD="' + self.SECRET + '"\n' + base["stdout"]}
        rows, errors, _results = _rule_rows(harness, [poisoned])
        assert errors == [] and len(rows) == 1  # rule backend judged the raw text, locally


# ============================ strict snapshot choice argmax ============================


class TestSnapshotChoiceArgmax:
    def _entry_with_distribution(self, harness, fixtures, probabilities, choice):
        entries = _rule_snapshot_entries(harness, fixtures)
        first = entries[0]
        first["answers"]["failure_kind"] = {
            "kind": "choice",
            "choice": choice,
            "probabilities": probabilities,
            "confidence": 0.9,
        }
        return entries, first["fixture"]

    def test_chosen_label_must_be_argmax(self, harness, fixtures, tmp_path):
        """A snapshot whose chosen label is NOT the top of its own claimed
        distribution is self-contradictory — rejected."""
        probabilities = {"code_bug": 0.05, "flaky_or_timeout": 0.90, "missing_dependency": 0.01,
                         "environment_or_tooling": 0.01, "test_needs_update": 0.02,
                         "unrelated_preexisting": 0.01}
        entries, fixture_name = self._entry_with_distribution(harness, fixtures, probabilities, "code_bug")
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        assert harness.main(["--backend", "snapshot", "--snapshot", str(path)]) == 4

    def test_argmax_tie_is_accepted(self, harness, fixtures, tmp_path):
        """Ties are allowed: two labels share the top probability; choosing
        either of them is consistent with the distribution."""
        probabilities = {"code_bug": 0.5, "flaky_or_timeout": 0.5, "missing_dependency": 0.0,
                         "environment_or_tooling": 0.0, "test_needs_update": 0.0,
                         "unrelated_preexisting": 0.0}
        entries, _name = self._entry_with_distribution(harness, fixtures, probabilities, "code_bug")
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        rc = harness.main(["--backend", "snapshot", "--snapshot", str(path)])
        assert rc == 0  # accepted, not a precision trap

    def test_near_argmax_within_tolerance_is_accepted(self, harness, fixtures, tmp_path):
        """Renormalization noise (same scale as the distribution-sum tolerance)
        must not reject an honest argmax."""
        probabilities = {"code_bug": 0.45, "flaky_or_timeout": 0.4500000001,
                         "missing_dependency": 0.0333333333, "environment_or_tooling": 0.0333333332,
                         "test_needs_update": 0.0333333334, "unrelated_preexisting": 0.0}
        entries, _name = self._entry_with_distribution(harness, fixtures, probabilities, "code_bug")
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        assert harness.main(["--backend", "snapshot", "--snapshot", str(path)]) == 0

    def test_rule_backend_snapshots_still_validate(self, harness, fixtures, tmp_path):
        """The rule backend's own outputs (chosen at pmax) pass the argmax
        check — no regression on the honest replay path."""
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(_snapshot_document(_rule_snapshot_entries(harness, fixtures))), encoding="utf-8")
        assert harness.main(["--backend", "snapshot", "--snapshot", str(path)]) == 0


# ============================ CLI as a script ============================


class TestCliScript:
    def _run(self, *args, cwd=REPO_ROOT):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, str(TOOL_PATH), *args], cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=120,
        )

    def test_direct_invocation_produces_json_report(self):
        result = self._run()
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["generated_by"] == "tools/evaluate_decision_triage.py"
        assert report["mode"]["offline"] is True
        assert report["go_no_go"]["status"] == PENDING

    def test_direct_invocation_from_other_cwd_still_imports_repository(self, tmp_path):
        result = self._run(cwd=tmp_path)  # no hardcoded /tmp — pytest-managed dir
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["dataset"]["fixture_count"] > 0

    def test_exit_2_for_unknown_backend(self):
        assert self._run("--backend", "bogus").returncode == 2

    def test_exit_2_for_snapshot_without_path(self):
        assert self._run("--backend", "snapshot").returncode == 2

    def test_exit_2_for_bad_act_threshold(self):
        assert self._run("--act-threshold", "0.2").returncode == 2  # below escalate 0.5

    def test_exit_4_for_missing_snapshot_file(self, tmp_path):
        assert self._run("--backend", "snapshot", "--snapshot", str(tmp_path / "nope.json")).returncode == 4

    def test_exit_4_for_invalid_snapshot_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert self._run("--backend", "snapshot", "--snapshot", str(bad)).returncode == 4

    def test_report_is_deterministic_except_observed_latency(self):
        def _normalized_report():
            result = self._run()
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            for row in report["predictions"]:
                row["latency_ms"] = 0
            latency = report["metrics"]["latency_tokens_fallback"]["latency_ms"]
            latency["p50"] = latency["p95"] = 0
            return report

        first = _normalized_report()
        second = _normalized_report()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_llm_comparison_is_imported_not_generated(self, harness, fixtures, tmp_path, capsys):
        entries = _rule_snapshot_entries(harness, fixtures)
        # honest imported comparison: one fixture predicted differently — the
        # flipped choice is backed by a consistent argmax distribution
        for entry in entries:
            if entry["fixture"] == "pytest_assertion_error":
                entry["answers"]["failure_kind"] = {
                    "kind": "choice",
                    "choice": "test_needs_update",
                    "probabilities": {"test_needs_update": 0.8, "code_bug": 0.1,
                                      "missing_dependency": 0.03, "environment_or_tooling": 0.03,
                                      "flaky_or_timeout": 0.02, "unrelated_preexisting": 0.02},
                    "confidence": 0.9,
                }
        llm_path = tmp_path / "llm.json"
        llm_path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        assert harness.main(["--llm-snapshot", str(llm_path)]) == 0
        report = json.loads(capsys.readouterr().out)
        comparison = report["llm_comparison"]
        assert comparison["fixture_count"] == len(fixtures)
        assert comparison["kind_accuracy"] < 1.0
        assert "never generated" in comparison["note"]

    def test_llm_snapshot_goes_through_strict_reconstruction(self, harness, fixtures, tmp_path):
        """The comparator path must NOT have an ad-hoc parser: an unknown
        choice label must be rejected exactly like --backend snapshot does."""
        entries = _rule_snapshot_entries(harness, fixtures)
        entries[0]["answers"]["failure_kind"]["choice"] = "made_up_label"
        llm_path = tmp_path / "llm-bad.json"
        llm_path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        assert harness.main(["--llm-snapshot", str(llm_path)]) == 4

    def test_llm_snapshot_incomplete_is_rejected(self, harness, fixtures, tmp_path):
        entries = _rule_snapshot_entries(harness, fixtures)[:-1]
        llm_path = tmp_path / "llm.json"
        llm_path.write_text(json.dumps(_snapshot_document(entries)), encoding="utf-8")
        assert harness.main(["--llm-snapshot", str(llm_path)]) == 4
