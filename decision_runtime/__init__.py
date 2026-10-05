"""decision_runtime — the Jev System One decision layer (docs/JEV-DESIGN.md).

S1a (offline skeleton): `RuleDecisionBackend` (deterministic), fake backend,
canonical `decision.made` recording, baseline verification rerun and the
`VerificationFailureGate` seam. S1b adds `JevDecisionBackend` — the real
TypeSafe (typesafe-sdk) backend — which is deliberately LAZY: importing this
package never imports typesafe-sdk and never reads TYPESAFE_API_KEY; both
are only touched when a decide() call actually runs, and any missing
key/SDK/remote failure surfaces as a typed `DecisionBackendError` that
callers map to the deterministic rule fallback (design rule 1). Remote state
leaves only through `decision_runtime.remote_state` (allowlist + redaction).
"""

from decision_runtime.errors import (
    DecisionBackendError,
    DecisionBackendFailureReason,
    DecisionInputError,
    DecisionRecordingError,
    DecisionRuntimeError,
)
from decision_runtime.fake_backend import FakeDecisionBackend
from decision_runtime.gate import VerificationFailureGate
from decision_runtime.jev_backend import JevDecisionBackend
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
from decision_runtime.remote_state import sanitize_process_output, sanitize_remote_state
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
    "DecisionBackendFailureReason",
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
    "JevDecisionBackend",
    "sanitize_process_output",
    "sanitize_remote_state",
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
