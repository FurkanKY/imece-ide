"""Typed failures for the end-to-end pipeline orchestration."""


class PipelineRuntimeError(Exception):
    """Base class for expected pipeline_runtime failures."""


class PipelineInputError(PipelineRuntimeError):
    """The caller supplied an invalid request/workspace/configuration value."""


class PipelineExecutionError(PipelineRuntimeError):
    """A Planner/Worker/Verification/Reviewer/FixLoop/ChangeProvider port
    failed unexpectedly, or returned evidence that violates the pipeline's
    provenance contract."""
