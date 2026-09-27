"""Shared ACP driving logic for the read-only semantic Planner/Reviewer roles.

pipeline_runtime.acp_planner.AcpPlanAttemptRunner and
executor_runtime.acp_reviewer.AcpReviewAttemptRunner both need three things
beyond what executor_runtime.acp_worker.AcpWorkerAttemptAdapter already
provides for the Worker role, so they are implemented once, here:

  1. Recovering the ACP agent's final assistant text. AcpRunResult
     deliberately does not carry it (acp_runtime is provider/transport-
     agnostic and never interprets message content) -- see
     acp_runtime.models.AcpRunResult. This module accumulates it from
     `agent_message_chunk` session updates as they stream, and drops every
     other update kind (agent_thought_chunk, tool_call updates, plan
     updates, ...): see _AgentMessageTextSink for why.

  2. Read-only enforcement, defense in depth. Layer 1 is
     acp_runtime.client._ImeceAcpClient.request_permission, which already
     auto-denies (as "cancelled") every permission request an ACP agent
     makes -- a well-behaved agent's tool-mediated write is already refused
     before it happens. Layer 2, added here, guards against an agent that
     writes to the workspace directly without ever asking permission (a
     buggy or malicious ACP agent, or a write path the adapter package
     never routes through request_permission at all): capture a workspace
     fingerprint immediately before and after the ACP session and raise
     AcpSemanticWorkspaceMutatedError if they differ.

  3. Running the ACP client synchronously from a HOW-adapter with the same
     "no running event loop" guard executor_runtime.acp_worker already
     enforces, translating acp_runtime failures uniformly for both callers.

Neither Planner nor Reviewer ACP session updates are ever recorded as
execution.* canonical events (that is exclusively the Worker's vocabulary,
via run_runtime.acp.CanonicalAcpEventSink) -- see the plan/review attempt
runner modules for how intermediate ACP progress is instead dropped rather
than mapped into plan.*/review.* canonical events.

F1 (live agent activity) addendum: `_AgentMessageTextSink` MAY additionally
be given an `activity_recorder` callable (see `run_acp_semantic_prompt`'s
`activity_recorder`/`role` parameters). When present, `tool_call` /
`tool_call_update` / `plan` session updates are mapped into
run_runtime.agent_activity.record_agent_activity(...) calls tagged with
`agent.activity` -- a NON-authoritative, advisory canonical event kept
entirely outside plan.*/review.*/execution.* vocabulary (see that module's
docstring for why this can never affect RunCompletionGate). Only a single
"Düşünüyor…" note is recorded per uninterrupted burst of
`agent_thought_chunk` updates (reset by the next tool_call/plan/message
update) -- individual thought text is still never persisted, matching the
existing agent_thought_chunk-drop behavior above.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any, Protocol

import acp

from agent_runtime.cancellation import OperationCancelledError
from acp_runtime.events import AcpEventSink, AcpPermissionRequested, AcpPermissionResolved, AcpSessionUpdateObserved
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest, AcpRunResult
from change_runtime.git import GitWorktreeChangeProvider
from run_runtime.activity_projection import acp_tool_title
from workspace.worktree import GitWorktreeWorkspace

# ACP ToolCallStart/ToolCallProgress.status -> agent.activity status; kept in
# sync with run_runtime.activity_projection._ACP_STATUS (not imported --
# that mapping is private to the projection module).
_ACP_ACTIVITY_STATUS = {
    "pending": "running",
    "in_progress": "running",
    "completed": "ok",
    "failed": "error",
}

ActivityRecorder = Any

ACP_SYSTEM_INSTRUCTIONS_HEADER = (
    "===== SYSTEM INSTRUCTIONS (highest priority; ACP has no separate "
    "system-prompt slot, so this section is injected ahead of the task -- "
    "it defines your role and constraints and is NOT part of the user task "
    "or repository data below it) =====\n"
)
ACP_SYSTEM_INSTRUCTIONS_FOOTER = "\n===== END SYSTEM INSTRUCTIONS =====\n\n"


class AcpSemanticError(Exception):
    """Base class for expected executor_runtime.acp_semantic failures."""


class AcpSemanticInputError(AcpSemanticError):
    """The caller supplied an invalid workspace/prompt/limits value."""


class AcpSemanticExecutionError(AcpSemanticError):
    """The underlying ACP session failed, or a read-only violation was
    detected during it."""


class AcpSemanticWorkspaceMutatedError(AcpSemanticExecutionError):
    """The workspace's change state differed before vs. after the ACP
    session, even though every permission request was already auto-denied.
    This is the defense-in-depth layer (see module docstring layer 2) --
    reaching this means an ACP agent wrote to the workspace without ever
    asking permission for it."""


def wrap_system_instructions_for_acp_prompt(system_instructions: str, rendered_task_input: str) -> str:
    """ACP has no separate system-prompt slot: prepend the role's stable
    system instructions ahead of the already-rendered, bounded task input,
    inside a clearly delimited section so the two can never be confused by
    the receiving agent."""
    if not isinstance(system_instructions, str) or not system_instructions.strip():
        raise AcpSemanticInputError("system_instructions must be a non-empty string.")
    if not isinstance(rendered_task_input, str) or not rendered_task_input:
        raise AcpSemanticInputError("rendered_task_input must be a non-empty string.")
    return (
        ACP_SYSTEM_INSTRUCTIONS_HEADER
        + system_instructions
        + ACP_SYSTEM_INSTRUCTIONS_FOOTER
        + rendered_task_input
    )


class _AgentMessageTextSink:
    """AcpEventSink that accumulates ONLY the agent's final assistant text.

    Every other observation the ACP session produces (agent_thought_chunk,
    tool_call updates, plan updates, permission request/resolved events, any
    other session_update kind) is intentionally dropped here, not persisted
    anywhere, and never mapped into a canonical event: agent_runtime's
    ModelCompleted/ToolStarted/etc. schema (turn_index/turn_id/item_id/
    call_id) has no natural, lossless counterpart in ACP's session_update
    notifications (which are keyed by session_id + content blocks, not by
    turn/item/call identity), and fabricating synthetic values for those
    fields risked corrupting canonical-schema semantics that other code
    (readmodels, projector) already depends on. Only the two lifecycle
    transitions CanonicalPlannerEventSink/CanonicalReviewEventSink actually
    require (started, terminal) are forwarded by the plan/review attempt
    runners that use this sink.
    """

    def __init__(self, *, role: str | None = None, activity_recorder: ActivityRecorder | None = None) -> None:
        self._chunks: list[str] = []
        self._role = role
        self._activity_recorder = activity_recorder
        self._thinking_note_open = False

    def _record_activity(self, **kwargs: Any) -> None:
        if self._activity_recorder is None or self._role is None:
            return
        try:
            self._activity_recorder(role=self._role, **kwargs)
        except Exception:
            # Best-effort only: a live-activity notice failure must never
            # interrupt/abort the underlying semantic ACP session.
            pass

    def emit(self, event: object) -> None:
        if isinstance(event, AcpSessionUpdateObserved):
            update = event.update
            if isinstance(update, acp.schema.AgentMessageChunk):
                content = getattr(update, "content", None)
                text = getattr(content, "text", None)
                if isinstance(text, str):
                    self._chunks.append(text)
                self._thinking_note_open = False
                return
            if isinstance(update, acp.schema.AgentThoughtChunk):
                if not self._thinking_note_open:
                    self._record_activity(kind="note", title="Düşünüyor…", status="info")
                    self._thinking_note_open = True
                return
            if isinstance(update, (acp.schema.ToolCallStart, acp.schema.ToolCallProgress)):
                self._thinking_note_open = False
                status = _ACP_ACTIVITY_STATUS.get(getattr(update, "status", None), "running")
                title = acp_tool_title(getattr(update, "kind", None), getattr(update, "title", None))
                tool_call_id = getattr(update, "tool_call_id", None)
                self._record_activity(
                    kind="tool", title=title, status=status,
                    tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                )
                return
            if isinstance(update, acp.schema.AgentPlanUpdate):
                self._thinking_note_open = False
                entries = getattr(update, "entries", None) or []
                summary = "; ".join(
                    f"{getattr(entry, 'status', '?')}: {getattr(entry, 'content', '')}" for entry in entries[:20]
                )
                self._record_activity(
                    kind="stage", title="Plan güncellendi", status="info",
                    detail=summary or None,
                )
                return
            return
        if isinstance(event, (AcpPermissionRequested, AcpPermissionResolved)):
            # Already auto-denied by the ACP client core (layer 1); nothing
            # further to do here.
            return

    @property
    def final_text(self) -> str:
        return "".join(self._chunks)


def _fallback_fingerprint(workspace) -> str:
    """Deterministic content hash over iter_files() for any Workspace that
    is not a GitWorktreeWorkspace (change_runtime.GitWorktreeChangeProvider
    only supports that one concrete type)."""
    hasher = hashlib.sha256()
    for relative_path in sorted(workspace.iter_files()):
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        full_path = workspace.root / relative_path
        try:
            data = full_path.read_bytes()
        except OSError:
            data = b""
        hasher.update(hashlib.sha256(data).digest())
        hasher.update(b"\n")
    return hasher.hexdigest()


def _workspace_fingerprint(workspace) -> str:
    if isinstance(workspace, GitWorktreeWorkspace):
        change_set = GitWorktreeChangeProvider().capture(workspace)
        return change_set.diff_sha256
    return _fallback_fingerprint(workspace)


class _AcpClientRunner(Protocol):
    async def run(
        self,
        launch: AcpLaunchSpec,
        request: AcpPromptRequest,
        *,
        limits: AcpClientLimits | None = None,
        event_sink: AcpEventSink | None = None,
    ) -> AcpRunResult: ...


def run_acp_semantic_prompt(
    *,
    acp_client: _AcpClientRunner,
    launch_spec: AcpLaunchSpec,
    workspace,
    prompt: str,
    limits: AcpClientLimits | None = None,
    cancel_token=None,
    role: str | None = None,
    activity_recorder: ActivityRecorder | None = None,
) -> tuple[str, AcpRunResult]:
    """Run one fresh, read-only ACP session and return (final_text, result).

    Raises AcpSemanticWorkspaceMutatedError if the workspace's change state
    differs before vs. after the session (see module docstring layer 2).
    Must not be called from inside a running event loop (mirrors
    executor_runtime.acp_worker.AcpWorkerAttemptAdapter.run's guard).

    `role`/`activity_recorder` are optional F1 (live agent activity) hooks:
    when both are given, tool_call/tool_call_update/plan session updates are
    mapped into `activity_recorder(role=role, kind=..., title=..., status=...,
    tool_call_id=..., detail=...)` calls -- see module docstring addendum.
    Omitting either keeps this function's behavior byte-identical to before
    F1 (no activity mapping, no extra canonical writes).
    """
    if limits is None:
        limits = AcpClientLimits()
    if not isinstance(limits, AcpClientLimits):
        raise AcpSemanticInputError("run_acp_semantic_prompt limits must be an AcpClientLimits.")

    cwd = str(workspace.root)
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise AcpSemanticInputError(f"ACP semantic session cwd must be an existing absolute directory: {cwd!r}.")

    try:
        prompt_request = AcpPromptRequest(cwd=cwd, prompt=prompt)
    except Exception as exc:  # noqa: BLE001 - AcpInputError et al. from acp_runtime.models
        raise AcpSemanticInputError(f"Invalid ACP semantic prompt/cwd: {exc}") from exc

    if len(prompt_request.prompt) > limits.max_prompt_chars:
        raise AcpSemanticInputError(
            "ACP semantic prompt exceeds max_prompt_chars "
            f"({len(prompt_request.prompt)} > {limits.max_prompt_chars})."
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AcpSemanticExecutionError("run_acp_semantic_prompt cannot execute inside a running event loop.")

    before_fingerprint = _workspace_fingerprint(workspace)
    sink = _AgentMessageTextSink(role=role, activity_recorder=activity_recorder)
    try:
        result = asyncio.run(
            acp_client.run(
                launch_spec, prompt_request, limits=limits, event_sink=sink, cancel_token=cancel_token,
            )
        )
    except OperationCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - any acp_runtime failure is infrastructure here
        raise AcpSemanticExecutionError(f"ACP semantic session failed: {exc}") from exc

    after_fingerprint = _workspace_fingerprint(workspace)
    if before_fingerprint != after_fingerprint:
        raise AcpSemanticWorkspaceMutatedError(
            "ACP agent mutated the workspace during a read-only Planner/Reviewer attempt; "
            "every permission request is already auto-denied, so this means a write "
            "occurred without ever requesting permission."
        )

    return sink.final_text, result
