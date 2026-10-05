"""VerificationFailureGate — the seam fix_runtime.FixLoopRunner calls into.

Wires together, for one failing verification check: deterministic fact
extraction (design rule 3), the optional baseline rerun (decision_runtime.
baseline — only ever invoked here, i.e. only when the decision layer is
enabled and a check has actually failed, never unconditionally), a
DecisionPort backend (the deterministic RuleDecisionBackend, or — since
Spike S1b — the REAL JevDecisionBackend on the same port), the canonical
decision.made record (design rule 5), and the action mapping
(decision_runtime.triage.decide_triage_action).

fix_runtime never imports decision_runtime.triage/policy/models directly —
it only ever sees this one class and TriageAction/TriageOutcome, keeping the
actual question set and rules replaceable without touching FixLoopRunner.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from decision_runtime.baseline import run_baseline_check
from decision_runtime.errors import DecisionBackendError
from decision_runtime.models import DecisionResult
from decision_runtime.policy import DecisionPolicy
from decision_runtime.ports import DecisionPort
from decision_runtime.recorder import CanonicalDecisionRecorder
from decision_runtime.remote_state import sanitize_process_output
from decision_runtime.triage import (
    RuleDecisionBackend,
    TriageAction,
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
        spec = build_triage_spec(TRIAGE_DECISION_ID)

        # LOCAL state: the ORIGINAL, unsanitized diagnostics — exactly
        # today's (S1a/rules-only) behaviour. The deterministic rule backend
        # ALWAYS classifies from this state, so a remote sanitization
        # rejection can never degrade the local evidence and the fallback is
        # indistinguishable from rules-only mode (baseline facts included).
        local_state = self._build_state(
            check, process_result, changed_paths, baseline_status, baseline_exit_code,
            stdout=process_result.stdout, stderr=process_result.stderr, command=check.request.argv,
        )

        remote_state = local_state
        if getattr(self._backend, "REMOTE_STATE_SANITIZED", False):
            # Remote (Jev) backend: redact the FULL raw output BEFORE any
            # extraction/cropping (decision_runtime.remote_state
            # .sanitize_process_output, no SDK import needed) — a long
            # quoted credential / private key straddling the error block's
            # crop boundary is redacted as a whole, so no fragment can
            # survive the cut. The remote backend's own state allowlist
            # re-verifies what it was handed.
            try:
                clean_stdout, clean_stderr, clean_command = sanitize_process_output(
                    process_result.stdout, process_result.stderr, check.request.argv,
                    workspace_root=getattr(workspace, "root", None),
                )
            except DecisionBackendError:
                # Fail closed BEFORE the remote backend is ever invoked (no
                # network call, no SDK import): fall back to the
                # deterministic rule backend on the ORIGINAL local evidence.
                # The canonical decision.made record is still written and
                # only the deterministic action guard is applied — this is a
                # design-rule-1 fallback, never a hard pipeline failure.
                return self._finish_with_fallback(spec, local_state, baseline_status)
            remote_state = self._build_state(
                check, process_result, changed_paths, baseline_status, baseline_exit_code,
                stdout=clean_stdout, stderr=clean_stderr, command=clean_command,
            )

        result = self._decide(spec, remote_state, local_state)
        if self._recorder is not None:
            self._recorder.record(result)
        outcome = decide_triage_action(result, self._policy)
        return _guard_pre_existing(outcome, baseline_status)

    def _finish_with_fallback(self, spec, local_state, baseline_status) -> TriageOutcome:
        """Remote PREPARATION was rejected (fail-closed sanitization): skip
        the remote backend entirely and settle the triage deterministically
        from the original local evidence (design rule 1)."""
        result = _mark_fallback(RuleDecisionBackend().decide(spec, local_state))
        if self._recorder is not None:
            self._recorder.record(result)
        outcome = decide_triage_action(result, self._policy)
        return _guard_pre_existing(outcome, baseline_status)

    def _build_state(
        self, check, process_result, changed_paths, baseline_status, baseline_exit_code,
        *, stdout, stderr, command,
    ):
        return build_triage_state(
            TriageFacts(
                check_id=check.check_id, command=command, exit_code=process_result.exit_code,
                timed_out=process_result.timed_out, error_block=extract_error_block(stdout, stderr),
                changed_paths=changed_paths, baseline_status=baseline_status,
                baseline_exit_code=baseline_exit_code,
            )
        )

    def _decide(self, spec, remote_state, local_state) -> DecisionResult:
        """design rule 1: an accelerator backend failure ALWAYS falls back to
        the deterministic rule backend — classifying the ORIGINAL local
        evidence (rules-only behaviour, never the sanitized copy) — and never
        propagates."""
        try:
            return self._backend.decide(spec, remote_state)
        except DecisionBackendError:
            return _mark_fallback(RuleDecisionBackend().decide(spec, local_state))


def _guard_pre_existing(outcome: TriageOutcome, baseline_status: str | None) -> TriageOutcome:
    """Deterministic safety guard on MARK_PRE_EXISTING (S1b): a remote model
    classifying `unrelated_preexisting` may only make the pipeline skip the
    fix loop when the SAME check ACTUALLY failed (VerificationStatus.FAIL)
    on the pre-change baseline run. A baseline that passed, was never
    obtained (None), or only timed out / errored (no real FAIL evidence) can
    never authorize skipping verification/review — such a misleading remote
    answer is downgraded to CONTINUE_FIX_LOOP (today's behaviour; a wrong
    skip is worse than a wasted attempt). The recorded decision.made result
    still shows the model's own answer; only the ACTION is vetoed."""
    if outcome.action is not TriageAction.MARK_PRE_EXISTING:
        return outcome
    if baseline_status == "fail":
        return outcome
    return dataclasses.replace(outcome, action=TriageAction.CONTINUE_FIX_LOOP)


def _mark_fallback(result: DecisionResult) -> DecisionResult:
    return dataclasses.replace(result, fallback_used=True)
