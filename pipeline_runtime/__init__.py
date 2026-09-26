"""pipeline_runtime — end-to-end plan -> initial attempt -> verify -> review
-> (fix loop) -> terminal outcome orchestration.

PipelineRunner is the missing piece that runs the FIRST Worker attempt:
fix_runtime.FixLoopRunner only ever starts from a failure trigger
(VERIFICATION_FAIL/REVIEW_NEEDS_FIX). PipelineRunner composes the
already-existing canonical Planner/Worker/Verification/Reviewer/FixLoop
pieces so a Run can go from a bare task string to a terminal outcome. See
pipeline_runtime.runner for the full flow and the "no verification plan"
design decision.
"""

from pipeline_runtime.acp_planner import AcpPlanAttemptRunner
from pipeline_runtime.errors import PipelineExecutionError, PipelineInputError, PipelineRuntimeError
from pipeline_runtime.models import PipelineReport, PipelineStatus
from pipeline_runtime.native_planner import NativePlanAttemptRunner
from pipeline_runtime.ports import PlanAttemptRunner
from pipeline_runtime.runner import PipelineRunner
from pipeline_runtime.verification_detect import detect_verification_plan

__all__ = [
    "PipelineRuntimeError",
    "PipelineInputError",
    "PipelineExecutionError",
    "PipelineStatus",
    "PipelineReport",
    "PlanAttemptRunner",
    "NativePlanAttemptRunner",
    "AcpPlanAttemptRunner",
    "PipelineRunner",
    "detect_verification_plan",
]
