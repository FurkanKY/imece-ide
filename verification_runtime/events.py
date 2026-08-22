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


VerificationLifecycleEvent: TypeAlias = (
    VerificationStarted
    | VerificationCheckStarted
    | VerificationCheckCompleted
    | VerificationCheckFailed
    | VerificationCompleted
)


class VerificationEventSink(Protocol):
    def emit(self, event: VerificationLifecycleEvent) -> None:
        """Synchronously record one verification lifecycle event."""


class NullVerificationEventSink:
    def emit(self, event: VerificationLifecycleEvent) -> None:
        return None
