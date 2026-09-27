"""CanonicalDecisionRecorder — appends one `decision.made` RunEvent per decide() call.

This is the audit trail and eventual tuning dataset design rule 5 asks for:
decision id, question set version, model version, answers, confidence,
latency, tokens, and whether the deterministic fallback was used. Like
run_runtime.reviewer.CanonicalReviewEventSink, this never touches
execution.*/verification.*/fix_loop.* events, and — like agent.activity is
described as being treated elsewhere — `decision.made` is intentionally
absent from run_runtime.projector's handler table and from every
execution-activity check in run_runtime.completion, so recording a decision
can never move a Run's projected phase/status or count as fresh execution
activity for staleness purposes (see run_runtime.events.RunEventType.DECISION_MADE).
"""

from __future__ import annotations

from typing import Any

from decision_runtime.models import ChoiceAnswer, DecisionResult, NoulAnswer, ScoreAnswer
from run_runtime.events import RunEventType
from run_runtime.service import RunRuntime

SOURCE = "decision_runtime"


def _answer_payload(answer: ChoiceAnswer | ScoreAnswer | NoulAnswer) -> dict[str, Any]:
    if isinstance(answer, ChoiceAnswer):
        return {
            "kind": "choice",
            "choice": answer.choice,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "kind": "score",
            "score": answer.score,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    return {"kind": "noul", "noul": answer.noul}


def decision_result_payload(result: DecisionResult) -> dict[str, Any]:
    """The canonical `decision.made` payload for a DecisionResult (design rule 5)."""
    return {
        "decision_id": result.decision_id,
        "question_set_version": result.question_set_version,
        "backend": result.backend,
        "model_version": result.model_version,
        "latency_ms": result.latency_ms,
        "prompt_tokens": result.prompt_tokens,
        "fallback_used": result.fallback_used,
        "answers": {name: _answer_payload(answer) for name, answer in result.answers.items()},
    }


class CanonicalDecisionRecorder:
    """Records `decision.made` for a RUNNING Run. Stateless across calls (unlike
    the fix-loop/review recorders, a decision has no started/terminal lifecycle
    — it is always exactly one event)."""

    def __init__(self, runtime: RunRuntime, run_id: str) -> None:
        self._runtime = runtime
        self._run_id = run_id

    def record(self, result: DecisionResult) -> None:
        if not isinstance(result, DecisionResult):
            raise TypeError("CanonicalDecisionRecorder.record requires a DecisionResult")
        self._runtime.record(
            run_id=self._run_id,
            type=RunEventType.DECISION_MADE,
            payload=decision_result_payload(result),
            execution_id=None,
            correlation_id=result.decision_id,
            source=SOURCE,
        )
