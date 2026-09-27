"""Typed failures for decision_runtime."""

from __future__ import annotations


class DecisionRuntimeError(Exception):
    """Base class for expected decision_runtime failures."""


class DecisionInputError(DecisionRuntimeError):
    """The caller supplied an invalid DecisionSpec/state/DecisionResult value."""


class DecisionBackendError(DecisionRuntimeError):
    """A DecisionPort implementation failed or returned an invalid result.

    This is a normal, EXPECTED outcome for an accelerator backend (timeout,
    API error, malformed response) — see docs/JEV-DESIGN.md design rule 1:
    callers must treat it as a signal to fall back to RuleDecisionBackend,
    never let it propagate as a hard failure of the surrounding pipeline.
    """


class DecisionRecordingError(DecisionRuntimeError):
    """The canonical decision.made event could not be recorded."""
