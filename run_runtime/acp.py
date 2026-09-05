"""Canonical RunEvent sink for one ACP Worker execution."""

from __future__ import annotations

from typing import Any

from acp_runtime.events import (
    AcpPermissionRequested,
    AcpPermissionResolved,
    AcpSessionUpdateObserved,
)
from acp_runtime.models import AcpRunResult

from run_runtime.events import RunEventSpec, RunEventType
from run_runtime.jsonutil import validate_json_value
from run_runtime.models import RunStatus
from run_runtime.service import RunRuntime


SOURCE = "acp_worker"
MAX_CANONICAL_ACP_TEXT_CHARS = 2_000
MAX_CANONICAL_ACP_PERMISSION_OPTIONS = 128


class CanonicalAcpEventSink:
    """Record one ACP execution with an optimistic event-sequence cursor."""

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        *,
        execution_id: str,
    ) -> None:
        self._runtime = runtime
        self._run_id = run_id
        self._execution_id = execution_id
        self._started = False
        self._terminal = False
        self._bound_session_id: str | None = None
        self._persistence_error: Exception | None = None

        run = runtime.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise ValueError(f"ACP sink requires RUNNING run, got {run.status}")
        self._expected_seq = run.last_event_seq

    @property
    def persistence_error(self) -> Exception | None:
        """Set once an actual canonical append attempt (record_many) has
        failed. Never set for ordinary payload/provenance validation errors
        where no append was attempted. Once set, the sink's expected
        sequence is known stale/conflicted and must never be used for
        another append (see fail())."""
        return self._persistence_error

    def _require_started_and_open(self) -> None:
        if not self._started:
            raise RuntimeError("ACP sink cannot record before start")
        if self._terminal:
            raise RuntimeError("ACP sink cannot record after terminal settlement")

    def _spec(self, event_type: str, payload: dict[str, Any]) -> RunEventSpec:
        return RunEventSpec(
            type=event_type,
            payload=payload,
            execution_id=self._execution_id,
            correlation_id=self._execution_id,
            source=SOURCE,
        )

    def _append(self, spec: RunEventSpec) -> None:
        try:
            committed, _ = self._runtime.record_many(
                run_id=self._run_id,
                specs=(spec,),
                expected_last_event_seq=self._expected_seq,
            )
        except Exception as exc:
            self._persistence_error = exc
            raise
        self._expected_seq = committed[-1].seq

    def start(self, task: str) -> None:
        """Persist the single exact execution.started event."""
        if self._started:
            raise RuntimeError("ACP sink start was already recorded")
        if self._terminal:
            raise RuntimeError("ACP sink already has a terminal event")
        self._append(
            self._spec(
                RunEventType.EXECUTION_STARTED,
                {"transport": "acp", "task": task},
            )
        )
        self._started = True

    def _bind_session(self, session_id: object) -> None:
        validated = self._canonical_text(session_id, field="session_id")
        if self._bound_session_id is None:
            self._bound_session_id = validated
        elif validated != self._bound_session_id:
            raise ValueError("ACP event session_id does not match this sink")

    @staticmethod
    def _canonical_text(value: object, *, field: str, allow_empty: bool = False) -> str:
        """Bounded canonical-text validation for one ACP-native provenance
        fact. `title` is the only field where an absent ACP value (already
        normalized to "" by the 3J2A client) is valid; every other field
        (session_id, tool_call_id, option_id, outcome) must be non-empty."""
        if not isinstance(value, str):
            raise ValueError(f"ACP {field} must be a string")
        if not allow_empty and not value:
            raise ValueError(f"ACP {field} must be a non-empty string")
        if "\x00" in value:
            raise ValueError(f"ACP {field} must not contain NUL")
        if len(value) > MAX_CANONICAL_ACP_TEXT_CHARS:
            raise ValueError(
                f"ACP {field} exceeds {MAX_CANONICAL_ACP_TEXT_CHARS} characters"
            )
        return value

    @classmethod
    def _permission_options(cls, option_ids: object) -> list[str]:
        if isinstance(option_ids, (str, bytes)):
            raise ValueError("ACP permission option_ids must be a sequence of strings")
        try:
            options = list(option_ids)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("ACP permission option_ids must be a sequence of strings") from exc
        if len(options) > MAX_CANONICAL_ACP_PERMISSION_OPTIONS:
            raise ValueError(
                "ACP permission option_ids exceed "
                f"{MAX_CANONICAL_ACP_PERMISSION_OPTIONS} entries"
            )
        return [cls._canonical_text(option, field="option_id") for option in options]

    @staticmethod
    def _serialize_update(update: object) -> object:
        model_dump = getattr(update, "model_dump", None)
        if not callable(model_dump):
            raise TypeError("ACP session update must support Pydantic model_dump() serialization")
        serialized = model_dump(mode="json", by_alias=True, exclude_none=True)
        validate_json_value(serialized, path="update")
        return serialized

    def emit(self, event: object) -> None:
        """Map one transient ACP observation to one canonical RunEvent."""
        self._require_started_and_open()
        if isinstance(event, AcpSessionUpdateObserved):
            update = self._serialize_update(event.update)
            self._bind_session(event.session_id)
            self._append(
                self._spec(
                    RunEventType.EXECUTION_OUTPUT,
                    {
                        "transport": "acp",
                        "session_id": event.session_id,
                        "update": update,
                        "serialized_chars": event.serialized_chars,
                    },
                )
            )
            return

        if isinstance(event, AcpPermissionRequested):
            session_id = self._canonical_text(event.session_id, field="session_id")
            tool_call_id = self._canonical_text(event.tool_call_id, field="tool_call_id")
            title = self._canonical_text(event.title, field="title", allow_empty=True)
            option_ids = self._permission_options(event.option_ids)
            self._bind_session(session_id)
            self._append(
                self._spec(
                    RunEventType.PERMISSION_REQUESTED,
                    {
                        "transport": "acp",
                        "session_id": session_id,
                        "tool_call_id": tool_call_id,
                        "title": title,
                        "option_ids": option_ids,
                    },
                )
            )
            return

        if isinstance(event, AcpPermissionResolved):
            session_id = self._canonical_text(event.session_id, field="session_id")
            tool_call_id = self._canonical_text(event.tool_call_id, field="tool_call_id")
            outcome = self._canonical_text(event.outcome, field="outcome")
            self._bind_session(session_id)
            self._append(
                self._spec(
                    RunEventType.PERMISSION_RESOLVED,
                    {
                        "transport": "acp",
                        "session_id": session_id,
                        "tool_call_id": tool_call_id,
                        "outcome": outcome,
                    },
                )
            )
            return

        raise TypeError(f"Unsupported ACP event: {type(event).__name__}")

    def complete(self, result: AcpRunResult) -> None:
        """Persist the single execution.completed event and bind its session."""
        self._require_started_and_open()
        if not isinstance(result, AcpRunResult):
            raise TypeError("ACP sink completion requires AcpRunResult")
        self._bind_session(result.session_id)
        self._append(
            self._spec(
                RunEventType.EXECUTION_COMPLETED,
                {
                    "transport": "acp",
                    "session_id": result.session_id,
                    "stop_reason": result.stop_reason,
                    "update_count": result.update_count,
                    "update_chars": result.update_chars,
                    "permission_request_count": result.permission_request_count,
                    "session_close_supported": result.session_close_supported,
                    "session_close_succeeded": result.session_close_succeeded,
                },
            )
        )
        self._terminal = True

    def fail(
        self,
        error: Exception,
        *,
        error_type: str | None = None,
        message: str | None = None,
    ) -> None:
        """Persist the single execution.failed event without inventing a session.

        If a prior append already proved canonical persistence
        unavailable/conflicted (self._persistence_error is set), this never
        attempts another record_many call with the now-stale expected
        sequence -- it immediately re-raises the stored persistence error."""
        self._require_started_and_open()
        if self._persistence_error is not None:
            raise self._persistence_error
        self._append(
            self._spec(
                RunEventType.EXECUTION_FAILED,
                {
                    "transport": "acp",
                    "error_type": error_type or type(error).__name__,
                    "message": (message if message is not None else str(error))
                    .replace("\x00", "")[:MAX_CANONICAL_ACP_TEXT_CHARS],
                },
            )
        )
        self._terminal = True
