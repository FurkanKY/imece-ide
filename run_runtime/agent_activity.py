"""run_runtime.agent_activity -- canonical recorder for 'live activity' notices.

`agent.activity` (run_runtime.events.RunEventType.AGENT_ACTIVITY) is a
NON-authoritative, purely advisory canonical event: it exists only to power
the F1 live-activity UI feed (see run_runtime.activity_projection). It is
deliberately kept OUT of every completion-gate/projector/readmodel
computation -- run_runtime.completion.RunCompletionGate,
run_runtime.projector.project_run and run_runtime.readmodels all dispatch on
an explicit, closed set of known event types and silently ignore anything
else (see projector.project_run's docstring: "Bilinmeyen bir event türü
projeksiyonu OLDUĞU GİBİ bırakır"). agent.activity is intentionally never
added to any of those dispatch tables, so recording/dropping/reshaping it
can never change a Run's settlement, phase or read-model output.

Because it is advisory, this module does NOT track an optimistic
expected_last_event_seq cursor the way run_runtime.native_agent /
run_runtime.acp do for their authoritative trajectories: it calls
RunRuntime.record() with no expected_last_event_seq, so a rare interleaving
with another appender never raises a conflict here -- worst case, an
activity notice lands at a slightly different seq than it would have under
strict CAS, which is immaterial for a UI-only feed.
"""

from __future__ import annotations

from typing import Any

from run_runtime.errors import EventValidationError
from run_runtime.events import RunEvent, RunEventType
from run_runtime.service import RunRuntime

ROLES = frozenset({"planner", "worker", "verification", "reviewer", "fix", "system"})
KINDS = frozenset({"tool", "model", "check", "stage", "note", "usage"})
STATUSES = frozenset({"running", "ok", "error", "info"})

MAX_TITLE_CHARS = 300
MAX_TOOL_CALL_ID_CHARS = 300
MAX_DETAIL_CHARS = 4096  # bounds verification output tails too (F1 decision 1).


def _bounded_text(value: Any, *, field: str, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"agent.activity {field} must be a string")
    if not allow_empty and not value:
        raise EventValidationError(f"agent.activity {field} must be a non-empty string")
    return value.replace("\x00", "")[:max_chars]


def record_agent_activity(
    runtime: RunRuntime,
    run_id: str,
    *,
    role: str,
    kind: str,
    title: str,
    status: str,
    tool_call_id: str | None = None,
    detail: str | None = None,
    execution_id: str | None = None,
    source: str = "agent_activity",
) -> RunEvent:
    """Append one agent.activity RunEvent and return it.

    Raises EventValidationError for an out-of-vocabulary role/kind/status;
    every text field is bounded (see module constants) rather than rejected
    for being long, since this is a best-effort UI notice, not authoritative
    evidence.
    """
    if role not in ROLES:
        raise EventValidationError(f"agent.activity role bilinmeyen: {role!r} (beklenen: {sorted(ROLES)})")
    if kind not in KINDS:
        raise EventValidationError(f"agent.activity kind bilinmeyen: {kind!r} (beklenen: {sorted(KINDS)})")
    if status not in STATUSES:
        raise EventValidationError(f"agent.activity status bilinmeyen: {status!r} (beklenen: {sorted(STATUSES)})")

    payload: dict[str, Any] = {
        "role": role,
        "kind": kind,
        "title": _bounded_text(title, field="title", max_chars=MAX_TITLE_CHARS),
        "status": status,
    }
    if tool_call_id is not None:
        payload["tool_call_id"] = _bounded_text(
            tool_call_id, field="tool_call_id", max_chars=MAX_TOOL_CALL_ID_CHARS
        )
    if detail is not None:
        payload["detail"] = _bounded_text(detail, field="detail", max_chars=MAX_DETAIL_CHARS, allow_empty=True)

    event, _run = runtime.record(
        run_id=run_id,
        type=RunEventType.AGENT_ACTIVITY,
        payload=payload,
        execution_id=execution_id,
        source=source,
    )
    return event
