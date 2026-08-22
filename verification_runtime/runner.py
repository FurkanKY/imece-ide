"""Sequential deterministic verification over the ProcessRunner primitive."""

from __future__ import annotations

import time
from typing import Any

from process_runtime import ProcessRunner
from process_runtime.errors import ProcessRuntimeError
from process_runtime.models import ProcessResult
from verification_runtime.errors import VerificationExecutionError, VerificationRecordingError
from verification_runtime.events import (
    NullVerificationEventSink,
    VerificationCheckCompleted,
    VerificationCheckFailed,
    VerificationCheckStarted,
    VerificationCompleted,
    VerificationEventSink,
    VerificationStarted,
)
from verification_runtime.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationPlan,
    VerificationReport,
    VerificationStatus,
    new_verification_id,
)


def classify(check: VerificationCheck, result: ProcessResult) -> VerificationStatus:
    """Classify only from ProcessResult facts and the explicit check policy."""
    if not isinstance(check, VerificationCheck) or not isinstance(result, ProcessResult):
        raise VerificationExecutionError("classify requires VerificationCheck and ProcessResult")
    if result.timed_out:
        return VerificationStatus.TIMEOUT
    if result.exit_code is None:
        return VerificationStatus.ERROR
    if result.exit_code in check.pass_exit_codes:
        return VerificationStatus.PASS
    if result.exit_code < 0:
        return VerificationStatus.ERROR
    if result.exit_code in check.error_exit_codes:
        return VerificationStatus.ERROR
    return VerificationStatus.FAIL


class VerificationRunner:
    def __init__(
        self,
        process_runner: ProcessRunner,
        event_sink: VerificationEventSink | None = None,
    ) -> None:
        self._process_runner = process_runner
        self._event_sink = event_sink or NullVerificationEventSink()

    def _emit(self, event) -> None:
        try:
            self._event_sink.emit(event)
        except VerificationRecordingError:
            raise
        except Exception as exc:
            raise VerificationRecordingError(
                f"Verification event recording failed: {exc}"
            ) from exc

    def run(
        self,
        workspace: Any,
        plan: VerificationPlan,
        *,
        verification_id: str | None = None,
    ) -> VerificationReport:
        if not isinstance(plan, VerificationPlan):
            raise VerificationExecutionError("VerificationRunner requires VerificationPlan")
        verification_id = verification_id or new_verification_id()
        # VerificationReport validates the identifier after execution; this
        # early validation prevents a malformed ID from causing side effects.
        from verification_runtime.models import _id

        _id(verification_id, "verification_id")
        started = time.monotonic()
        self._emit(VerificationStarted(verification_id, plan.plan_id, len(plan.checks)))
        results: list[VerificationCheckResult] = []
        for check in plan.checks:
            self._emit(VerificationCheckStarted(verification_id, check))
            try:
                process_result = self._process_runner.run(workspace, check.request)
            except ProcessRuntimeError as exc:
                result = VerificationCheckResult(
                    check_id=check.check_id,
                    name=check.name,
                    status=VerificationStatus.ERROR,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                results.append(result)
                self._emit(VerificationCheckFailed(verification_id, check, result))
                continue
            except Exception as exc:
                raise VerificationExecutionError(
                    f"Unexpected verification process failure: {exc}"
                ) from exc
            if not isinstance(process_result, ProcessResult):
                raise VerificationExecutionError("ProcessRunner returned an invalid ProcessResult")
            status = classify(check, process_result)
            result = VerificationCheckResult(
                check_id=check.check_id,
                name=check.name,
                status=status,
                process_result=process_result,
            )
            results.append(result)
            self._emit(VerificationCheckCompleted(verification_id, check, result))
        report = VerificationReport(
            verification_id=verification_id,
            plan_id=plan.plan_id,
            results=tuple(results),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        self._emit(VerificationCompleted(verification_id, report))
        return report
