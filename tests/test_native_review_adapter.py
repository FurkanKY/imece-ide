"""NativeReviewAttemptAdapter — binds fix_runtime.ports.ReviewAttemptRunner
to a caller-configured ReviewerRunner."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelTurn, ModelUsage  # noqa: E402
from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError  # noqa: E402
from executor_runtime.native_reviewer import NativeReviewAttemptAdapter  # noqa: E402
from review_runtime.models import ReviewRequest  # noqa: E402
from review_runtime.runner import ReviewerRunner  # noqa: E402
from run_runtime import RunEventType, RunRuntime, RunStore  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


def setup_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def respond(self, input_items):
        self.inputs.append(input_items)
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


def _review_json(verdict="APPROVED", summary="Looks good."):
    if verdict == "APPROVED":
        return f'{{"verdict":"APPROVED","summary":"{summary}","findings":[]}}'
    return (
        f'{{"verdict":"NEEDS_FIX","summary":"{summary}","findings":['
        '{"severity":"major","message":"bug"}]}'
    )


def _completed_turn(text):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _valid_request():
    return ReviewRequest(task="fix the bug", diff="diff --git a/x b/x\n")


# ---------------- constructor / validation ----------------


def test_run_id_property_exposes_constructor_value(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json())]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    assert adapter.run_id == run.run_id


def test_non_reviewer_runner_rejected():
    class NotAReviewer:
        pass

    with pytest.raises(ExecutorAdapterInputError):
        NativeReviewAttemptAdapter(object(), "run-1", NotAReviewer())


def test_non_review_request_rejected_before_reviewer_call(tmp_path):
    runtime, run = setup_runtime(tmp_path)

    class ExplodingBackend:
        def open_session(self, **kwargs):
            raise AssertionError("ReviewerRunner must not be invoked for a rejected request")

    reviewer = ReviewerRunner(ExplodingBackend())
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, "not a request", review_id="rev-1")


# ---------------- exact forwarding + normal outcomes ----------------


def test_exact_review_id_workspace_and_request_forwarded_approved(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json("APPROVED"))]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)
    request = _valid_request()

    report = adapter.run(workspace, request, review_id="rev-exact")

    assert report.review_id == "rev-exact"
    assert report.verdict.value == "APPROVED"
    events = runtime.events(run.run_id, limit=200).events
    started = [e for e in events if e.type == RunEventType.REVIEW_STARTED]
    assert len(started) == 1
    assert started[0].payload["review_id"] == "rev-exact"


def test_needs_fix_returns_normally_not_an_exception(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json("NEEDS_FIX"))]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    report = adapter.run(workspace, _valid_request(), review_id="rev-needsfix")

    assert report.verdict.value == "NEEDS_FIX"


def test_canonical_review_recorder_used_as_sink(tmp_path):
    from run_runtime.reviewer import CanonicalReviewEventSink

    captured = {}
    original_init = CanonicalReviewEventSink.__init__

    def _capturing_init(self, runtime, run_id, *, review_id):
        captured["review_id"] = review_id
        return original_init(self, runtime, run_id, review_id=review_id)

    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json("APPROVED"))]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    CanonicalReviewEventSink.__init__ = _capturing_init
    try:
        adapter.run(workspace, _valid_request(), review_id="rev-sink")
    finally:
        CanonicalReviewEventSink.__init__ = original_init

    assert captured["review_id"] == "rev-sink"


# ---------------- fail-closed / infra failure wrapping ----------------


def test_report_review_id_mismatch_fails_closed(tmp_path, monkeypatch):
    import review_runtime.runner as rr_module

    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json("APPROVED"))]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    real_run = rr_module.ReviewerRunner.run

    def _mismatched_run(self, workspace, request, *, recorder=None, review_id=None):
        report = real_run(self, workspace, request, recorder=recorder, review_id=review_id)
        object.__setattr__(report, "review_id", "totally-different-id")
        return report

    monkeypatch.setattr(rr_module.ReviewerRunner, "run", _mismatched_run)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(workspace, _valid_request(), review_id="rev-real")


def test_reviewer_protocol_error_wrapped_with_cause(tmp_path):
    from review_runtime.errors import ReviewProtocolError

    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn("not json at all")]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    with pytest.raises(ExecutorAdapterExecutionError) as excinfo:
        adapter.run(workspace, _valid_request(), review_id="rev-malformed")

    assert isinstance(excinfo.value.__cause__, ReviewProtocolError)


def test_reviewer_backend_infra_error_wrapped_with_cause(tmp_path):
    from review_runtime.errors import ReviewExecutionError

    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([RuntimeError("provider unavailable")]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    with pytest.raises(ExecutorAdapterExecutionError) as excinfo:
        adapter.run(workspace, _valid_request(), review_id="rev-backendfail")

    assert isinstance(excinfo.value.__cause__, ReviewExecutionError)


# ---------------- no orchestration / canonical shape ----------------


def test_no_run_completion_gate_or_prompt_construction_in_module():
    import executor_runtime.native_reviewer as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "RunCompletionGate" not in source
    assert "render_initial_review_input" not in source
    assert "ContextEngine" not in source


def test_no_retry_reviewer_invoked_exactly_once(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    backend = ScriptedBackend([_completed_turn(_review_json("APPROVED"))])
    reviewer = ReviewerRunner(backend)
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    adapter.run(workspace, _valid_request(), review_id="rev-once")

    assert len(backend.session.inputs) == 1


def test_canonical_review_lifecycle_keeps_execution_id_none(tmp_path):
    runtime, run = setup_runtime(tmp_path)
    reviewer = ReviewerRunner(ScriptedBackend([_completed_turn(_review_json("APPROVED"))]))
    adapter = NativeReviewAttemptAdapter(runtime, run.run_id, reviewer)
    workspace = LocalWorkspace(tmp_path)

    adapter.run(workspace, _valid_request(), review_id="rev-execid")

    events = runtime.events(run.run_id, limit=200).events
    review_events = [e for e in events if e.type in (RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED)]
    assert review_events
    assert all(e.execution_id is None for e in review_events)
