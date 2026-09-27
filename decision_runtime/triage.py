"""Verification failure triage v1 (docs/JEV-DESIGN.md "Spike S1").

Deterministic facts (exit code, timeout flag, command, extracted error
block, changed paths, baseline result) are always computed IN CODE (design
rule 3) — RuleDecisionBackend below only judges from those facts, exactly
like a future JevDecisionBackend would judge from the same filtered state
(design rule 4: send only the fields a question needs).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from decision_runtime.models import (
    Choice,
    ChoiceAnswer,
    DecisionResult,
    DecisionSpec,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
)
from decision_runtime.policy import ConfidenceBand, DecisionPolicy

QUESTION_SET_VERSION = "verification_failure_triage.v1"
_MAX_ERROR_BLOCK_LINES = 40
_MAX_ERROR_BLOCK_CHARS = 4_000
_MAX_COMMAND_TOKENS = 32
_MAX_CHANGED_PATHS = 50


class FailureKind(StrEnum):
    """The 6 labels from docs/JEV-DESIGN.md's failure_kind Choice."""

    CODE_BUG = "code_bug"
    TEST_NEEDS_UPDATE = "test_needs_update"
    MISSING_DEPENDENCY = "missing_dependency"
    ENVIRONMENT_OR_TOOLING = "environment_or_tooling"
    FLAKY_OR_TIMEOUT = "flaky_or_timeout"
    UNRELATED_PREEXISTING = "unrelated_preexisting"


_FAILURE_KIND_DESCRIPTIONS: dict[str, str] = {
    FailureKind.CODE_BUG: "The change itself is wrong; the failure is a real bug introduced by it.",
    FailureKind.TEST_NEEDS_UPDATE: "The test's expectations must change per the task, not the code.",
    FailureKind.MISSING_DEPENDENCY: "A required package/module is missing from the environment.",
    FailureKind.ENVIRONMENT_OR_TOOLING: "Interpreter, PATH, config, or a required service is missing/broken.",
    FailureKind.FLAKY_OR_TIMEOUT: "The check is flaky or exceeded its time budget, independent of the change.",
    FailureKind.UNRELATED_PREEXISTING: "The failure already exists on the pre-change baseline.",
}

_FIXABLE_LEVELS = ("needs the user", "uncertain / partially fixable", "clearly fixable in code")


class TriageAction(StrEnum):
    """The pipeline actions from docs/JEV-DESIGN.md's Spike S1 action table."""

    CONTINUE_FIX_LOOP = "continue_fix_loop"
    NEEDS_USER = "needs_user"
    RERUN_VERIFICATION_ONCE = "rerun_verification_once"
    MARK_PRE_EXISTING = "mark_pre_existing"


# ---------------- deterministic facts (design rule 3: code does the math) ----------------


_TRACEBACK_START_RE = re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE)
_ASSERTION_RE = re.compile(r"^.*Assert(ion)?Error.*$|^E\s+assert.*$", re.MULTILINE)
_COMMAND_NOT_FOUND_RE = re.compile(
    r"^.*(?:command not found|: not found|is not recognized as an internal or external command).*$",
    re.MULTILINE | re.IGNORECASE,
)
_PY_MODULE_NOT_FOUND_RE = re.compile(r"^.*(?:ModuleNotFoundError|No module named).*$", re.MULTILINE)
_JS_MODULE_NOT_FOUND_RE = re.compile(r"^.*Cannot find module.*$", re.MULTILINE)
_GO_PACKAGE_NOT_FOUND_RE = re.compile(
    r"^.*(?:cannot find package|no required module provides package).*$", re.MULTILINE
)


def extract_error_block(stdout: str, stderr: str, *, max_lines: int = _MAX_ERROR_BLOCK_LINES) -> str:
    """Deterministically extract the last traceback / assertion / "command not
    found" / missing-module lines from process output, bounded to
    `max_lines` lines and _MAX_ERROR_BLOCK_CHARS characters.

    Order of preference (most specific/actionable signal first): the LAST
    Python traceback block in the combined output, else the last assertion
    line, else the last "command not found" line, else the last
    missing-module line (Python/Node/Go), else — nothing matched — the last
    `max_lines` lines of stderr (or stdout if stderr is empty), as a
    generic fallback so callers always have *something* to look at.
    """
    combined = f"{stdout}\n{stderr}" if stdout and stderr else (stdout or stderr)
    lines = combined.splitlines()

    tb_starts = [m.start() for m in _TRACEBACK_START_RE.finditer(combined)]
    if tb_starts:
        block = combined[tb_starts[-1]:]
        return _bounded_lines(block.splitlines(), max_lines)

    for pattern in (_ASSERTION_RE, _COMMAND_NOT_FOUND_RE, _PY_MODULE_NOT_FOUND_RE,
                    _JS_MODULE_NOT_FOUND_RE, _GO_PACKAGE_NOT_FOUND_RE):
        matches = list(pattern.finditer(combined))
        if matches:
            last = matches[-1]
            line_index = combined.count("\n", 0, last.start())
            start = max(0, line_index - 2)
            return _bounded_lines(lines[start:line_index + 1], max_lines)

    return _bounded_lines(lines[-max_lines:], max_lines)


def _bounded_lines(lines: list[str], max_lines: int) -> str:
    bounded = lines[-max_lines:]
    text = "\n".join(bounded)
    if len(text) > _MAX_ERROR_BLOCK_CHARS:
        text = text[-_MAX_ERROR_BLOCK_CHARS:]
    return text


def missing_python_module(error_block: str) -> str | None:
    """The module name from a Python "No module named 'X'" line, if present."""
    match = re.search(r"No module named ['\"]?([\w.]+)", error_block)
    return match.group(1) if match else None


def missing_js_module(error_block: str) -> str | None:
    """The module specifier from a Node/Jest "Cannot find module 'X'" line, if present."""
    match = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", error_block)
    return match.group(1) if match else None


def _is_project_local_python_module(module: str, changed_paths: list) -> bool:
    """Design rule per docs/JEV-DESIGN.md: only a "No module named" of a
    NON-PROJECT module is missing_dependency — a missing import whose name
    matches one of the files this very change touched is much more likely a
    code_bug (e.g. the diff renamed/moved a module and forgot to update an
    import), computed in code from the changed_paths fact rather than
    guessed by the classifier."""
    top_level = module.split(".")[0]
    for path in changed_paths:
        first_component = path.split("/", 1)[0]
        stem = path[:-3] if path.endswith(".py") else path
        if first_component == top_level or stem.replace("/", ".") == module:
            return True
    return False


def _is_project_local_js_module(module: str) -> bool:
    """A relative/path specifier ('./foo', '../bar', '/abs') is this project's
    own file, never a missing third-party dependency."""
    return module.startswith((".", "/"))


@dataclass(frozen=True, slots=True)
class TriageFacts:
    """Deterministic, code-computed facts for one failing verification check."""

    check_id: str
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    error_block: str
    changed_paths: tuple[str, ...]
    baseline_status: str | None = None  # "pass" | "fail" | "timeout" | "error" | None (unknown/not run)
    baseline_exit_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command)[:_MAX_COMMAND_TOKENS])
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths)[:_MAX_CHANGED_PATHS])
        object.__setattr__(self, "error_block", self.error_block[-_MAX_ERROR_BLOCK_CHARS:])


def build_triage_state(facts: TriageFacts) -> dict[str, Any]:
    """Small, filtered state (design rule 4) — only what the triage questions need."""
    return {
        "command": list(facts.command),
        "exit_code": facts.exit_code,
        "timed_out": facts.timed_out,
        "error_block": facts.error_block,
        "changed_paths": list(facts.changed_paths),
        "baseline_status": facts.baseline_status,
        "baseline_exit_code": facts.baseline_exit_code,
    }


def build_triage_spec(decision_id: str) -> DecisionSpec:
    """The one-call question set from docs/JEV-DESIGN.md's Spike S1 (one Choice, one Noul, one Score)."""
    return DecisionSpec(
        decision_id=decision_id,
        question_set_version=QUESTION_SET_VERSION,
        questions={
            "failure_kind": Choice(
                instructions=(
                    "A verification check failed. Given the command, exit code, timeout "
                    "flag, extracted error block, changed paths, and whether the SAME "
                    "check also fails on the pre-change baseline, classify why."
                ),
                criteria=dict(_FAILURE_KIND_DESCRIPTIONS),
            ),
            "caused_by_change": Noul(
                instructions="Is the failure plausibly caused by the changed files, given their paths and the error location?",
            ),
            "fixable_by_agent": Score(
                instructions="How fixable is this in code by an automated coding agent, without user input?",
                criteria=_FIXABLE_LEVELS,
            ),
        },
    )


# ---------------- RuleDecisionBackend (deterministic fallback) ----------------


def _choice_answer(chosen: str, pmax: float, *, labels: tuple[str, ...]) -> ChoiceAnswer:
    """Build a Choice answer whose confidence follows TypeSafe's stated shape
    (docs/JEV-DESIGN.md: "(3*pmax - 1)/2" for 3 options), generalized to n
    options as `(n*pmax - 1) / (n - 1)` — the same "how far pmax sits above
    uniform" shape, clipped to [0, 1]."""
    n = len(labels)
    remainder = (1.0 - pmax) / (n - 1) if n > 1 else 0.0
    probabilities = {label: (pmax if label == chosen else remainder) for label in labels}
    confidence = max(0.0, min(1.0, (n * pmax - 1) / (n - 1))) if n > 1 else 1.0
    return ChoiceAnswer(choice=chosen, probabilities=probabilities, confidence=confidence)


def _score_answer(score: float, *, confidence: float) -> ScoreAnswer:
    levels = len(_FIXABLE_LEVELS)
    lo = max(0, min(levels - 1, round(score)))
    probabilities = {str(i): (0.8 if i == lo else 0.2 / max(1, levels - 1)) for i in range(levels)}
    return ScoreAnswer(score=float(score), probabilities=probabilities, confidence=confidence)


class RuleDecisionBackend:
    """Deterministic fallback DecisionPort: no model, no network — pure rules.

    Used both as decision_runtime's OWN fallback (design rule 1: every
    decision point has a deterministic fallback) and as the whole backend
    for the "rules" ui_prefs.decision_layer setting, and it's what "jev"
    currently falls back to (no JevDecisionBackend exists yet — see
    engine_factory.build_decision_backend).
    """

    BACKEND_NAME = "rule"
    MODEL_VERSION = "rule-v1"

    def decide(self, spec: DecisionSpec, state: dict) -> DecisionResult:
        started = time.monotonic()
        labels = tuple(_FAILURE_KIND_DESCRIPTIONS.keys())
        kind, kind_confidence, caused_by_change, fixable = self._classify(state)
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return DecisionResult(
            decision_id=spec.decision_id,
            question_set_version=spec.question_set_version,
            answers={
                "failure_kind": _choice_answer(kind.value, kind_confidence, labels=labels),
                "caused_by_change": NoulAnswer(noul=caused_by_change),
                "fixable_by_agent": _score_answer(fixable, confidence=kind_confidence),
            },
            backend=self.BACKEND_NAME,
            model_version=self.MODEL_VERSION,
            latency_ms=latency_ms,
            prompt_tokens=0,
            fallback_used=False,
        )

    @staticmethod
    def _classify(state: dict) -> tuple[FailureKind, float, float, float]:
        error_block = state.get("error_block") or ""
        timed_out = bool(state.get("timed_out"))
        baseline_status = state.get("baseline_status")
        changed_paths = state.get("changed_paths") or []

        # Rule 1 (strongest): the SAME check also fails on the pre-change
        # baseline -> the failure predates this change entirely.
        if baseline_status in ("fail", "timeout", "error"):
            return FailureKind.UNRELATED_PREEXISTING, 0.99, 0.02, 0.0

        # Rule 2: the shell couldn't find the command/tool itself.
        if _COMMAND_NOT_FOUND_RE.search(error_block):
            caused = 0.7 if changed_paths else 0.3
            return FailureKind.ENVIRONMENT_OR_TOOLING, 0.97, caused, 0.0

        # Rule 3: a Python/Node/Go module/package the code imports is missing
        # — but ONLY when it's a NON-PROJECT module (docs/JEV-DESIGN.md): a
        # missing import matching a path this very change touched is much
        # more likely the change itself breaking an internal import.
        py_module = missing_python_module(error_block)
        if py_module is not None:
            if _is_project_local_python_module(py_module, changed_paths):
                return FailureKind.CODE_BUG, 0.9, 0.9, 2.0
            return FailureKind.MISSING_DEPENDENCY, 0.95, 0.2, 0.0
        js_module = missing_js_module(error_block)
        if js_module is not None:
            if _is_project_local_js_module(js_module):
                return FailureKind.CODE_BUG, 0.9, 0.9, 2.0
            return FailureKind.MISSING_DEPENDENCY, 0.95, 0.2, 0.0
        if _GO_PACKAGE_NOT_FOUND_RE.search(error_block):
            caused = 0.7 if changed_paths else 0.3
            return FailureKind.MISSING_DEPENDENCY, 0.95, caused, 0.0

        # Rule 4: the check exceeded its time budget.
        if timed_out:
            return FailureKind.FLAKY_OR_TIMEOUT, 0.95, 0.4, 1.0

        # Default: no strong deterministic signal — guess code_bug, but with
        # LOW confidence so callers fall back to today's behaviour (design
        # rule 1 / docs/JEV-DESIGN.md action table: "low confidence -> today's
        # behaviour").
        caused = 0.6 if changed_paths else 0.3
        return FailureKind.CODE_BUG, 0.3, caused, 1.5


# ---------------- action mapping (policy) ----------------


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    action: TriageAction
    failure_kind: str
    confidence: float
    result: DecisionResult
    needs_user_message: str | None = None


_NEEDS_USER_MESSAGES: dict[FailureKind, str] = {
    FailureKind.MISSING_DEPENDENCY: (
        "Doğrulama adımı gerekli bir bağımlılık eksik olduğu için başarısız oldu. "
        "Lütfen projenin bağımlılıklarını kurup tekrar deneyin."
    ),
    FailureKind.ENVIRONMENT_OR_TOOLING: (
        "Doğrulama adımı ortam/araç sorunu nedeniyle çalıştırılamadı (ör. eksik komut, "
        "yanlış PATH veya çalışmayan bir servis). Lütfen ortamınızı kontrol edin."
    ),
}


def decide_triage_action(result: DecisionResult, policy: DecisionPolicy) -> TriageOutcome:
    """Map a triage DecisionResult to a pipeline action (docs/JEV-DESIGN.md's
    Spike S1 action table). Only `failure_kind`'s own confidence gates the
    action — a decision the policy isn't confident in about is exactly the
    "low confidence -> today's behaviour" row, before it even reaches the
    other kind-specific rows."""
    failure_kind = result.answers["failure_kind"].choice
    confidence = result.confidence_of("failure_kind")
    band = policy.band(confidence)

    if band is not ConfidenceBand.ACT:
        return TriageOutcome(
            action=TriageAction.CONTINUE_FIX_LOOP, failure_kind=failure_kind, confidence=confidence, result=result,
        )

    try:
        kind = FailureKind(failure_kind)
    except ValueError:
        return TriageOutcome(
            action=TriageAction.CONTINUE_FIX_LOOP, failure_kind=failure_kind, confidence=confidence, result=result,
        )

    if kind in _NEEDS_USER_MESSAGES:
        return TriageOutcome(
            action=TriageAction.NEEDS_USER, failure_kind=failure_kind, confidence=confidence, result=result,
            needs_user_message=_NEEDS_USER_MESSAGES[kind],
        )
    if kind is FailureKind.FLAKY_OR_TIMEOUT:
        return TriageOutcome(
            action=TriageAction.RERUN_VERIFICATION_ONCE, failure_kind=failure_kind, confidence=confidence, result=result,
        )
    if kind is FailureKind.UNRELATED_PREEXISTING:
        return TriageOutcome(
            action=TriageAction.MARK_PRE_EXISTING, failure_kind=failure_kind, confidence=confidence, result=result,
        )
    # CODE_BUG / TEST_NEEDS_UPDATE: fix loop as today, with classification
    # attached to the fix prompt by the caller (see fix_runtime.runner).
    return TriageOutcome(
        action=TriageAction.CONTINUE_FIX_LOOP, failure_kind=failure_kind, confidence=confidence, result=result,
    )
