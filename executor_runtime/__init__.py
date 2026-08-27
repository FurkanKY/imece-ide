"""executor_runtime — native production adapters binding the 3H attempt
ports (fix_runtime.ports) to the existing native Agent/Verification/
Reviewer runtimes.

These adapters answer only HOW an existing WorkerAttemptRunner/
VerificationAttemptRunner/ReviewAttemptRunner port invokes the already-
existing production runtime (AgentSession / VerificationRunner /
ReviewerRunner) — never WHEN it should run or WHICH executor/provider should
be selected. See
docs/superpowers/specs/2026-08-27-native-attempt-adapters-design.md.
"""

from executor_runtime.errors import (
    ExecutorAdapterError,
    ExecutorAdapterExecutionError,
    ExecutorAdapterInputError,
)
from executor_runtime.native_reviewer import NativeReviewAttemptAdapter
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from executor_runtime.native_worker import NativeWorkerAttemptAdapter

__all__ = [
    "ExecutorAdapterError",
    "ExecutorAdapterInputError",
    "ExecutorAdapterExecutionError",
    "NativeWorkerAttemptAdapter",
    "NativeVerificationAttemptAdapter",
    "NativeReviewAttemptAdapter",
]
