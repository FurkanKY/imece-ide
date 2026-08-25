"""Recording port so review_runtime does not depend directly on run_runtime."""

from __future__ import annotations

from typing import Protocol

from agent_runtime.events import AgentLifecycleEvent
from review_runtime.models import ReviewReport


class ReviewRecorder(Protocol):
    def emit(self, event: AgentLifecycleEvent) -> None:
        """Record one transient AgentSession lifecycle event for this review attempt."""

    def complete(self, report: ReviewReport) -> None:
        """Record the terminal outcome of a successfully parsed review."""

    def fail(self, review_id: str, error_type: str, message: str) -> None:
        """Record a Reviewer infrastructure/protocol failure (not a semantic verdict)."""


class NullReviewRecorder:
    """A no-op ReviewRecorder for standalone Reviewer use outside RunRuntime."""

    def emit(self, event: AgentLifecycleEvent) -> None:
        return None

    def complete(self, report: ReviewReport) -> None:
        return None

    def fail(self, review_id: str, error_type: str, message: str) -> None:
        return None
