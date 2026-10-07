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
    """Cancellation was observed mid-verification. The matching check ID and
    producer_quiescent flag record whether a supervisor authenticated process
    tree reaping; cancellation is never represented as a PASS result."""

    plan_id: str
    reason: str
    check_id: str | None = None
    producer_quiescent: bool = False


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
