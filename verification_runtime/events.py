"""Transient deterministic verification lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from verification_runtime.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationPlan,
    VerificationReport,
)


@dataclass(frozen=True, slots=True)
class VerificationEvent:
    verification_id: str


@dataclass(frozen=True, slots=True)
class VerificationStarted(VerificationEvent):
    plan_id: str
    check_count: int


@dataclass(frozen=True, slots=True)
class VerificationCheckStarted(VerificationEvent):
    check: VerificationCheck


@dataclass(frozen=True, slots=True)
class VerificationCheckCompleted(VerificationEvent):
    check: VerificationCheck
    result: VerificationCheckResult


@dataclass(frozen=True, slots=True)
class VerificationCheckFailed(VerificationEvent):
    check: VerificationCheck
    result: VerificationCheckResult


@dataclass(frozen=True, slots=True)
class VerificationCompleted(VerificationEvent):
    report: VerificationReport


@dataclass(frozen=True, slots=True)
class VerificationInterrupted(VerificationEvent):
    """A cancellation was observed mid-verification (between or during a
    check, via a CancellationToken); the in-flight check's process tree has
    already been terminated by the time this is emitted. Terminal for this
    verification attempt, exactly like VerificationCompleted -- this is the
    "the run was cancelled, not that verification failed" outcome."""

    plan_id: str
    reason: str


VerificationLifecycleEvent: TypeAlias = (
    VerificationStarted
    | VerificationCheckStarted
    | VerificationCheckCompleted
    | VerificationCheckFailed
    | VerificationCompleted
    | VerificationInterrupted
)


class VerificationEventSink(Protocol):
    def emit(self, event: VerificationLifecycleEvent) -> None:
        """Synchronously record one verification lifecycle event."""


class NullVerificationEventSink:
    def emit(self, event: VerificationLifecycleEvent) -> None:
        return None
