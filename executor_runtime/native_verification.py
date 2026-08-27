"""NativeVerificationAttemptAdapter — thin HOW-adapter binding
fix_runtime.ports.VerificationAttemptRunner to the existing deterministic
VerificationRunner.
"""

from __future__ import annotations

from process_runtime import ProcessRunner

from run_runtime.service import RunRuntime
from run_runtime.verification import CanonicalVerificationEventSink
from verification_runtime.models import VerificationPlan, VerificationReport
from verification_runtime.runner import VerificationRunner

from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError


class NativeVerificationAttemptAdapter:
    """Runs exactly one fresh deterministic verification attempt.

    Concrete production implementation of
    fix_runtime.ports.VerificationAttemptRunner.
    """

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        *,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ExecutorAdapterInputError("NativeVerificationAttemptAdapter.run_id must be a non-empty string.")
        self._runtime = runtime
        self._run_id = run_id
        self._process_runner = process_runner or ProcessRunner()

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(
        self, workspace, plan: VerificationPlan, *, verification_id: str
    ) -> VerificationReport:
        if not isinstance(plan, VerificationPlan):
            raise ExecutorAdapterInputError("NativeVerificationAttemptAdapter.run requires a VerificationPlan.")

        try:
            sink = CanonicalVerificationEventSink(self._runtime, self._run_id, verification_id=verification_id)
        except ValueError as exc:
            raise ExecutorAdapterInputError(f"Invalid verification_id: {exc}") from exc

        runner = VerificationRunner(self._process_runner, event_sink=sink)
        try:
            report = runner.run(workspace, plan, verification_id=verification_id)
        except Exception as exc:
            raise ExecutorAdapterExecutionError(f"Verification port failed: {exc}") from exc

        if not isinstance(report, VerificationReport) or report.verification_id != verification_id:
            raise ExecutorAdapterExecutionError(
                "VerificationRunner did not return the requested verification_id."
            )
        return report
