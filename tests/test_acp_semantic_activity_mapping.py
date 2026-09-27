"""Aşama 3 F1 (decision 2): ACP Planner/Reviewer session updates
(tool_call/tool_call_update/plan) map into `agent.activity` canonical
events via executor_runtime.acp_semantic's activity_recorder hook, and
NEVER produce execution.*/plan.*/review.* canonical events for those two
roles (that invariant predates F1 -- see acp_semantic's module docstring).

Uses a lightweight in-process fake ACP client (no subprocess) that drives
run_acp_semantic_prompt's real event_sink through a scripted burst of
acp.schema session updates -- this exercises the exact integration surface
changed for F1 without the cost of a real ACP subprocess fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acp

from acp_runtime.events import AcpSessionUpdateObserved
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpRunResult
from executor_runtime.acp_semantic import run_acp_semantic_prompt
from run_runtime import RunEventType, RunRuntime, RunStore
from run_runtime.agent_activity import record_agent_activity
from workspace.worktree import GitWorktreeWorkspace


def _git_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (source / "known.txt").write_text("hello\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return source


def _workspace(tmp_path, run_id):
    return GitWorktreeWorkspace.create(
        source_root=_git_source(tmp_path), run_id=run_id, base_dir=tmp_path / "workspaces",
    )


def _running_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run.run_id


class _FakeAcpClient:
    """Emits a scripted burst of session updates through event_sink, then
    returns a stable AcpRunResult -- mirrors AcpClientRuntime.run's async
    signature without spawning a real subprocess."""

    def __init__(self, updates, *, final_text="OK", session_id="s1"):
        self._updates = list(updates)
        self._final_text = final_text
        self._session_id = session_id

    async def run(self, launch, request, *, limits=None, event_sink=None, cancel_token=None):
        for update in self._updates:
            event_sink.emit(
                AcpSessionUpdateObserved(session_id=self._session_id, update=update, serialized_chars=0)
            )
        event_sink.emit(
            AcpSessionUpdateObserved(
                session_id=self._session_id,
                update=acp.schema.AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=acp.schema.TextContentBlock(type="text", text=self._final_text),
                ),
                serialized_chars=0,
            )
        )
        return AcpRunResult(
            session_id=self._session_id, stop_reason="end_turn", update_count=len(self._updates) + 1,
            update_chars=0, permission_request_count=0, session_close_supported=True,
            session_close_succeeded=True,
        )


def _launch():
    return AcpLaunchSpec(argv=(sys.executable, "-c", "pass"))


def test_tool_call_and_plan_updates_map_to_agent_activity_not_execution(tmp_path):
    runtime, run_id = _running_runtime(tmp_path)
    workspace = _workspace(tmp_path, "acp-sem-1")
    try:
        recorded = []

        def activity_recorder(**kwargs):
            recorded.append(kwargs)
            record_agent_activity(runtime, run_id, execution_id="acp_planner_exec_p1", **kwargs)

        updates = [
            acp.schema.ToolCallStart(
                session_update="tool_call", tool_call_id="tc1", title="Read known.txt",
                kind="read", status="in_progress",
            ),
            acp.schema.ToolCallProgress(session_update="tool_call_update", tool_call_id="tc1", status="completed"),
            acp.schema.AgentPlanUpdate(session_update="plan", entries=[
                acp.schema.PlanEntry(content="Read the file", priority="medium", status="completed"),
            ]),
        ]
        client = _FakeAcpClient(updates, final_text='{"summary":"ok"}')
        final_text, _result = run_acp_semantic_prompt(
            acp_client=client, launch_spec=_launch(), workspace=workspace, prompt="do the thing",
            limits=AcpClientLimits(), role="planner", activity_recorder=activity_recorder,
        )
        assert final_text == '{"summary":"ok"}'

        assert [r["kind"] for r in recorded] == ["tool", "tool", "stage"]
        assert recorded[0]["status"] == "running"
        assert recorded[1]["status"] == "ok"
        assert recorded[2]["title"] == "Plan güncellendi"

        page = runtime.events(run_id, after_seq=0)
        types = [e.type for e in page.events]
        assert types.count(RunEventType.AGENT_ACTIVITY) == 3
        # The read-only Planner invariant (predates F1): never execution.*/
        # plan.* for intermediate ACP session updates.
        assert RunEventType.EXECUTION_STARTED not in types
        assert RunEventType.EXECUTION_COMPLETED not in types
        assert RunEventType.PLAN_STARTED not in types
        assert RunEventType.PLAN_COMPLETED not in types
    finally:
        workspace.dispose()


def test_thought_chunk_burst_collapses_to_a_single_thinking_note(tmp_path):
    workspace = _workspace(tmp_path, "acp-sem-2")
    try:
        recorded = []
        updates = [
            acp.schema.AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=acp.schema.TextContentBlock(type="text", text=text),
            )
            for text in ("hmm", "still thinking", "more")
        ]
        client = _FakeAcpClient(updates, final_text='{"verdict":"APPROVED","summary":"ok","findings":[]}')
        run_acp_semantic_prompt(
            acp_client=client, launch_spec=_launch(), workspace=workspace, prompt="review it",
            limits=AcpClientLimits(), role="reviewer", activity_recorder=lambda **kw: recorded.append(kw),
        )
        assert len(recorded) == 1
        assert recorded[0]["title"] == "Düşünüyor…"
        assert recorded[0]["kind"] == "note"
    finally:
        workspace.dispose()


def test_a_tool_call_after_a_thought_burst_reopens_the_next_thinking_note(tmp_path):
    workspace = _workspace(tmp_path, "acp-sem-3")
    try:
        recorded = []
        updates = [
            acp.schema.AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=acp.schema.TextContentBlock(type="text", text="hmm"),
            ),
            acp.schema.ToolCallStart(
                session_update="tool_call", tool_call_id="tc1", title="x", kind="read", status="completed",
            ),
            acp.schema.AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=acp.schema.TextContentBlock(type="text", text="again"),
            ),
        ]
        client = _FakeAcpClient(updates, final_text="fine")
        run_acp_semantic_prompt(
            acp_client=client, launch_spec=_launch(), workspace=workspace, prompt="task",
            limits=AcpClientLimits(), role="planner", activity_recorder=lambda **kw: recorded.append(kw),
        )
        assert [r["kind"] for r in recorded] == ["note", "tool", "note"]
    finally:
        workspace.dispose()


def test_activity_recorder_failure_never_fails_the_semantic_session(tmp_path):
    workspace = _workspace(tmp_path, "acp-sem-4")
    try:
        def boom(**kwargs):
            raise RuntimeError("recorder exploded")

        updates = [
            acp.schema.ToolCallStart(
                session_update="tool_call", tool_call_id="tc1", title="x", kind="read", status="completed",
            ),
        ]
        client = _FakeAcpClient(updates, final_text="fine")
        final_text, _ = run_acp_semantic_prompt(
            acp_client=client, launch_spec=_launch(), workspace=workspace, prompt="task",
            limits=AcpClientLimits(), role="planner", activity_recorder=boom,
        )
        assert final_text == "fine"
    finally:
        workspace.dispose()


def test_omitting_role_and_recorder_keeps_prior_behavior_byte_identical(tmp_path):
    """No role/activity_recorder given -> _AgentMessageTextSink behaves
    exactly as it did before F1 (only final_text accumulation)."""
    workspace = _workspace(tmp_path, "acp-sem-5")
    try:
        updates = [
            acp.schema.ToolCallStart(
                session_update="tool_call", tool_call_id="tc1", title="x", kind="read", status="completed",
            ),
        ]
        client = _FakeAcpClient(updates, final_text="unchanged")
        final_text, _ = run_acp_semantic_prompt(
            acp_client=client, launch_spec=_launch(), workspace=workspace, prompt="task",
            limits=AcpClientLimits(),
        )
        assert final_text == "unchanged"
    finally:
        workspace.dispose()
