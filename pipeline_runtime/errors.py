"""Typed failures for the end-to-end pipeline orchestration."""


class PipelineRuntimeError(Exception):
    """Base class for expected pipeline_runtime failures."""


class PipelineInputError(PipelineRuntimeError):
    """The caller supplied an invalid request/workspace/configuration value."""


class PipelineExecutionError(PipelineRuntimeError):
    """A Planner/Worker/Verification/Reviewer/FixLoop/ChangeProvider port
    failed unexpectedly, or returned evidence that violates the pipeline's
    provenance contract."""


from agent_runtime.cancellation import OperationCancelledError


class PipelineCancelledError(PipelineExecutionError, OperationCancelledError):
    """A Planner/Worker/Verification/Reviewer/FixLoop port was cancelled via
    a CancellationToken. PipelineRunner catches this specifically (never the
    generic PipelineExecutionError path) to record a cancelled outcome
    rather than a failure."""
