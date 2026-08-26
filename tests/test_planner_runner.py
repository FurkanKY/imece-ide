import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from context_runtime import ContextEngine  # noqa: E402
from planner_runtime.errors import PlannerExecutionError, PlannerInputError, PlannerProtocolError  # noqa: E402
from planner_runtime.models import MAX_TASK_CHARS, TaskComplexity, TaskScope  # noqa: E402
from planner_runtime.recording import NullPlanRecorder  # noqa: E402
from planner_runtime.runner import PlannerRunner, _planner_policy, _planner_registry  # noqa: E402
from tool_runtime.models import PermissionEffect, PermissionRequest  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


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
        self.opened_with = None

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        self.opened_with = {
            "instructions": instructions,
            "tools": tools,
            "allow_parallel_tool_calls": allow_parallel_tool_calls,
        }
        return self.session


def _plan_json(summary="A plan.", complexity="LOW", scope="LOCAL"):
    return (
        f'{{"summary":"{summary}","steps":[{{"title":"Step 1","objective":"Do it"}}],'
        f'"acceptance_criteria":["tests pass"],"risks":[],'
        f'"task_profile":{{"complexity":"{complexity}","scope":"{scope}"}}}}'
    )


# ---------------- direct plans ----------------


def test_valid_json_produces_plan_report(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Add a feature")
    assert report.summary == "A plan."
    assert len(report.steps) == 1
    assert report.task_profile.complexity is TaskComplexity.LOW
    assert report.task_profile.scope is TaskScope.LOCAL


def test_plan_id_supplied_by_caller_preserved(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Add a feature", plan_id="plan_custom-1")
    assert report.plan_id == "plan_custom-1"


def test_plan_id_generated_when_omitted(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Add a feature")
    assert report.plan_id.startswith("plan_")


def test_task_sha256_exact(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    task = "Add a rate limiter"
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), task)
    assert report.task_sha256 == hashlib.sha256(task.encode("utf-8")).hexdigest()


def test_repository_fingerprint_from_context_pack_not_model(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    engine = ContextEngine()
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    workspace = LocalWorkspace(tmp_path)
    task = "review a.py"
    from context_runtime import ContextBudget
    from context_runtime.ranking import MAX_QUERY_CHARS

    expected_pack = engine.build(workspace, task[:MAX_QUERY_CHARS], ContextBudget(24_000, 6_000, 6_000))
    report = PlannerRunner(backend, context_engine=engine).run(workspace, task)
    assert report.repository_fingerprint == expected_pack.repository_fingerprint


def test_task_profile_exact(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(complexity="HIGH", scope="REPOSITORY_WIDE"), (), ModelStopReason.COMPLETED, ModelUsage())])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Refactor everything")
    assert report.task_profile.complexity is TaskComplexity.HIGH
    assert report.task_profile.scope is TaskScope.REPOSITORY_WIDE


# ---------------- tool-assisted planning ----------------


def test_search_code_tool_assisted_plan(tmp_path):
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("s1", "search_code", {"query": "add"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Plan work on the add function")
    assert report.summary == "A plan."
    tool_result = backend.session.inputs[1][0].result
    assert tool_result.is_error is False


def test_read_file_tool_assisted_plan(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    backend = ScriptedBackend([
        ModelTurn("", (ModelToolCall("r1", "read_file", {"path": "a.py"}),), ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage()),
    ])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Plan work on a.py")
    assert report.summary == "A plan."
    tool_result = backend.session.inputs[1][0].result
    assert "value = 1" in tool_result.content


# ---------------- read-only tool surface / fail-closed policy ----------------


def test_planner_tool_definitions_are_exactly_the_read_only_five(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t")
    names = {tool.name for tool in backend.opened_with["tools"]}
    assert names == {"read_file", "list_files", "search_text", "repo_map", "search_code"}
    assert "write_file" not in names
    assert "delete_path" not in names
    assert "run_process" not in names


def test_planner_registry_never_includes_mutating_tools():
    registry = _planner_registry(ContextEngine())
    names = {spec.name for spec in registry.list_specs()}
    assert names == {"read_file", "list_files", "search_text", "repo_map", "search_code"}


def test_planner_policy_denies_unexpected_permission_by_default():
    policy = _planner_policy()
    decision = policy.evaluate([PermissionRequest("edit", "x.py")])
    assert decision.effect is PermissionEffect.DENY


def test_planner_policy_allows_read_list_search():
    policy = _planner_policy()
    for action in ("read", "list", "search"):
        decision = policy.evaluate([PermissionRequest(action, "anything")])
        assert decision.effect is PermissionEffect.ALLOW


def test_planner_policy_never_asks():
    policy = _planner_policy()
    assert policy.default_effect is PermissionEffect.DENY


# ---------------- malformed / protocol failures ----------------


@pytest.mark.parametrize(
    "raw",
    [
        "SUMMARY: do the thing",
        '```json\n{"summary":"a","steps":[{"title":"t","objective":"o"}],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"LOW","scope":"LOCAL"}}\n```',
        '{"summary":"a","steps":[{"title":"t","objective":"o"}],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"LOW","scope":"LOCAL"}} trailing text',
        '{"summary":"a","summary":"b","steps":[{"title":"t","objective":"o"}],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"LOW","scope":"LOCAL"}}',
        '{"summary":"a","steps":[],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"LOW","scope":"LOCAL"}}',
        '{"summary":"a","steps":[{"title":"t","objective":"o"}],"acceptance_criteria":[],"risks":[],"task_profile":{"complexity":"EXTREME","scope":"LOCAL"}}',
    ],
)
def test_malformed_final_output_is_typed_protocol_failure(tmp_path, raw):
    backend = ScriptedBackend([ModelTurn(raw, (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t")


def test_backend_failure_is_planner_execution_error(tmp_path):
    backend = ScriptedBackend([RuntimeError("provider unavailable")])
    with pytest.raises(PlannerExecutionError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t")


def test_refusal_is_planner_execution_error(tmp_path):
    backend = ScriptedBackend([ModelTurn("I refuse.", (), ModelStopReason.REFUSAL, ModelUsage())])
    with pytest.raises(PlannerExecutionError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t")


# ---------------- input validation happens before any Agent side effect ----------------


def test_invalid_task_fails_before_model_side_effect(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerInputError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "   ")
    assert backend.opened_with is None


def test_oversized_task_fails_before_model_side_effect(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerInputError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t" * (MAX_TASK_CHARS + 1))
    assert backend.opened_with is None


def test_malformed_plan_id_fails_before_model_side_effect(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerInputError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", plan_id="../evil")
    assert backend.opened_with is None


# ---------------- fresh transient execution / no retries / no second harness ----------------


def test_planner_uses_fresh_transient_planner_execution_id(tmp_path):
    events = []

    class SpyRecorder(NullPlanRecorder):
        def emit(self, event):
            events.append(event)

    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", plan_id="plan_fixed", recorder=SpyRecorder())
    # AgentSession is the only harness invoked; its execution_id is derived
    # deterministically from plan_id, never a Worker/Reviewer/FixLoop id.
    execution_ids = {event.execution_id for event in events}
    assert execution_ids == {"planner_exec_plan_fixed"}


def test_no_retry_on_protocol_failure(tmp_path):
    backend = ScriptedBackend([ModelTurn("not json", (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t")
    assert backend.session.turns == []  # no second attempt was made


# ---------------- trust-boundary rendering reaches AgentSession verbatim ----------------


def test_prompt_injection_content_is_rendered_as_untrusted_data(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "Plan the rate limiter feature")
    rendered_input = backend.session.inputs[0][0].text
    assert "UNTRUSTED DATA" in rendered_input
    assert backend.opened_with["instructions"] == __import__(
        "planner_runtime.prompt", fromlist=["PLANNER_SYSTEM_INSTRUCTIONS"]
    ).PLANNER_SYSTEM_INSTRUCTIONS


# ---------------- recorder wiring ----------------


def test_null_recorder_is_used_by_default(tmp_path):
    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    report = PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", recorder=None)
    assert report.summary == "A plan."


def test_recorder_receives_emit_calls(tmp_path):
    events = []

    class SpyRecorder(NullPlanRecorder):
        def emit(self, event):
            events.append(event)

    backend = ScriptedBackend([ModelTurn(_plan_json(), (), ModelStopReason.COMPLETED, ModelUsage())])
    PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", recorder=SpyRecorder())
    assert events, "recorder.emit must be called for lifecycle events"


def test_recorder_fail_called_on_parser_failure_and_complete_not_called(tmp_path):
    calls = {"complete": 0, "fail": 0}

    class SpyRecorder(NullPlanRecorder):
        def complete(self, report):
            calls["complete"] += 1

        def fail(self, plan_id, error_type, message):
            calls["fail"] += 1

    backend = ScriptedBackend([ModelTurn("not json", (), ModelStopReason.COMPLETED, ModelUsage())])
    with pytest.raises(PlannerProtocolError):
        PlannerRunner(backend).run(LocalWorkspace(tmp_path), "t", recorder=SpyRecorder())
    assert calls == {"complete": 0, "fail": 1}
