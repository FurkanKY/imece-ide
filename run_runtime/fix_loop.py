"""Canonical recorder for the bounded native Fix Loop (Milestone 3H).

CanonicalFixLoopRecorder owns ONLY fix_loop.*/fix_attempt.* events. It never
writes execution.*/verification.*/review.* events — those remain owned by
the Worker/Verification/Reviewer adapters and their own canonical sinks
(CanonicalAgentEventSink / CanonicalVerificationEventSink /
CanonicalReviewEventSink), exactly as before 3H. Every fix-loop-authored
event has execution_id=None, correlation_id=fix_loop_id, source="fix_loop".

CRITICAL: this recorder does NOT hold one stale expected_last_event_seq
across the whole loop. The Worker/Verification/Reviewer adapters append
their own events between this recorder's boundary writes, so every method
here re-reads the RUNNING Run's current last_event_seq immediately before
its own append (see module docstring point 16 in the milestone spec). If a
concurrent writer races after that read, EventSequenceError surfaces
unmodified — it is never swallowed or silently retried.
"""

from __future__ import annotations

import re
from typing import Any

from run_runtime.errors import FixLoopRecordingError
from run_runtime.events import RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime

SOURCE = "fix_loop"
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_ID_LENGTH = 128


def _validate_fix_loop_id(value: Any) -> str:
    """Same bounded-stable-identifier discipline as review/verification IDs.

    Deliberately duplicated rather than imported from fix_runtime: run_runtime
    must not depend on fix_runtime (fix_runtime already depends on run_runtime
    for RunRuntime/RunCompletionGate/CanonicalFixLoopRecorder — importing back
    would create a circular package import)."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise ValueError("fix_loop_id must be a bounded stable identifier.")
    return value


class CanonicalFixLoopRecorder:
    """Records exactly one fix-loop attempt's fix_loop.*/fix_attempt.* trail."""

    def __init__(self, runtime: RunRuntime, run_id: str, *, fix_loop_id: str) -> None:
        fix_loop_id = _validate_fix_loop_id(fix_loop_id)
        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Fix loop recorder requires RUNNING run, got {run.status}")
        self._runtime = runtime
        self._run_id = run_id
        self._fix_loop_id = fix_loop_id
        self._started_persisted = False
        self._terminal_recorded = False
        self._active_attempt_index: int | None = None
        self._active_attempt_id: str | None = None
        self._active_worker_execution_id: str | None = None
        self._last_completed_attempt_index = 0

    @property
    def fix_loop_id(self) -> str:
        return self._fix_loop_id

    @property
    def has_active_attempt(self) -> bool:
        return self._active_attempt_index is not None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._started_persisted:
            raise FixLoopRecordingError(f"fix_loop.started already recorded: {self._fix_loop_id}")
        self._reject_reused_loop_id()
        self._commit(RunEventType.FIX_LOOP_STARTED, {"fix_loop_id": self._fix_loop_id})
        self._started_persisted = True

    def attempt_started(
        self,
        *,
        fix_attempt_id: str,
        attempt_index: int,
        trigger_kind: str,
        worker_execution_id: str,
        before_diff_sha256: str,
    ) -> None:
        self._require_started()
        self._require_not_terminal()
        if self._active_attempt_index is not None:
            raise FixLoopRecordingError(
                f"An attempt is already active for this fix loop: {self._fix_loop_id}"
            )
        expected_index = self._last_completed_attempt_index + 1
        if attempt_index != expected_index:
            raise FixLoopRecordingError(
                f"attempt_index must be monotonic: expected {expected_index}, got {attempt_index}"
            )
        self._commit(RunEventType.FIX_ATTEMPT_STARTED, {
            "fix_loop_id": self._fix_loop_id,
            "fix_attempt_id": fix_attempt_id,
            "attempt_index": attempt_index,
            "trigger_kind": trigger_kind,
            "worker_execution_id": worker_execution_id,
            "before_diff_sha256": before_diff_sha256,
        })
        self._active_attempt_index = attempt_index
        self._active_attempt_id = fix_attempt_id
        self._active_worker_execution_id = worker_execution_id

    def attempt_completed(
        self,
        *,
        fix_attempt_id: str,
        attempt_index: int,
        worker_execution_id: str,
        before_diff_sha256: str,
        after_diff_sha256: str,
        changed: bool,
    ) -> None:
        self._require_started()
        self._require_not_terminal()
        if self._active_attempt_index != attempt_index or self._active_attempt_id != fix_attempt_id:
            raise FixLoopRecordingError(
                f"fix_attempt.completed does not match the active attempt: {self._fix_loop_id}"
            )
        self._commit(RunEventType.FIX_ATTEMPT_COMPLETED, {
            "fix_loop_id": self._fix_loop_id,
            "fix_attempt_id": fix_attempt_id,
            "attempt_index": attempt_index,
            "worker_execution_id": worker_execution_id,
            "before_diff_sha256": before_diff_sha256,
            "after_diff_sha256": after_diff_sha256,
            "changed": bool(changed),
        })
        self._active_attempt_index = None
        self._active_attempt_id = None
        self._active_worker_execution_id = None
        self._last_completed_attempt_index = attempt_index

    def attempt_interrupted(self, *, reason: str, outcome_unknown: bool = True, **extra: Any) -> None:
        """Settle the currently active attempt on an in-process infrastructure abort.

        Uses the recorder's own internally-tracked active-attempt identity
        (never caller-supplied) so it can never be recorded against the
        wrong attempt. Raises if no attempt is currently active — callers
        must check `has_active_attempt` first (see FixLoopRunner's
        best-effort infrastructure-failure settlement path).
        """
        self._require_started()
        self._require_not_terminal()
        if self._active_attempt_index is None:
            raise FixLoopRecordingError(
                f"No active fix attempt to interrupt: {self._fix_loop_id}"
            )
        payload = {
            "fix_loop_id": self._fix_loop_id,
            "fix_attempt_id": self._active_attempt_id,
            "attempt_index": self._active_attempt_index,
            "worker_execution_id": self._active_worker_execution_id,
            "reason": reason,
            "outcome_unknown": bool(outcome_unknown),
        }
        payload.update(extra)
        self._commit(RunEventType.FIX_ATTEMPT_INTERRUPTED, payload)
        self._active_attempt_index = None
        self._active_attempt_id = None
        self._active_worker_execution_id = None

    def completed(
        self,
        *,
        attempts_used: int,
        final_execution_id: str | None,
        verification_id: str,
        review_id: str,
        diff_sha256: str,
    ) -> None:
        self._append_terminal(RunEventType.FIX_LOOP_COMPLETED, {
            "fix_loop_id": self._fix_loop_id,
            "attempts_used": attempts_used,
            "final_execution_id": final_execution_id,
            "verification_id": verification_id,
            "review_id": review_id,
            "diff_sha256": diff_sha256,
        })

    def exhausted(self, *, reason: str, attempts_used: int, max_fix_attempts: int, **extra: Any) -> None:
        payload = {
            "fix_loop_id": self._fix_loop_id,
            "reason": reason,
            "attempts_used": attempts_used,
            "max_fix_attempts": max_fix_attempts,
        }
        payload.update(extra)
        self._append_terminal(RunEventType.FIX_LOOP_EXHAUSTED, payload)

    def failed(self, *, reason: str, **extra: Any) -> None:
        payload = {"fix_loop_id": self._fix_loop_id, "reason": reason}
        payload.update(extra)
        self._append_terminal(RunEventType.FIX_LOOP_FAILED, payload)

    # ---------------- internals ----------------

    def _require_started(self) -> None:
        if not self._started_persisted:
            raise FixLoopRecordingError(f"fix_loop.started has not been persisted yet: {self._fix_loop_id}")

    def _require_not_terminal(self) -> None:
        if self._terminal_recorded:
            raise FixLoopRecordingError(f"Fix loop already has a terminal event: {self._fix_loop_id}")

    def _append_terminal(self, event_type: str, payload: dict[str, Any]) -> None:
        self._require_started()
        self._require_not_terminal()
        if self._active_attempt_index is not None:
            raise FixLoopRecordingError(
                f"Cannot record a fix-loop terminal while an attempt is still active "
                f"(attempt_index={self._active_attempt_index}); complete or interrupt "
                f"it first: {self._fix_loop_id}"
            )
        self._commit(event_type, payload)
        self._terminal_recorded = True

    def _current_expected_seq(self) -> int:
        run = self._runtime.get_run(self._run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Fix loop recorder requires RUNNING run, got {run.status}")
        return run.last_event_seq

    def _commit(self, event_type: str, payload: dict[str, Any]) -> None:
        expected = self._current_expected_seq()
        self._runtime.record(
            run_id=self._run_id,
            type=event_type,
            payload=payload,
            execution_id=None,
            correlation_id=self._fix_loop_id,
            source=SOURCE,
            expected_last_event_seq=expected,
        )

    def _reject_reused_loop_id(self) -> None:
        after_seq = 0
        while True:
            page = self._runtime.events(self._run_id, after_seq=after_seq, limit=200)
            for existing in page.events:
                if (
                    existing.type == RunEventType.FIX_LOOP_STARTED
                    and existing.payload.get("fix_loop_id") == self._fix_loop_id
                ):
                    raise ValueError(f"fix_loop_id already started in run: {self._fix_loop_id}")
            if not page.has_more:
                return
            after_seq = page.events[-1].seq
