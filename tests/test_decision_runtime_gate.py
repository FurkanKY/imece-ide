"""decision_runtime.gate.VerificationFailureGate — orchestration tests.

Uses FakeDecisionBackend (no baseline rerun: run_baseline=False, so no real
git repo is needed here — decision_runtime.baseline is covered on its own in
tests/test_decision_runtime_baseline.py) to test: fact extraction from a
real VerificationReport/Plan, the design-rule-1 fallback to
RuleDecisionBackend on a DecisionBackendError, and optional canonical
decision.made recording.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime import gate as gate_module  # noqa: E402
from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason  # noqa: E402
from decision_runtime.fake_backend import FakeDecisionBackend  # noqa: E402
from decision_runtime.gate import VerificationFailureGate  # noqa: E402
from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer, ScoreAnswer  # noqa: E402
from decision_runtime.policy import DecisionPolicy  # noqa: E402
from decision_runtime.recorder import CanonicalDecisionRecorder  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    QUESTION_SET_VERSION,
    TriageAction,
    build_triage_spec,
    build_triage_state,
    TriageFacts,
    RuleDecisionBackend,
)
from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import VerificationCheck, VerificationCheckResult, VerificationPlan, VerificationReport, VerificationStatus  # noqa: E402


def _plan_and_failing_report():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="ModuleNotFoundError: No module named 'requests'\n",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    result = VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process)
    report = VerificationReport(verification_id="ver-1", plan_id="plan-1", results=(result,), duration_ms=1)
    return plan, report


def _rule_result_for(state):
    spec = build_triage_spec("verification_failure_triage")
    return RuleDecisionBackend().decide(spec, state)


def test_evaluate_routes_a_confident_rule_answer_without_baseline():
    plan, report = _plan_and_failing_report()
    backend = RuleDecisionBackend()
    gate = VerificationFailureGate(backend, run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.NEEDS_USER
    assert outcome.failure_kind == "missing_dependency"


def test_backend_failure_falls_back_to_rule_backend_and_marks_fallback_used():
    plan, report = _plan_and_failing_report()
    backend = FakeDecisionBackend([DecisionBackendError("boom, backend is down")])
    gate = VerificationFailureGate(backend, run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.NEEDS_USER  # rule fallback still classifies confidently
    assert outcome.result.fallback_used is True
    assert outcome.result.backend == RuleDecisionBackend.BACKEND_NAME


def test_real_jev_backend_without_key_falls_back_to_rule_backend_and_marks_fallback_used(monkeypatch):
    """S1b: the gate's design-rule-1 fallback works for the REAL Jev backend
    too — no TYPESAFE_API_KEY (and the SDK imported lazily) degrades the
    triage to the deterministic rule backend with fallback_used=True, never
    a hard failure."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    plan, report = _plan_and_failing_report()
    from decision_runtime.jev_backend import JevDecisionBackend

    gate = VerificationFailureGate(JevDecisionBackend(), run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.NEEDS_USER  # rule fallback classifies the missing module confidently
    assert outcome.result.fallback_used is True
    assert outcome.result.backend == RuleDecisionBackend.BACKEND_NAME


def test_evaluate_records_a_decision_made_event_when_a_recorder_is_given(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)

    plan, report = _plan_and_failing_report()
    gate = VerificationFailureGate(RuleDecisionBackend(), recorder=recorder, run_baseline=False)
    gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    events = runtime.events(run.run_id, limit=50).events
    decision_events = [e for e in events if e.type == RunEventType.DECISION_MADE]
    assert len(decision_events) == 1
    assert decision_events[0].payload["answers"]["failure_kind"]["choice"] == "missing_dependency"


def test_evaluate_requires_a_failing_check_matching_the_plan():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    passing_report = VerificationReport(
        verification_id="ver-1", plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check", VerificationStatus.PASS, process),), duration_ms=1,
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=False)
    with pytest.raises(ValueError):
        gate.evaluate(workspace=object(), verification_plan=plan, verification_report=passing_report)


def test_low_confidence_default_guess_continues_fix_loop():
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="assert 4 == 5\n",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    report = VerificationReport(
        verification_id="ver-1", plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process),), duration_ms=1,
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=False)
    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)
    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP


# ==================== S1b safety guard: MARK_PRE_EXISTING needs a real
# baseline FAIL (a misleading remote answer may never skip
# verification/review) ====================


_LABELS = (
    "code_bug", "test_needs_update", "missing_dependency",
    "environment_or_tooling", "flaky_or_timeout", "unrelated_preexisting",
)


def _misleading_remote_preexisting_result():
    """A high-confidence 'unrelated_preexisting' DecisionResult AS A REMOTE
    (Jev) backend would return it — deliberately INDEPENDENT of the actual
    baseline fact, so the gate's deterministic guard is what stands between
    this answer and a skipped fix loop."""
    probabilities = {label: (0.99 if label == "unrelated_preexisting" else 0.002) for label in _LABELS}
    return DecisionResult(
        decision_id="verification_failure_triage",
        question_set_version=QUESTION_SET_VERSION,
        answers={
            "failure_kind": ChoiceAnswer(
                choice="unrelated_preexisting", probabilities=probabilities, confidence=0.99,
            ),
            "caused_by_change": NoulAnswer(noul=0.02),
            "fixable_by_agent": ScoreAnswer(
                score=0.0, probabilities={"0": 0.9, "1": 0.1, "2": 0.0}, confidence=0.99,
            ),
        },
        backend="jev",
        model_version="jev-1.13.0",
        latency_ms=5,
        prompt_tokens=87,
        fallback_used=False,
    )


def _baseline_process_result(**overrides):
    defaults = dict(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )
    defaults.update(overrides)
    return ProcessResult(**defaults)


@pytest.mark.parametrize(
    ("baseline_result", "expected_action"),
    [
        pytest.param(_baseline_process_result(exit_code=1), TriageAction.MARK_PRE_EXISTING, id="baseline-fail"),
        pytest.param(_baseline_process_result(exit_code=0), TriageAction.CONTINUE_FIX_LOOP, id="baseline-pass"),
        pytest.param(_baseline_process_result(timed_out=True), TriageAction.CONTINUE_FIX_LOOP, id="baseline-timeout"),
        pytest.param(_baseline_process_result(exit_code=None), TriageAction.CONTINUE_FIX_LOOP, id="baseline-error"),
        pytest.param(None, TriageAction.CONTINUE_FIX_LOOP, id="baseline-unavailable"),
    ],
)
def test_mark_pre_existing_only_when_the_same_check_actually_failed_on_baseline(
    monkeypatch, baseline_result, expected_action,
):
    plan, report = _plan_and_failing_report()
    monkeypatch.setattr(
        gate_module, "run_baseline_check", lambda workspace, request: baseline_result,
    )
    gate = VerificationFailureGate(FakeDecisionBackend([_misleading_remote_preexisting_result()]), run_baseline=True)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is expected_action
    assert outcome.failure_kind == "unrelated_preexisting"  # the model's answer is kept visible
    assert outcome.result.fallback_used is False  # the remote backend itself did not fail


def test_downgraded_pre_existing_still_records_the_model_answer(tmp_path, monkeypatch):
    """The audit trail stays honest: the recorded decision.made event shows
    the (misleading) remote answer, while the ACTION was vetoed by the
    deterministic baseline guard."""
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)

    plan, report = _plan_and_failing_report()
    monkeypatch.setattr(
        gate_module, "run_baseline_check", lambda workspace, request: _baseline_process_result(exit_code=0),
    )
    gate = VerificationFailureGate(
        FakeDecisionBackend([_misleading_remote_preexisting_result()]),
        recorder=recorder, run_baseline=True,
    )
    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP
    events = runtime.events(run.run_id, limit=50).events
    decision_events = [e for e in events if e.type == RunEventType.DECISION_MADE]
    assert len(decision_events) == 1
    assert decision_events[0].payload["answers"]["failure_kind"]["choice"] == "unrelated_preexisting"
    assert decision_events[0].payload["backend"] == "jev"


def test_rule_backend_baseline_timeout_is_also_guarded_at_the_gate(monkeypatch):
    """The guard applies to ANY backend, including today's deterministic one:
    a baseline that only TIMED OUT (no real FAIL) no longer authorizes
    skipping the fix loop even though RuleDecisionBackend's own rule 1
    classifies it as unrelated_preexisting with high confidence."""
    plan, report = _plan_and_failing_report()
    monkeypatch.setattr(
        gate_module, "run_baseline_check", lambda workspace, request: _baseline_process_result(timed_out=True),
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=True)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP
    assert outcome.failure_kind == "unrelated_preexisting"  # model answer unchanged; action vetoed


def test_rule_backend_with_a_real_baseline_fail_still_marks_pre_existing(monkeypatch):
    """The legit case is untouched: a baseline that actually FAILed the same
    check still authorizes MARK_PRE_EXISTING (today's S1a behaviour)."""
    plan, report = _plan_and_failing_report()
    monkeypatch.setattr(
        gate_module, "run_baseline_check", lambda workspace, request: _baseline_process_result(exit_code=1),
    )
    gate = VerificationFailureGate(RuleDecisionBackend(), run_baseline=True)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    assert outcome.action is TriageAction.MARK_PRE_EXISTING
    assert outcome.failure_kind == "unrelated_preexisting"


# ==================== S1b: remote backends sanitize the FULL raw output
# BEFORE extract/crop (redact-before-truncate) ====================


class _RemoteLikeFake(FakeDecisionBackend):
    """A fake backend that declares the remote-state contract like
    JevDecisionBackend does (REMOTE_STATE_SANITIZED), so the gate's
    sanitize-first path runs without any SDK/network."""

    REMOTE_STATE_SANITIZED = True


_SECRET_MIDDLE_MARK = "battery staple fill filler padded"

_CROPPED_LIKE_STDOUT = (
    "Traceback (most recent call last):\n"
    '  File "test_x.py", line 1, in <module>\n'
    "    running: export PASSWORD=\"" + ("correct horse " * 400) + _SECRET_MIDDLE_MARK + "\"\n"
    + "AssertionError: expected 4 == 5\n"
)


def _failing_report_with_stdout(stdout: str):
    plan = VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("pytest", "-q"))),))
    process = ProcessResult(
        argv=("pytest", "-q"), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
        stdout=stdout, stderr="",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=len(stdout), stderr_bytes=0,
    )
    result = VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process)
    report = VerificationReport(verification_id="ver-1", plan_id="plan-1", results=(result,), duration_ms=1)
    return plan, report


def test_remote_backend_sanitizes_full_output_before_error_block_crop(monkeypatch):
    """A long quoted credential straddling extract_error_block's 4000-char
    crop boundary: the gate sanitizes the FULL stdout first, so the state the
    (remote) backend receives carries NO fragment of the credential — the
    naive crop-then-sanitize order would leak its unrecognizable middle."""
    plan, report = _failing_report_with_stdout(_CROPPED_LIKE_STDOUT)
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)
    gate = VerificationFailureGate(_RemoteLikeFake([_misleading_remote_preexisting_result()]), run_baseline=False)

    gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    sent_state = gate._backend.calls[0][1]  # noqa: SLF001 - FakeDecisionBackend records (spec, state)
    sent_block = sent_state["error_block"]
    assert _SECRET_MIDDLE_MARK not in sent_block
    assert '"correct horse' not in sent_block
    assert "[REDACTED]" in sent_block  # the value was redacted as a whole


def test_without_sanitize_first_the_crop_would_leak_the_credential_middle(monkeypatch):
    """Proves the regression is real: a backend WITHOUT the remote marker
    keeps today's (offline) unsanitized path, and the crop then cuts the
    quoted credential in half — an unrecognizable middle fragment survives
    in the state. This is exactly what the sanitize-first path prevents for
    remote backends."""
    plan, report = _failing_report_with_stdout(_CROPPED_LIKE_STDOUT)
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)
    gate = VerificationFailureGate(FakeDecisionBackend([_misleading_remote_preexisting_result()]), run_baseline=False)

    gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    sent_state = gate._backend.calls[0][1]  # noqa: SLF001
    assert _SECRET_MIDDLE_MARK in sent_state["error_block"]  # the leak the guard prevents


def test_remote_backend_full_private_key_is_redacted_before_crop(monkeypatch):
    body_b64 = "\n".join("Ab3dEf6Gh9Ij2Kl5Mn8Qr1Tu4Wx7Yz0A" + str(i) * 5 for i in range(6))
    key_block = "-----BEGIN PRIVATE KEY-----\n" + body_b64 + "\n-----END PRIVATE KEY-----"
    stdout = "x" * 3900 + "\n" + key_block + "\nTraceback (most recent call last):\nAssertionError: boom\n"
    plan, report = _failing_report_with_stdout(stdout)
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)
    gate = VerificationFailureGate(_RemoteLikeFake([_misleading_remote_preexisting_result()]), run_baseline=False)

    gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    sent_state = gate._backend.calls[0][1]  # noqa: SLF001
    sent_block = sent_state["error_block"]
    assert "PRIVATE KEY" not in sent_block
    assert "Ab3dEf6Gh9Ij2Kl5" not in sent_block


def test_unsanitizable_remote_output_falls_back_without_invoking_the_remote_backend(tmp_path, monkeypatch):
    """A fail-closed sanitization rejection (unclosed quoted assignment) is a
    design-rule-1 fallback, NEVER a raise: the remote backend is not invoked
    at all (no network/SDK), the deterministic rule backend classifies the
    ORIGINAL local evidence, the canonical decision.made record is still
    written with fallback_used=True and no raw secret text, and the
    deterministic action guard is applied."""
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    recorder = CanonicalDecisionRecorder(runtime, run.run_id)

    plan, report = _failing_report_with_stdout('error: PASSWORD="unterminated secret value here')
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)
    gate = VerificationFailureGate(
        _RemoteLikeFake([_misleading_remote_preexisting_result()]),
        recorder=recorder, run_baseline=False,
    )

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    # the remote backend was NOT invoked
    assert gate._backend.calls == []  # noqa: SLF001
    # deterministic fallback classified the ORIGINAL local evidence
    assert outcome.result.fallback_used is True
    assert outcome.result.backend == RuleDecisionBackend.BACKEND_NAME
    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP  # low-confidence default -> today's behaviour
    # canonical record: exactly one decision.made, marked as fallback, no raw secret
    events = runtime.events(run.run_id, limit=50).events
    decision_events = [e for e in events if e.type == RunEventType.DECISION_MADE]
    assert len(decision_events) == 1
    payload = decision_events[0].payload
    assert payload["fallback_used"] is True
    assert payload["backend"] == "rule"
    assert "unterminated secret value" not in str(payload)
    assert "PASSWORD" not in str(payload)


def test_remote_failure_after_successful_sanitize_falls_back_on_original_local_evidence(monkeypatch):
    """The remote call itself failing (after successful sanitization) also
    falls back to the rule backend on the ORIGINAL evidence — not the
    sanitized copy — so rules-only behaviour is preserved byte-for-byte,
    while the remote backend still received only the sanitized state."""
    class _FailingRemote(_RemoteLikeFake):
        REMOTE_STATE_SANITIZED = True

        def decide(self, spec, state):  # noqa: D102 - scripted failure
            self.calls.append((spec, state))
            raise DecisionBackendError("remote is down")

    secret_line = 'running: export PASSWORD="some quoted secret value here"'
    stdout = (
        "Traceback (most recent call last):\n"
        "AssertionError: expected 4 == 5\n"
        + secret_line + "\n"
    )
    plan, report = _failing_report_with_stdout(stdout)
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)

    classified_states: list[dict] = []
    real_rule_backend = RuleDecisionBackend

    class _RecordingRuleBackend(real_rule_backend):
        def decide(self, spec, state):
            classified_states.append(state)
            return super().decide(spec, state)

    monkeypatch.setattr(gate_module, "RuleDecisionBackend", _RecordingRuleBackend)
    gate = VerificationFailureGate(_FailingRemote([None]), run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    # the remote backend WAS invoked once, and got a state derived from the
    # SANITIZED text only (the extracted block must not carry the secret)
    assert len(gate._backend.calls) == 1  # noqa: SLF001
    remote_state = gate._backend.calls[0][1]  # noqa: SLF001
    assert "some quoted secret value" not in remote_state["error_block"]
    assert "PASSWORD=[REDACTED]" in remote_state["error_block"]
    # ...while the rule fallback classified the ORIGINAL evidence
    assert len(classified_states) == 1
    assert "some quoted secret value here" in classified_states[0]["error_block"]
    assert outcome.result.fallback_used is True
    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP


def test_non_remote_backend_receives_the_original_state_unsanitized(monkeypatch):
    """normal no effect: a backend without the remote marker keeps today's
    (S1a) unsanitized path — original state in, original behaviour out."""
    plan, report = _failing_report_with_stdout('PASSWORD="some quoted secret value here" + assertion')
    monkeypatch.setattr(gate_module, "run_baseline_check", lambda workspace, request: None)
    gate = VerificationFailureGate(FakeDecisionBackend([_misleading_remote_preexisting_result()]), run_baseline=False)

    outcome = gate.evaluate(workspace=object(), verification_plan=plan, verification_report=report)

    sent_state = gate._backend.calls[0][1]  # noqa: SLF001
    assert "some quoted secret value here" in sent_state["error_block"]  # original, unsanitized
    assert outcome.result.fallback_used is False
    assert outcome.action is TriageAction.CONTINUE_FIX_LOOP  # guard: baseline never ran -> no skip
