"""Integration proof: PipelineRunner composes the REAL Planner/Worker/
Verification/Reviewer adapters (and the real, unmodified FixLoopRunner) end
to end on a real GitWorktreeWorkspace, using a ScriptedBackend — mirroring
tests/test_native_attempt_adapters_integration.py's pattern."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage, UserInput  # noqa: E402
from change_runtime import GitWorktreeChangeProvider  # noqa: E402
from executor_runtime.native_reviewer import NativeReviewAttemptAdapter  # noqa: E402
from executor_runtime.native_verification import NativeVerificationAttemptAdapter  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from pipeline_runtime.models import PipelineStatus  # noqa: E402
from pipeline_runtime.native_planner import NativePlanAttemptRunner  # noqa: E402
from pipeline_runtime.runner import PipelineRunner  # noqa: E402
from process_runtime.models import ProcessResult  # noqa: E402
from review_runtime.runner import ReviewerRunner  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.received_inputs = []

    def respond(self, input_items):
        self.received_inputs.append(input_items)
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session

    @property
    def first_user_input_text(self) -> str:
        """The text of the very first UserInput this backend's session
        received (i.e. the rendered prompt for its one attempt)."""
        first_call_items = self.session.received_inputs[0]
        first_user_input = next(item for item in first_call_items if isinstance(item, UserInput))
        return first_user_input.text


class FakeProcessRunner:
    def __init__(self, results):
        self._results = list(results)

    def run(self, workspace, request, *, cancel_token=None):
        return self._results.pop(0)


def _completed_turn(text):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _process_result(exit_code=0):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _plan_json():
    return (
        '{"summary":"Fix the bug in a.txt.","steps":[{"title":"Step 1","objective":"Fix it."}],'
        '"acceptance_criteria":["a.txt says fixed"],"risks":[],'
        '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
    )


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


@pytest.fixture
def repo_workspace(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()

    def _git(args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.name", "T"])
    _git(["config", "user.email", "t@example.com"])
    (source / "a.txt").write_text("buggy\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="pipeline-integration", base_dir=tmp_path / "workspaces")
    yield ws
    ws.dispose()


def test_pipeline_end_to_end_approved_first_try(tmp_path, repo_workspace):
    runtime, run = setup_runtime(tmp_path)

    planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
    planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    stages = []
    report = pipeline.run(run.run_id, repo_workspace, "Fix the bug in a.txt", on_stage=lambda s, i: stages.append(s))

    # The user still has the final Apply/Reject word: a Reviewer-APPROVED,
    # Verification-PASSed Run is left WAITING_USER, never SUCCEEDED.
    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "reviewed"
    assert (repo_workspace.root / "a.txt").read_text(encoding="utf-8") == "fixed\n"
    assert stages == ["planning", "working", "verifying", "reviewing", "done"]

    events = runtime.events(run.run_id, limit=500).events
    types = [e.type for e in events]

    assert RunEventType.PLAN_STARTED in types
    assert RunEventType.PLAN_COMPLETED in types
    assert RunEventType.EXECUTION_STARTED in types
    assert RunEventType.EXECUTION_COMPLETED in types
    assert RunEventType.VERIFICATION_STARTED in types
    assert RunEventType.VERIFICATION_COMPLETED in types
    assert RunEventType.REVIEW_STARTED in types
    assert RunEventType.REVIEW_COMPLETED in types
    assert RunEventType.PROPOSAL_READY in types
    assert RunEventType.RUN_WAITING_USER in types
    assert RunEventType.RUN_COMPLETED not in types
    assert runtime.get_run(run.run_id).status.value == "waiting_user"

    # Coarse-grained terminal-shape ordering (the real adapters interleave
    # finer-grained turn.*/model.*/tool.*/usage.recorded events between each
    # of these — see the full `types` dump this test prints on failure).
    coarse = [t for t in types if t in {
        RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED,
        RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED,
        RunEventType.VERIFICATION_STARTED, RunEventType.VERIFICATION_COMPLETED,
        RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED,
        RunEventType.PROPOSAL_READY, RunEventType.RUN_WAITING_USER,
    }]
    assert coarse == [
        RunEventType.RUN_STARTED,
        RunEventType.PLAN_STARTED,
        RunEventType.PLAN_COMPLETED,
        RunEventType.EXECUTION_STARTED,
        RunEventType.EXECUTION_COMPLETED,
        RunEventType.VERIFICATION_STARTED,
        RunEventType.VERIFICATION_COMPLETED,
        RunEventType.REVIEW_STARTED,
        RunEventType.REVIEW_COMPLETED,
        RunEventType.PROPOSAL_READY,
        RunEventType.RUN_WAITING_USER,
    ], f"full event sequence was: {types}"

    proposal = events[-2]
    assert proposal.source == "run_gate"
    assert proposal.payload["reason"] == "reviewed"
    assert proposal.payload["review_verdict"] == "APPROVED"

    # Planner activity must never be mistaken for execution activity.
    planner_events = [e for e in events if e.source == "planner"]
    assert planner_events
    assert all(e.execution_id is None for e in planner_events)


def test_pipeline_end_to_end_verification_fail_then_fix_loop(tmp_path, repo_workspace):
    runtime, run = setup_runtime(tmp_path)

    planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
    planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

    worker_backend = ScriptedBackend([
        # Initial attempt: writes something wrong.
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "still buggy\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Made a first attempt."),
        # Fix attempt: writes the real fix.
        ModelTurn(
            "", (ModelToolCall("c2", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id,
        process_runner=FakeProcessRunner([_process_result(1), _process_result(0)]),
    )

    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider, max_fix_attempts=1,
    )

    report = pipeline.run(run.run_id, repo_workspace, "Fix the bug in a.txt")

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.fix_loop_report is not None
    assert (repo_workspace.root / "a.txt").read_text(encoding="utf-8") == "fixed\n"

    events = runtime.events(run.run_id, limit=500).events
    types = [e.type for e in events]
    assert RunEventType.FIX_LOOP_STARTED in types
    assert RunEventType.FIX_LOOP_COMPLETED in types
    assert RunEventType.PROPOSAL_READY in types
    assert RunEventType.RUN_WAITING_USER in types
    assert RunEventType.RUN_COMPLETED not in types
    assert runtime.get_run(run.run_id).status.value == "waiting_user"


def test_pipeline_no_verification_plan_leaves_run_waiting_user(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()

    def _git(args):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    _git(["init", "-q"])
    _git(["config", "user.name", "T"])
    _git(["config", "user.email", "t@example.com"])
    (source / "a.txt").write_text("buggy\n", encoding="utf-8")
    _git(["add", "-A"])
    _git(["commit", "-q", "-m", "init"])

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="pipeline-no-verify", base_dir=tmp_path / "workspaces")
    try:
        runtime, run = setup_runtime(tmp_path)

        planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
        planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

        worker_backend = ScriptedBackend([
            ModelTurn(
                "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
                ModelStopReason.TOOL_USE, ModelUsage(),
            ),
            _completed_turn("Fixed the bug."),
        ])
        worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

        verification = NativeVerificationAttemptAdapter(runtime, run.run_id, process_runner=FakeProcessRunner([]))

        review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Looks fine.","findings":[]}')])
        reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

        change_provider = GitWorktreeChangeProvider()

        pipeline = PipelineRunner(
            runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
            change_provider=change_provider,
        )

        report = pipeline.run(run.run_id, ws, "Fix the bug in a.txt")

        assert report.status is PipelineStatus.NEEDS_USER
        events = runtime.events(run.run_id, limit=500).events
        types = [e.type for e in events]
        assert RunEventType.VERIFICATION_STARTED not in types
        assert RunEventType.REVIEW_STARTED in types
        assert RunEventType.PROPOSAL_READY in types
        assert RunEventType.RUN_WAITING_USER in types
        assert runtime.get_run(run.run_id).status.value == "waiting_user"
    finally:
        ws.dispose()


def test_pipeline_project_rules_reach_planner_worker_and_reviewer(tmp_path, repo_workspace):
    """An AGENTS.md committed in the repo must be discovered from the
    isolated GitWorktreeWorkspace root and reach the Planner, the initial
    Worker attempt, and the Reviewer -- proving the ScriptedBackend actually
    received input containing the project rules text for all three roles."""
    (repo_workspace.root / "AGENTS.md").write_text(
        "PROJECT-SPECIFIC-RULE-MARKER: always call helper() for logging.",
        encoding="utf-8",
    )

    runtime, run = setup_runtime(tmp_path)

    planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
    planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    report = pipeline.run(run.run_id, repo_workspace, "Fix the bug in a.txt")
    assert report.status is PipelineStatus.NEEDS_USER

    marker = "PROJECT-SPECIFIC-RULE-MARKER: always call helper() for logging."
    for backend, role in ((planner_backend, "planner"), (worker_backend, "worker"), (review_backend, "reviewer")):
        text = backend.first_user_input_text
        assert marker in text, f"{role} did not receive the project rules"
        assert "untrusted" in text.lower(), f"{role}'s rules section was not labelled untrusted"


def test_pipeline_pinned_paths_reach_planner_reviewer_content_and_worker_path_list(tmp_path, repo_workspace):
    """F6 (@-mentions): a pinned file's CONTENT reaches the Planner and
    Reviewer (via ContextEngine, at highest priority), while the Worker only
    gets the pinned PATH LIST (it has full repository tools and reads pinned
    files itself -- see fix_runtime.prompt's module docstring)."""
    runtime, run = setup_runtime(tmp_path)

    pinned_marker = "PINNED-FILE-SENTINEL-CONTENT-6f2a"
    (repo_workspace.root / "pinned.txt").write_text(f"{pinned_marker}\n", encoding="utf-8")

    planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
    planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    report = pipeline.run(
        run.run_id, repo_workspace, "Fix the bug in a.txt", pinned_paths=("pinned.txt",),
    )
    assert report.status is PipelineStatus.NEEDS_USER

    # Planner and Reviewer went through ContextEngine.build(pinned_paths=...):
    # the pinned file's actual CONTENT is inlined, tagged as user-referenced.
    for backend, role in ((planner_backend, "planner"), (review_backend, "reviewer")):
        text = backend.first_user_input_text
        assert pinned_marker in text, f"{role} did not receive the pinned file's content"
        assert "[user-referenced file]" in text, f"{role}'s pinned segment was not tagged"

    # Worker gets only the PATH, never the pinned file's content inlined --
    # it has its own repository tools to read it (see render_initial_worker_input).
    worker_text = worker_backend.first_user_input_text
    assert "USER-REFERENCED FILES" in worker_text
    assert "pinned.txt" in worker_text
    assert pinned_marker not in worker_text

    # Canonical provenance: plan.started carries pinned_paths (decision 5),
    # the same additive-field style as rules_sha256.
    events = runtime.events(run.run_id, limit=500).events
    plan_started = next(e for e in events if e.type == RunEventType.PLAN_STARTED)
    assert plan_started.payload.get("pinned_paths") == ["pinned.txt"]


def test_pipeline_without_pinned_paths_is_unaffected(tmp_path, repo_workspace):
    """Backward compatibility: omitting pinned_paths reproduces the exact
    prior behavior (no USER-REFERENCED FILES noise, no pinned_paths field)."""
    runtime, run = setup_runtime(tmp_path)

    planner_backend = ScriptedBackend([_completed_turn(_plan_json())])
    planner = NativePlanAttemptRunner(runtime, run.run_id, planner_backend)

    worker_backend = ScriptedBackend([
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("Fixed the bug."),
    ])
    worker = NativeWorkerAttemptAdapter(runtime, run.run_id, worker_backend)

    verification = NativeVerificationAttemptAdapter(
        runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(0)]),
    )

    review_backend = ScriptedBackend([_completed_turn('{"verdict":"APPROVED","summary":"Good fix.","findings":[]}')])
    reviewer = NativeReviewAttemptAdapter(runtime, run.run_id, ReviewerRunner(review_backend))

    change_provider = GitWorktreeChangeProvider()

    pipeline = PipelineRunner(
        runtime, planner=planner, worker=worker, verification=verification, reviewer=reviewer,
        change_provider=change_provider,
    )

    report = pipeline.run(run.run_id, repo_workspace, "Fix the bug in a.txt")
    assert report.status is PipelineStatus.NEEDS_USER

    assert worker_backend.first_user_input_text.count("USER-REFERENCED FILES") == 1
    assert "(none)" in worker_backend.first_user_input_text

    events = runtime.events(run.run_id, limit=500).events
    plan_started = next(e for e in events if e.type == RunEventType.PLAN_STARTED)
    assert "pinned_paths" not in plan_started.payload
