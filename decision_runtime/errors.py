"""Typed failures for decision_runtime."""

from __future__ import annotations

from enum import StrEnum


class DecisionRuntimeError(Exception):
    """Base class for expected decision_runtime failures."""


class DecisionInputError(DecisionRuntimeError):
    """The caller supplied an invalid DecisionSpec/state/DecisionResult value."""


class DecisionBackendFailureReason(StrEnum):
    """Why a DecisionPort backend could not answer.

    A DecisionBackendError always carries one of these reasons so callers can
    tell a missing setup (key/SDK) from a remote failure without ever having
    to inspect an exception message — messages are fixed, safe strings and
    never contain the API key, response bodies, or raw server error text.
    """

    MISSING_API_KEY = "missing_api_key"
    MISSING_SDK = "missing_sdk"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    MALFORMED_RESPONSE = "malformed_response"
    REQUEST_INVALID = "request_invalid"
    STATE_REJECTED = "state_rejected"


class DecisionBackendError(DecisionRuntimeError):
    """A DecisionPort implementation failed or returned an invalid result.

    This is a normal, EXPECTED outcome for an accelerator backend (timeout,
    API error, malformed response) — see docs/JEV-DESIGN.md design rule 1:
    callers must treat it as a signal to fall back to RuleDecisionBackend,
    never let it propagate as a hard failure of the surrounding pipeline.

    `message` MUST be a fixed, safe string (no exception text, no response
    bodies, no API key — raw server bodies and the request payload are both
    suppressed); `reason` is the typed category. The single-message
    constructor stays backwards compatible with S1a call sites
    (`DecisionBackendError("boom")` -> reason=None).
    """

    def __init__(self, message: str, *, reason: DecisionBackendFailureReason | str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class DecisionRecordingError(DecisionRuntimeError):
    """The canonical decision.made event could not be recorded."""
