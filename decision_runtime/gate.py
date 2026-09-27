"""VerificationFailureGate — the seam fix_runtime.FixLoopRunner calls into.

Wires together, for one failing verification check: deterministic fact
extraction (design rule 3), the optional baseline rerun (decision_runtime.
baseline — only ever invoked here, i.e. only when the decision layer is
enabled and a check has actually failed, never unconditionally), a
DecisionPort backend (RuleDecisionBackend today; a future JevDecisionBackend
is a drop-in), the canonical decision.made record (design rule 5), and the
action mapping (decision_runtime.triage.decide_triage_action).

fix_runtime never imports decision_runtime.triage/policy/models directly —
it only ever sees this one class and TriageAction/TriageOutcome, keeping the
actual question set and rules replaceable without touching FixLoopRunner.
"""

from __future__ import annotations

from typing import Any

from decision_runtime.baseline import run_baseline_check
from decision_runtime.errors import DecisionBackendError
from decision_runtime.models import DecisionResult
from decision_runtime.policy import DecisionPolicy
from decision_runtime.ports import DecisionPort
from decision_runtime.recorder import CanonicalDecisionRecorder
from decision_runtime.triage import (
    RuleDecisionBackend,
    TriageFacts,
    TriageOutcome,
    build_triage_spec,
    build_triage_state,
    decide_triage_action,
    extract_error_block,
)
from verification_runtime.models import VerificationCheck, VerificationPlan, VerificationReport, VerificationStatus
from verification_runtime.runner import classify

TRIAGE_DECISION_ID = "verification_failure_triage"


def _first_failed_check(plan: VerificationPlan, report: VerificationReport) -> tuple[VerificationCheck, Any]:
    """The first FAILing check result and its VerificationCheck (deterministic:
    plan/report order is already stable — see VerificationPlan/Report)."""
    checks_by_id = {check.check_id: check for check in plan.checks}
    for result in report.results:
        if result.status is VerificationStatus.FAIL:
            check = checks_by_id.get(result.check_id)
            if check is not None:
                return check, result
    raise ValueError("VerificationFailureGate requires a report with at least one FAIL result "
                      "matching a check in the given plan.")


def _baseline_status(check: VerificationCheck, baseline_result) -> tuple[str | None, int | None]:
    if baseline_result is None:
        return None, None
    if baseline_result.timed_out:
        return "timeout", None
    status = classify(check, baseline_result)
    return status.value, baseline_result.exit_code


class VerificationFailureGate:
    """Triage a verification FAIL and decide the pipeline action to take."""

    def __init__(
        self,
        backend: DecisionPort,
        *,
        policy: DecisionPolicy | None = None,
        recorder: CanonicalDecisionRecorder | None = None,
        run_baseline: bool = True,
    ) -> None:
        self._backend = backend
        self._policy = policy or DecisionPolicy()
        self._recorder = recorder
        self._run_baseline = run_baseline

    def evaluate(
        self,
        *,
        workspace: Any,
        verification_plan: VerificationPlan,
        verification_report: VerificationReport,
        changed_paths: tuple[str, ...] = (),
    ) -> TriageOutcome:
        check, check_result = _first_failed_check(verification_plan, verification_report)
        process_result = check_result.process_result

        baseline_result = None
        if self._run_baseline:
            # Baseline rerun is intentionally best-effort (see
            # decision_runtime.baseline module docstring) and ONLY ever
            # invoked here — after a real FAIL, never speculatively.
            baseline_result = run_baseline_check(workspace, check.request)
        baseline_status, baseline_exit_code = _baseline_status(check, baseline_result)

        error_block = extract_error_block(process_result.stdout, process_result.stderr)
        state = build_triage_state(
            TriageFacts(
                check_id=check.check_id, command=check.request.argv, exit_code=process_result.exit_code,
                timed_out=process_result.timed_out, error_block=error_block, changed_paths=changed_paths,
                baseline_status=baseline_status, baseline_exit_code=baseline_exit_code,
            )
        )
        spec = build_triage_spec(TRIAGE_DECISION_ID)

        result = self._decide(spec, state)
        if self._recorder is not None:
            self._recorder.record(result)
        return decide_triage_action(result, self._policy)

    def _decide(self, spec, state) -> DecisionResult:
        """design rule 1: an accelerator backend failure ALWAYS falls back to
        the deterministic rule backend, never propagates."""
        try:
            return self._backend.decide(spec, state)
        except DecisionBackendError:
            fallback_result = RuleDecisionBackend().decide(spec, state)
            return _mark_fallback(fallback_result)


def _mark_fallback(result: DecisionResult) -> DecisionResult:
    import dataclasses

    return dataclasses.replace(result, fallback_used=True)
