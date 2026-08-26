"""fix_runtime — bounded native Fix Loop orchestration (Milestone 3H).

FixLoopRunner is orchestration ("who/when"), not a second Agent harness
("how"): it depends only on small Worker/Verification/Reviewer/ChangeProvider
ports plus canonical Run bookkeeping.
"""

from fix_runtime.errors import (
    FixLoopExecutionError,
    FixLoopInputError,
    FixLoopRecordingError,
    FixLoopRuntimeError,
)
from fix_runtime.models import (
    DEFAULT_MAX_FIX_ATTEMPTS,
    MAX_MAX_FIX_ATTEMPTS,
    MIN_MAX_FIX_ATTEMPTS,
    FixAttemptResult,
    FixLoopReport,
    FixLoopRequest,
    FixLoopStatus,
    FixTrigger,
    FixTriggerKind,
    FixWorkerRequest,
    new_fix_attempt_id,
    new_fix_execution_id,
    new_fix_loop_id,
    validate_fix_loop_id,
)
from fix_runtime.ports import (
    ReviewAttemptRunner,
    VerificationAttemptRunner,
    WorkerAttemptResult,
    WorkerAttemptRunner,
)
from fix_runtime.prompt import MAX_FIX_INPUT_CHARS, render_fix_worker_input
from fix_runtime.runner import FixLoopRunner

__all__ = [
    "FixLoopRuntimeError",
    "FixLoopInputError",
    "FixLoopExecutionError",
    "FixLoopRecordingError",
    "FixTriggerKind",
    "FixLoopStatus",
    "FixTrigger",
    "FixWorkerRequest",
    "FixLoopRequest",
    "FixAttemptResult",
    "FixLoopReport",
    "DEFAULT_MAX_FIX_ATTEMPTS",
    "MIN_MAX_FIX_ATTEMPTS",
    "MAX_MAX_FIX_ATTEMPTS",
    "new_fix_loop_id",
    "new_fix_attempt_id",
    "new_fix_execution_id",
    "validate_fix_loop_id",
    "WorkerAttemptResult",
    "WorkerAttemptRunner",
    "VerificationAttemptRunner",
    "ReviewAttemptRunner",
    "MAX_FIX_INPUT_CHARS",
    "render_fix_worker_input",
    "FixLoopRunner",
]
