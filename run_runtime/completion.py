"""Explicit Run terminal ownership for native execution and verification."""

from __future__ import annotations

from dataclasses import dataclass

from run_runtime.errors import InvalidRunStateError, RunCompletionError
from run_runtime.events import RunEvent, RunEventType
from run_runtime.models import RunStatus
from run_runtime.readmodels import load_full_event_history
from run_runtime.service import RunRuntime

_MAX_MESSAGE = 2000
_ERROR_STATUSES = {"fail", "timeout", "error"}


@dataclass(frozen=True, slots=True)
class _RunEvidence:
    run_id: str
    decision_seq: int
    events: tuple[RunEvent, ...]


def _bounded_message(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return value.replace("\x00", "")[:_MAX_MESSAGE]


class RunCompletionGate:
    """Validate canonical evidence and append explicit Run terminal events."""

    def __init__(self, runtime: RunRuntime) -> None:
        self._runtime = runtime

    def _evidence(self, run_id: str) -> _RunEvidence:
        run = self._runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise InvalidRunStateError(
                f"Run completion requires RUNNING status, got {run.status}"
            )
        decision_seq = run.last_event_seq
        events = load_full_event_history(
            self._runtime.store, run_id, through_seq=decision_seq
        )
        return _RunEvidence(run_id, decision_seq, events)

    @staticmethod
    def _latest_verification_start(evidence: _RunEvidence) -> RunEvent:
        starts = [
            event for event in evidence.events
            if event.type == RunEventType.VERIFICATION_STARTED
        ]
        if not starts:
            raise RunCompletionError("No verification attempt exists for this Run")
        return starts[-1]

    @staticmethod
    def _verification_terminal(
        evidence: _RunEvidence, start: RunEvent, verification_id: str
    ) -> RunEvent:
        terminals = [
            event for event in evidence.events
            if event.seq >= start.seq
            and event.payload.get("verification_id") == verification_id
            and event.type in {
                RunEventType.VERIFICATION_COMPLETED,
                RunEventType.VERIFICATION_INTERRUPTED,
            }
        ]
        if not terminals:
            raise RunCompletionError("Verification attempt has no terminal evidence")
        return terminals[-1]

    @staticmethod
    def _latest_execution_lifecycle_before(
        evidence: _RunEvidence, seq: int
    ) -> RunEvent | None:
        lifecycle = [
            event for event in evidence.events
            if event.seq < seq
            and event.type in {
                RunEventType.EXECUTION_STARTED,
                RunEventType.EXECUTION_COMPLETED,
                RunEventType.EXECUTION_FAILED,
            }
        ]
        return lifecycle[-1] if lifecycle else None

    @staticmethod
    def _has_newer_execution_activity(evidence: _RunEvidence, seq: int) -> bool:
        """Return whether this stable prefix contains a later execution attempt fact."""
        return any(
            event.seq > seq
            and event.type in {
                RunEventType.EXECUTION_STARTED,
                RunEventType.EXECUTION_COMPLETED,
                RunEventType.EXECUTION_FAILED,
            }
            for event in evidence.events
        )

    @staticmethod
    def _require_latest_verification(
        evidence: _RunEvidence, verification_id: str
    ) -> tuple[RunEvent, RunEvent]:
        if not isinstance(verification_id, str) or not verification_id:
            raise RunCompletionError("verification_id must be non-empty")
        start = RunCompletionGate._latest_verification_start(evidence)
        if start.payload.get("verification_id") != verification_id:
            raise RunCompletionError("Requested verification is not the latest attempt")
        terminal = RunCompletionGate._verification_terminal(evidence, start, verification_id)
        if terminal.type == RunEventType.VERIFICATION_INTERRUPTED:
            raise RunCompletionError("Verification attempt was interrupted")
        return start, terminal

    def complete_verified(self, run_id: str, *, verification_id: str) -> None:
        evidence = self._evidence(run_id)
        start, terminal = self._require_latest_verification(evidence, verification_id)
        if terminal.payload.get("status") != "pass":
            raise RunCompletionError("Only a PASS verification can complete a Run")
        if self._has_newer_execution_activity(evidence, start.seq):
            raise RunCompletionError(
                "Verification evidence is stale because newer execution activity exists"
            )
        execution = self._latest_execution_lifecycle_before(evidence, start.seq)
        if execution is None:
            raise RunCompletionError("Verification requires prior execution evidence")
        if execution.type != RunEventType.EXECUTION_COMPLETED:
            raise RunCompletionError(
                "Execution was not settled successfully when verification began"
            )
        if not execution.execution_id:
            raise RunCompletionError("Successful execution evidence lacks execution_id")
        self._runtime.record(
            run_id=run_id,
            type=RunEventType.RUN_COMPLETED,
            payload={
                "reason": "verified",
                "verification_id": verification_id,
                "verification_status": "pass",
                "verified_execution_id": execution.execution_id,
            },
            source="run_gate",
            expected_last_event_seq=evidence.decision_seq,
        )

    def fail_execution(self, run_id: str, *, execution_id: str) -> None:
        evidence = self._evidence(run_id)
        if not isinstance(execution_id, str) or not execution_id:
            raise RunCompletionError("execution_id must be non-empty")
        failures = [
            event for event in evidence.events
            if event.type == RunEventType.EXECUTION_FAILED
            and event.execution_id == execution_id
        ]
        if not failures:
            raise RunCompletionError("Requested execution failure evidence is missing")
        failure = failures[-1]
        if self._has_newer_execution_activity(evidence, failure.seq):
            raise RunCompletionError("Execution failure evidence is stale")
        self._runtime.record(
            run_id=run_id,
            type=RunEventType.RUN_FAILED,
            payload={
                "error_code": "execution_failed",
                "error_message": _bounded_message(
                    failure.payload.get("message", failure.payload.get("error_message")),
                    "Execution failed.",
                ),
                "execution_id": execution_id,
            },
            source="run_gate",
            expected_last_event_seq=evidence.decision_seq,
        )

    def fail_verification(self, run_id: str, *, verification_id: str) -> None:
        evidence = self._evidence(run_id)
        start, terminal = self._require_latest_verification(evidence, verification_id)
        status = terminal.payload.get("status")
        if not isinstance(status, str) or status not in _ERROR_STATUSES:
            raise RunCompletionError(
                "Only completed fail, timeout, or error verification can fail a Run"
            )
        if self._has_newer_execution_activity(evidence, start.seq):
            raise RunCompletionError(
                "Verification evidence is stale because newer execution activity exists"
            )
        execution = self._latest_execution_lifecycle_before(evidence, start.seq)
        if execution is not None and execution.type == RunEventType.EXECUTION_STARTED:
            raise RunCompletionError(
                "Execution was unsettled when verification began"
            )
        messages = {
            "fail": "Verification failed.",
            "timeout": "Verification timed out.",
            "error": "Verification produced an error.",
        }
        self._runtime.record(
            run_id=run_id,
            type=RunEventType.RUN_FAILED,
            payload={
                "error_code": f"verification_{status}",
                "error_message": messages[status],
                "verification_id": verification_id,
                "verification_status": status,
            },
            source="run_gate",
            expected_last_event_seq=evidence.decision_seq,
        )
