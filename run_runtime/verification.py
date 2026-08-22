"""Canonical adapter for transient deterministic verification events."""

from __future__ import annotations

from typing import Any

from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime
from verification_runtime.events import (
    VerificationCheckCompleted,
    VerificationCheckFailed,
    VerificationCheckStarted,
    VerificationCompleted,
    VerificationEvent,
    VerificationStarted,
)

SOURCE = "verification"


class CanonicalVerificationEventSink:
    """Persist one verification attempt through the canonical RunRuntime."""

    def __init__(self, runtime: RunRuntime, run_id: str, *, verification_id: str) -> None:
        if not isinstance(verification_id, str) or not verification_id:
            raise ValueError("verification_id must be non-empty")
        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"Verification sink requires RUNNING run, got {run.status}")
        self._runtime = runtime
        self._run_id = run_id
        self._verification_id = verification_id
        self._expected_seq = run.last_event_seq

    @property
    def verification_id(self) -> str:
        return self._verification_id

    @property
    def expected_last_event_seq(self) -> int:
        return self._expected_seq

    def emit(self, event: VerificationEvent) -> None:
        if not isinstance(event, VerificationEvent):
            raise TypeError("CanonicalVerificationEventSink accepts VerificationEvent values only")
        if event.verification_id != self._verification_id:
            raise ValueError("Verification event ID does not match this sink")
        if isinstance(event, VerificationStarted):
            self._reject_reused_verification_id()
        specs = self._specs(event)
        if not specs:
            raise ValueError(f"Unsupported verification event: {type(event).__name__}")
        committed, _ = self._runtime.record_many(
            run_id=self._run_id,
            specs=tuple(specs),
            expected_last_event_seq=self._expected_seq,
        )
        self._expected_seq = committed[-1].seq

    def _reject_reused_verification_id(self) -> None:
        after_seq = 0
        while True:
            page = self._runtime.events(self._run_id, after_seq=after_seq, limit=200)
            for existing in page.events:
                if (
                    existing.type == RunEventType.VERIFICATION_STARTED
                    and existing.payload.get("verification_id") == self._verification_id
                ):
                    raise ValueError(
                        f"verification_id already started in run: {self._verification_id}"
                    )
            if not page.has_more:
                return
            after_seq = page.events[-1].seq

    def _spec(
        self,
        event: VerificationEvent,
        event_type: str,
        payload: dict[str, Any],
        *,
        item_id: str | None = None,
    ) -> RunEventSpec:
        return RunEventSpec(
            type=event_type,
            payload=payload,
            item_id=item_id,
            correlation_id=self._verification_id,
            source=SOURCE,
        )

    def _check_item_id(self, verification_id: str, check_id: str) -> str:
        return f"verification:{verification_id}:{check_id}"

    @staticmethod
    def _result_payload(event, *, include_error: bool = False) -> dict[str, Any]:
        result = event.result
        payload: dict[str, Any] = {
            "verification_id": event.verification_id,
            "check_id": result.check_id,
            "name": result.name,
            "status": result.status.value,
            "execution_isolation": "host",
        }
        if include_error:
            payload.update({
                "error_type": result.error_type,
                "error_message": result.error_message,
            })
        if result.process_result is not None:
            process = result.process_result
            payload.update({
                "exit_code": process.exit_code,
                "timed_out": process.timed_out,
                "duration_ms": process.duration_ms,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "stdout_truncated": process.stdout_truncated,
                "stderr_truncated": process.stderr_truncated,
                "stdout_bytes": process.stdout_bytes,
                "stderr_bytes": process.stderr_bytes,
            })
        return payload

    def _specs(self, event: VerificationEvent) -> list[RunEventSpec]:
        if isinstance(event, VerificationStarted):
            return [self._spec(event, RunEventType.VERIFICATION_STARTED, {
                "verification_id": event.verification_id,
                "plan_id": event.plan_id,
                "check_count": event.check_count,
            })]
        if isinstance(event, VerificationCheckStarted):
            check = event.check
            request = check.request
            return [self._spec(event, RunEventType.VERIFICATION_CHECK_STARTED, {
                "verification_id": event.verification_id,
                "check_id": check.check_id,
                "name": check.name,
                "argv": list(request.argv),
                "cwd": request.cwd,
                "timeout_ms": request.timeout_ms,
                "pass_exit_codes": list(check.pass_exit_codes),
                "error_exit_codes": list(check.error_exit_codes),
                "env_override_keys": sorted(request.env),
            }, item_id=self._check_item_id(event.verification_id, check.check_id))]
        if isinstance(event, VerificationCheckCompleted):
            self._validate_check_event(event, failed=False)
            return [self._spec(
                event,
                RunEventType.VERIFICATION_CHECK_COMPLETED,
                self._result_payload(event),
                item_id=self._check_item_id(event.verification_id, event.check.check_id),
            )]
        if isinstance(event, VerificationCheckFailed):
            self._validate_check_event(event, failed=True)
            return [self._spec(
                event,
                RunEventType.VERIFICATION_CHECK_FAILED,
                self._result_payload(event, include_error=True),
                item_id=self._check_item_id(event.verification_id, event.check.check_id),
            )]
        if isinstance(event, VerificationCompleted):
            report = event.report
            if report.verification_id != event.verification_id:
                raise ValueError("Verification report ID does not match this sink")
            return [self._spec(event, RunEventType.VERIFICATION_COMPLETED, {
                "verification_id": report.verification_id,
                "plan_id": report.plan_id,
                "status": report.status.value,
                "duration_ms": report.duration_ms,
                "counts": {
                    "pass": report.passed,
                    "fail": report.failed,
                    "timeout": report.timed_out,
                    "error": report.errors,
                    "total": report.total,
                },
            })]
        raise ValueError(f"Unsupported verification event: {type(event).__name__}")

    @staticmethod
    def _validate_check_event(event, *, failed: bool) -> None:
        if (
            event.result.check_id != event.check.check_id
            or event.result.name != event.check.name
        ):
            raise ValueError("Verification check and result identity do not match")
        if failed:
            if (
                event.result.status.value != "error"
                or event.result.process_result is not None
                or not event.result.error_type
                or event.result.error_message is None
            ):
                raise ValueError("VerificationCheckFailed requires infrastructure ERROR evidence")
        elif event.result.process_result is None:
            raise ValueError("VerificationCheckCompleted requires ProcessResult")
