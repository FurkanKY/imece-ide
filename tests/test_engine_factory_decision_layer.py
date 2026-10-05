"""engine_factory.py — decision_layer preference wiring (off | rules | jev).

S1b: "jev" now resolves to the real JevDecisionBackend (typesafe-sdk,
optional dependency). Its constructor is LAZY — no TYPESAFE_API_KEY read and
no SDK import — so a missing key/dependency can never raise at run
construction and can therefore never trigger the caller's (webhost/api/
run.py) "yeni motor kullanılamadı -> klasik motor" engine fallback; it only
surfaces at decide() time as a typed DecisionBackendError that the gate maps
to the deterministic rule backend.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine_factory  # noqa: E402
from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason  # noqa: E402
from decision_runtime.gate import VerificationFailureGate  # noqa: E402
from decision_runtime.jev_backend import JevDecisionBackend  # noqa: E402
from decision_runtime.recorder import CanonicalDecisionRecorder  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    RuleDecisionBackend,
    build_triage_spec,
    build_triage_state,
    TriageFacts,
)
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
import ui_prefs  # noqa: E402


def test_off_is_the_ui_prefs_default():
    assert ui_prefs.DEFAULTS["decision_layer"] == "off"


def test_build_decision_backend_off_returns_none():
    assert engine_factory.build_decision_backend("off") is None


def test_build_decision_backend_rules_resolves_to_rule_backend():
    assert isinstance(engine_factory.build_decision_backend("rules"), RuleDecisionBackend)


def test_build_decision_backend_jev_resolves_to_the_real_jev_backend(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def _must_not_import():  # pragma: no cover - only runs on a lazy-constructor bug
        raise AssertionError("build_decision_backend('jev') must not import the SDK")

    monkeypatch.setattr("decision_runtime.jev_backend._import_typesafe_sdk", _must_not_import)
    backend = engine_factory.build_decision_backend("jev")
    assert isinstance(backend, JevDecisionBackend)


def test_build_decision_backend_jev_never_fails_at_construction_without_key_or_sdk(monkeypatch):
    """Missing key AND missing optional dependency must not raise here —
    this is the exact property that keeps webhost's engine selection from
    falling back to the legacy engine when the user only chose "jev"."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    def _no_sdk():
        raise ImportError("No module named 'typesafe_sdk'")

    monkeypatch.setattr("decision_runtime.jev_backend._import_typesafe_sdk", _no_sdk)
    backend = engine_factory.build_decision_backend("jev")
    assert isinstance(backend, JevDecisionBackend)
    # the missing setup only surfaces at decide() time, typed, as a signal to
    # fall back to the rule backend — never as a legacy-engine fallback here.
    spec = build_triage_spec("verification_failure_triage")
    state = build_triage_state(
        TriageFacts(check_id="c1", command=("pytest",), exit_code=1, timed_out=False,
                    error_block="boom", changed_paths=())
    )
    with pytest.raises(DecisionBackendError) as excinfo:
        backend.decide(spec, state)
    assert excinfo.value.reason is DecisionBackendFailureReason.MISSING_API_KEY


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


def test_build_verification_failure_gate_jev_uses_the_jev_backend(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    gate = engine_factory.build_verification_failure_gate("jev")
    assert isinstance(gate, VerificationFailureGate)
    assert isinstance(gate._backend, JevDecisionBackend)  # noqa: SLF001 - wiring contract


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
