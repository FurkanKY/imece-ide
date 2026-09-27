"""decision_runtime — the Jev System One decision layer (docs/JEV-DESIGN.md).

S1a (this slice): fully offline. No typesafe-sdk import, no API key read,
no network call anywhere in this package. `RuleDecisionBackend` is a
deterministic stand-in usable on its own (ui_prefs "rules") or as the
fallback every other backend must use (design rule 1). The seam for a
future `JevDecisionBackend` is `decision_runtime.ports.DecisionPort` — no
such class exists yet (see engine_factory.build_decision_backend).
"""

from decision_runtime.errors import (
    DecisionBackendError,
    DecisionInputError,
    DecisionRecordingError,
    DecisionRuntimeError,
)
from decision_runtime.fake_backend import FakeDecisionBackend
from decision_runtime.gate import VerificationFailureGate
from decision_runtime.models import (
    Answer,
    Choice,
    ChoiceAnswer,
    DecisionResult,
    DecisionSpec,
    Noul,
    NoulAnswer,
    Question,
    QuestionKind,
    Score,
    ScoreAnswer,
)
from decision_runtime.policy import ConfidenceBand, DecisionPolicy
from decision_runtime.ports import DecisionPort
from decision_runtime.recorder import CanonicalDecisionRecorder
from decision_runtime.triage import (
    FailureKind,
    RuleDecisionBackend,
    TriageAction,
    TriageFacts,
    TriageOutcome,
    build_triage_spec,
    build_triage_state,
    decide_triage_action,
    extract_error_block,
    missing_js_module,
    missing_python_module,
)

__all__ = [
    "DecisionRuntimeError",
    "DecisionInputError",
    "DecisionBackendError",
    "DecisionRecordingError",
    "QuestionKind",
    "Choice",
    "Score",
    "Noul",
    "Question",
    "Answer",
    "ChoiceAnswer",
    "ScoreAnswer",
    "NoulAnswer",
    "DecisionSpec",
    "DecisionResult",
    "DecisionPort",
    "ConfidenceBand",
    "DecisionPolicy",
    "FakeDecisionBackend",
    "CanonicalDecisionRecorder",
    "VerificationFailureGate",
    "FailureKind",
    "TriageAction",
    "TriageFacts",
    "TriageOutcome",
    "RuleDecisionBackend",
    "build_triage_spec",
    "build_triage_state",
    "decide_triage_action",
    "extract_error_block",
    "missing_python_module",
    "missing_js_module",
]
