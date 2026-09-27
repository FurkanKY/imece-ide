"""engine_factory.py — decision_layer preference wiring (off | rules | jev)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine_factory  # noqa: E402
from decision_runtime.gate import VerificationFailureGate  # noqa: E402
from decision_runtime.recorder import CanonicalDecisionRecorder  # noqa: E402
from decision_runtime.triage import RuleDecisionBackend  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
import ui_prefs  # noqa: E402


def test_off_is_the_ui_prefs_default():
    assert ui_prefs.DEFAULTS["decision_layer"] == "off"


def test_build_decision_backend_off_returns_none():
    assert engine_factory.build_decision_backend("off") is None


def test_build_decision_backend_rules_and_jev_both_resolve_to_rule_backend():
    rules_backend = engine_factory.build_decision_backend("rules")
    jev_backend = engine_factory.build_decision_backend("jev")
    assert isinstance(rules_backend, RuleDecisionBackend)
    # "jev" falls back to the rule backend — no JevDecisionBackend exists yet (S1a).
    assert isinstance(jev_backend, RuleDecisionBackend)


def test_build_decision_backend_rejects_unknown_value():
    with pytest.raises(ValueError):
        engine_factory.build_decision_backend("not-a-real-value")


def test_build_verification_failure_gate_off_returns_none():
    assert engine_factory.build_verification_failure_gate("off") is None


def test_build_verification_failure_gate_rules_returns_a_gate_without_recording_by_default():
    gate = engine_factory.build_verification_failure_gate("rules")
    assert isinstance(gate, VerificationFailureGate)
    # no runtime/run_id given -> no recorder attached (private, but behaviorally
    # this is exercised end-to-end in tests/test_decision_runtime_gate.py).
    assert gate._recorder is None  # noqa: SLF001 - internal check, this module owns the wiring contract


def test_build_verification_failure_gate_attaches_a_recorder_when_runtime_and_run_id_given(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    gate = engine_factory.build_verification_failure_gate("rules", runtime=runtime, run_id=run.run_id)

    assert isinstance(gate._recorder, CanonicalDecisionRecorder)  # noqa: SLF001


def test_decision_layer_preference_defaults_to_off_for_missing_or_unknown_value():
    assert engine_factory.decision_layer_preference({}) == "off"
    assert engine_factory.decision_layer_preference({"decision_layer": "not-real"}) == "off"
    assert engine_factory.decision_layer_preference({"decision_layer": "rules"}) == "rules"


def test_decision_layer_preference_reads_real_ui_prefs_when_no_dict_given(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_prefs, "_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(ui_prefs, "_DIR", str(tmp_path))
    assert engine_factory.decision_layer_preference() == "off"
