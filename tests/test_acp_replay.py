"""Replay tests driven by tests/fixtures/acp_replay/calc_average_fix.jsonl,
a sanitized reconstruction of a real end-to-end ACP session (see
tests/fixtures/acp_replay/README.md for provenance/format).

These exercise acp_runtime.permission_policy end to end through a real
official-SDK subprocess (tests/fixtures/acp_replay_agent.py) driven by a
multi-step transcript reconstructed from an actual run, rather than the
single-tool-call synthetic fixtures used elsewhere
(tests/fixtures/acp_permission_worker_agent.py): a real
session/request_permission round trip for an in-worktree edit (must be
allowed), one for a shell execute (must be rejected), and one for an edit
outside the worktree (must be rejected).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp_runtime.client import AcpClientRuntime  # noqa: E402
from acp_runtime.events import AcpPermissionRequested, AcpPermissionResolved  # noqa: E402
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest  # noqa: E402
from acp_runtime.permission_policy import WorktreeEditAcpPermissionPolicy  # noqa: E402
from executor_runtime.acp_worker import AcpWorkerAttemptAdapter, AcpWorkerLaunchProfile  # noqa: E402
from fix_runtime.models import FixTrigger, FixTriggerKind, FixWorkerRequest  # noqa: E402
from process_runtime.models import ProcessResult  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from verification_runtime.models import (  # noqa: E402
    VerificationCheckResult,
    VerificationReport,
    VerificationStatus,
)
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402
from acp_test_support import fixture_child_env  # noqa: E402

_REPLAY_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_replay_agent.py")
_TRANSCRIPT = str(Path(__file__).resolve().parent / "fixtures" / "acp_replay" / "calc_average_fix.jsonl")

_FIXED_CALC_PY = (
    "def average(numbers):\n"
    "    if not numbers:\n"
    "        return 0.0\n"
    "    return sum(numbers) / len(numbers)\n"
)


class _RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _launch(*, outside: str | None = None) -> AcpLaunchSpec:
    env = {"ACP_REPLAY_TRANSCRIPT": _TRANSCRIPT}
    if outside is not None:
        env["ACP_REPLAY_OUTSIDE"] = outside
    return AcpLaunchSpec(argv=(sys.executable, _REPLAY_AGENT), env=fixture_child_env(env))


def test_replay_allows_in_worktree_edit_and_rejects_execute_and_outside_path(tmp_path):
    """Drives AcpClientRuntime directly (no worker/pipeline plumbing) with
    the real WorktreeEditAcpPermissionPolicy, exactly as
    AcpWorkerAttemptAdapter wires it for the ACP Worker role."""
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir()
    sink = _RecordingSink()
    runtime = AcpClientRuntime()

    result = asyncio.run(
        runtime.run(
            _launch(outside=str(outside_dir / "secrets.txt")),
            AcpPromptRequest(cwd=str(tmp_path), prompt="fix the empty-list average bug"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
            event_sink=sink,
            permission_policy=WorktreeEditAcpPermissionPolicy(str(tmp_path)),
        )
    )

    # (c) terminal state.
    assert result.stop_reason == "end_turn"
    assert result.permission_request_count == 3

    requested = [e for e in sink.events if isinstance(e, AcpPermissionRequested)]
    resolved = [e for e in sink.events if isinstance(e, AcpPermissionResolved)]
    assert [r.tool_call_id for r in requested] == ["p1", "p2", "p3"]
    outcomes = {r.tool_call_id: r.outcome for r in resolved}

    # (a) in-worktree edit allowed; execute and outside-path edit rejected
    # (the policy selects the offered "reject_once" option rather than
    # cancelling outright whenever one was offered -- see
    # acp_runtime.permission_policy.WorktreeEditAcpPermissionPolicy).
    assert outcomes["p1"] == "selected:allow_once"
    assert outcomes["p2"] == "selected:reject_once"
    assert outcomes["p3"] == "selected:reject_once"

    # (b) the expected file change actually happened in the worktree...
    calc_py = tmp_path / "calc.py"
    assert calc_py.read_text(encoding="utf-8") == _FIXED_CALC_PY

    # ...and the rejected edit never touched anything outside the worktree.
    assert not (outside_dir / "secrets.txt").exists()


def test_old_reject_all_behavior_would_have_produced_no_changes(tmp_path):
    """Cheap reproduction of the real recorded run's actual outcome before
    this task's permission-policy fix: replay the exact same transcript
    with NO permission_policy supplied at all, i.e. AcpClientRuntime's
    original/default behavior (DenyAllAcpPermissionPolicy -- the same
    policy every non-Worker caller, e.g. Planner/Reviewer, still uses).
    Every request_permission call -- including the in-worktree edit --
    must resolve cancelled, and calc.py must never be written, matching
    the real run_6eb8cb41 outcome (no workspace change)."""
    sink = _RecordingSink()
    runtime = AcpClientRuntime()

    result = asyncio.run(
        runtime.run(
            _launch(),
            AcpPromptRequest(cwd=str(tmp_path), prompt="fix the empty-list average bug"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
            event_sink=sink,
            # No permission_policy: defaults to DenyAllAcpPermissionPolicy.
        )
    )

    assert result.stop_reason == "end_turn"
    resolved = [e for e in sink.events if isinstance(e, AcpPermissionResolved)]
    assert len(resolved) == 3
    assert all(r.outcome == "cancelled" for r in resolved)
    assert not (tmp_path / "calc.py").exists()


# ---------------- through the real AcpWorkerAttemptAdapter path ----------------


def _git_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (source / "calc.py").write_text(
        "def average(numbers):\n    return sum(numbers) / len(numbers)\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return source


def _runtime_and_run(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _worker_request():
    report = VerificationReport(
        verification_id="initial-verification",
        plan_id="plan-1",
        results=(
            VerificationCheckResult(
                "check-1", "Check",
                VerificationStatus.FAIL,
                ProcessResult(
                    argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=1,
                    stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                    stdout_bytes=0, stderr_bytes=0,
                ),
            ),
        ),
        duration_ms=1,
    )
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=report)
    return FixWorkerRequest(
        task="fix the empty-list average bug", trigger=trigger, attempt_index=1,
        rendered_input="fix the empty-list average bug",
    )


def test_worker_replay_allows_in_worktree_edit_end_to_end(tmp_path):
    """Same fixture, but through the real production path:
    AcpWorkerAttemptAdapter -> AcpClientRuntime -> real ACP subprocess,
    against a real GitWorktreeWorkspace (a shadow git worktree, exactly how
    the ACP Worker actually runs in production). AcpWorkerAttemptAdapter
    supplies WorktreeEditAcpPermissionPolicy(cwd) itself -- this test does
    not pass one explicitly."""
    source = _git_source(tmp_path)
    runtime, run = _runtime_and_run(tmp_path)
    workspace = GitWorktreeWorkspace.create(
        source_root=source, run_id="acp-replay", base_dir=tmp_path / "workspaces"
    )
    try:
        adapter = AcpWorkerAttemptAdapter(
            runtime, run.run_id,
            AcpWorkerLaunchProfile(
                command=sys.executable,
                args=(_REPLAY_AGENT,),
                env=fixture_child_env({"ACP_REPLAY_TRANSCRIPT": _TRANSCRIPT}),
            ),
            AcpClientRuntime(),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
        )
        result = adapter.run(workspace, _worker_request(), execution_id="execution-replay-1")

        assert result.execution_id == "execution-replay-1"
        # (b) the expected file change happened in the real shadow worktree...
        assert (workspace.root / "calc.py").read_text(encoding="utf-8") == _FIXED_CALC_PY
        # ...and never touched the source repo it was cloned from.
        assert (source / "calc.py").read_text(encoding="utf-8") != _FIXED_CALC_PY

        # (a) canonical permission events show the edit allowed, the shell
        # execute and the outside-path edit rejected.
        events = runtime.events(run.run_id, limit=100).events
        resolved = [e for e in events if e.type == RunEventType.PERMISSION_RESOLVED]
        assert len(resolved) == 3
        outcomes = {e.payload["tool_call_id"]: e.payload["outcome"] for e in resolved}
        assert outcomes["p1"] == "selected:allow_once"
        assert outcomes["p2"] == "selected:reject_once"
        assert outcomes["p3"] == "selected:reject_once"

        # (c) terminal state.
        assert events[-1].type == RunEventType.EXECUTION_COMPLETED
    finally:
        workspace.dispose()
