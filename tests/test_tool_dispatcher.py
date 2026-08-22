import math
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_runtime import (
    ApprovalGrant,
    Dispatcher,
    PermissionEffect,
    PermissionRequest,
    PermissionRule,
    PreparedToolCall,
    PolicyEvaluator,
    ToolAnnotations,
    ToolCall,
    ToolExecutionContext,
    ToolObservation,
    ToolRegistry,
    ToolSpec,
)
from tool_runtime.errors import (
    ToolApprovalMismatchError,
    ToolApprovalRequiredError,
    ToolDeniedError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolNotFoundError,
    ToolObservationError,
    ToolPreparedCallError,
    ToolPolicyError,
    ToolCallConsumedError,
)
from workspace.base import Workspace


SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}


class FakeExecutor:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.arguments = None
        self.result = result
        self.error = error

    def execute(self, arguments, context):
        self.calls += 1
        self.arguments = arguments
        if self.error:
            raise self.error
        return self.result or ToolObservation("ok", {})


class BlockingExecutor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, arguments, context):
        self.calls += 1
        self.arguments = arguments
        self.entered.set()
        self.release.wait()
        return ToolObservation("ok", {})


class FakeWorkspace(Workspace):
    def __init__(self, label):
        self.label = label

    @property
    def root(self):
        return self.label

    def dispose(self):
        return None


def make(resolver=None, effect=PermissionEffect.ALLOW, executor=None, tool_name="fake"):
    registry = ToolRegistry()
    executor = executor or FakeExecutor()
    resolver = resolver or (lambda arguments, context: [PermissionRequest("shell", arguments["command"])])
    registry.register(ToolSpec(tool_name, "Fake tool", SCHEMA, ToolAnnotations(), resolver), executor)
    rules = [PermissionRule("shell", "git status", effect)]
    return Dispatcher(registry, PolicyEvaluator(rules)), executor


def call(arguments=None):
    return ToolCall("call-1", "fake", arguments if arguments is not None else {"command": "git status"})


WORKSPACE = FakeWorkspace("workspace-a")
CONTEXT = ToolExecutionContext(workspace=WORKSPACE, run_id="run-1", execution_id="exec-1")


def test_unknown_tool_typed_error():
    dispatcher, _ = make()
    with pytest.raises(ToolNotFoundError):
        dispatcher.prepare(ToolCall("1", "missing", {}), CONTEXT)


@pytest.mark.parametrize("bad_context", [None, object()])
def test_context_requires_workspace_instance(bad_context):
    with pytest.raises(ToolInputValidationError):
        ToolExecutionContext(workspace=bad_context)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["run_id", "execution_id"])
def test_context_identifiers_are_none_or_non_empty_strings(field):
    with pytest.raises(ToolInputValidationError):
        ToolExecutionContext(workspace=WORKSPACE, **{field: "   "})


@pytest.mark.parametrize("arguments", [[], "bad", {"command": float("nan")}, {"command": b"bad"}])
def test_malformed_or_non_strict_arguments_rejected(arguments):
    with pytest.raises(ToolInputValidationError):
        ToolCall("1", "fake", arguments)  # type: ignore[arg-type]


def test_schema_invalid_arguments_fail_before_resolver_or_executor():
    resolver_calls = 0

    def resolver(arguments, context):
        nonlocal resolver_calls
        resolver_calls += 1
        return [PermissionRequest("shell", "git status")]

    dispatcher, executor = make(resolver=resolver)
    with pytest.raises(ToolInputValidationError):
        dispatcher.prepare(call({"command": "git status", "extra": "no"}), CONTEXT)
    assert resolver_calls == 0 and executor.calls == 0


def test_zero_requests_and_resolver_failure_are_policy_errors():
    dispatcher, _ = make(resolver=lambda arguments, context: [])
    with pytest.raises(ToolPolicyError):
        dispatcher.prepare(call(), CONTEXT)
    dispatcher, _ = make(resolver=lambda arguments, context: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ToolPolicyError) as exc:
        dispatcher.prepare(call(), CONTEXT)
    assert isinstance(exc.value.__cause__, ValueError)


def test_allow_executes_exactly_once_and_executor_gets_fresh_arguments():
    dispatcher, executor = make()
    prepared = dispatcher.prepare(call(), CONTEXT)
    observation = dispatcher.execute(prepared, CONTEXT)
    assert observation.content == "ok"
    assert executor.calls == 1
    assert executor.arguments == {"command": "git status"}
    assert executor.arguments is not prepared
    with pytest.raises(ToolCallConsumedError):
        dispatcher.execute(prepared, CONTEXT)
    assert executor.calls == 1


def test_same_call_id_cannot_be_prepared_twice_and_original_survives():
    dispatcher, executor = make()
    original = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolPreparedCallError):
        dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolPreparedCallError):
        dispatcher.prepare(call({"command": "git push"}), CONTEXT)
    dispatcher.execute(original, CONTEXT)
    assert executor.calls == 1


def test_concurrent_prepare_same_call_id_has_one_winner():
    dispatcher, _ = make()
    start = threading.Barrier(2)
    successes = []
    failures = []

    def prepare_once():
        start.wait()
        try:
            successes.append(dispatcher.prepare(call(), CONTEXT))
        except ToolPreparedCallError as exc:
            failures.append(exc)

    threads = [threading.Thread(target=prepare_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(failures) == 1


def test_concurrent_execute_consumes_once_before_executor():
    executor = BlockingExecutor()
    dispatcher, executor = make(executor=executor)
    prepared = dispatcher.prepare(call(), CONTEXT)
    start = threading.Barrier(2)
    failures = []

    def execute_once():
        start.wait()
        try:
            dispatcher.execute(prepared, CONTEXT)
        except ToolCallConsumedError as exc:
            failures.append(exc)

    threads = [threading.Thread(target=execute_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert executor.entered.wait(timeout=1)
    executor.release.set()
    for thread in threads:
        thread.join(timeout=1)

    assert executor.calls == 1
    assert len(failures) == 1
    assert executor.arguments == {"command": "git status"}


def test_forged_prepared_call_cannot_execute():
    dispatcher, executor = make()
    original = dispatcher.prepare(call(), CONTEXT)
    forged = PreparedToolCall(
        call_id=original.call_id,
        tool_name=original.tool_name,
        arguments_json=original.arguments_json,
        permission_requests=original.permission_requests,
        policy_decision=original.policy_decision,
        approval_fingerprint=original.approval_fingerprint,
    )
    with pytest.raises(ToolPreparedCallError):
        dispatcher.execute(forged, CONTEXT)
    assert executor.calls == 0


def test_prepared_call_cannot_cross_dispatcher_instances():
    dispatcher_a, executor = make()
    dispatcher_b, _ = make()
    prepared = dispatcher_a.prepare(call(), CONTEXT)
    with pytest.raises(ToolPreparedCallError):
        dispatcher_b.execute(prepared, CONTEXT)
    assert executor.calls == 0


def test_originating_dispatcher_accepts_its_prepared_call():
    dispatcher, executor = make()
    prepared = dispatcher.prepare(call(), CONTEXT)
    dispatcher.execute(prepared, CONTEXT)
    assert executor.calls == 1


@pytest.mark.parametrize(
    "context",
    [
        ToolExecutionContext(workspace=FakeWorkspace("workspace-b"), run_id="run-1", execution_id="exec-1"),
        ToolExecutionContext(workspace=WORKSPACE, run_id="run-2", execution_id="exec-1"),
        ToolExecutionContext(workspace=WORKSPACE, run_id="run-1", execution_id="exec-2"),
    ],
    ids=["workspace", "run_id", "execution_id"],
)
def test_context_mismatch_rejects_before_executor(context):
    dispatcher, executor = make()
    prepared = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolPreparedCallError):
        dispatcher.execute(prepared, context)
    assert executor.calls == 0


def test_context_mismatch_cannot_be_overridden_by_grant():
    dispatcher, executor = make(effect=PermissionEffect.ASK)
    prepared = dispatcher.prepare(call(), CONTEXT)
    grant = ApprovalGrant(prepared.call_id, prepared.approval_fingerprint)
    with pytest.raises(ToolPreparedCallError):
        dispatcher.execute(prepared, ToolExecutionContext(WORKSPACE, "run-2", "exec-1"), grant)
    assert executor.calls == 0


def test_deny_never_executes_and_grant_cannot_override():
    dispatcher, executor = make(effect=PermissionEffect.DENY)
    prepared = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolDeniedError):
        dispatcher.execute(prepared, CONTEXT, ApprovalGrant(prepared.call_id, prepared.approval_fingerprint))
    assert executor.calls == 0


def test_ask_requires_exact_one_shot_grant():
    dispatcher, executor = make(effect=PermissionEffect.ASK)
    prepared = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolApprovalRequiredError):
        dispatcher.execute(prepared, CONTEXT)
    with pytest.raises(ToolApprovalMismatchError):
        dispatcher.execute(prepared, CONTEXT, ApprovalGrant("wrong", prepared.approval_fingerprint))
    with pytest.raises(ToolApprovalMismatchError):
        dispatcher.execute(prepared, CONTEXT, ApprovalGrant(prepared.call_id, "wrong"))
    assert executor.calls == 0
    dispatcher.execute(prepared, CONTEXT, ApprovalGrant(prepared.call_id, prepared.approval_fingerprint))
    assert executor.calls == 1
    with pytest.raises(ToolCallConsumedError):
        dispatcher.execute(prepared, CONTEXT, ApprovalGrant(prepared.call_id, prepared.approval_fingerprint))


def test_prepare_is_immune_to_input_mutation_and_fingerprints_are_deterministic():
    args = {"command": "git status"}
    dispatcher, _ = make()
    prepared = dispatcher.prepare(ToolCall("call-1", "fake", args), CONTEXT)
    args["command"] = "git push"
    same = make()[0].prepare(call(), CONTEXT)
    assert json_load(prepared.arguments_json) == {"command": "git status"}
    assert prepared.approval_fingerprint == same.approval_fingerprint


def test_changed_arguments_change_fingerprint():
    dispatcher, _ = make(resolver=lambda arguments, context: [PermissionRequest("shell", arguments["command"])])
    first = dispatcher.prepare(call(), CONTEXT)
    second = make(resolver=lambda arguments, context: [PermissionRequest("shell", arguments["command"])])[0].prepare(
        call({"command": "git push"}), CONTEXT
    )
    assert first.approval_fingerprint != second.approval_fingerprint


def test_permission_request_order_does_not_change_fingerprint():
    first_resolver = lambda arguments, context: [
        PermissionRequest("read", "a"), PermissionRequest("edit", "b")
    ]
    second_resolver = lambda arguments, context: [
        PermissionRequest("edit", "b"), PermissionRequest("read", "a")
    ]
    first = make(resolver=first_resolver)[0].prepare(call(), CONTEXT)
    second = make(resolver=second_resolver)[0].prepare(call(), CONTEXT)
    assert first.approval_fingerprint == second.approval_fingerprint


def test_permission_resource_change_changes_fingerprint():
    first = make(resolver=lambda arguments, context: [PermissionRequest("read", "a")])[0].prepare(call(), CONTEXT)
    second = make(resolver=lambda arguments, context: [PermissionRequest("read", "b")])[0].prepare(call(), CONTEXT)
    assert first.approval_fingerprint != second.approval_fingerprint


def test_tool_name_change_changes_fingerprint():
    first = make()[0].prepare(call(), CONTEXT)
    second = make(tool_name="other")[0].prepare(
        ToolCall("call-1", "other", {"command": "git status"}), CONTEXT
    )
    assert first.approval_fingerprint != second.approval_fingerprint


def test_executor_exception_and_invalid_return_are_wrapped():
    dispatcher, executor = make(executor=FakeExecutor(error=ValueError("boom")))
    prepared = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolExecutionError) as exc:
        dispatcher.execute(prepared, CONTEXT)
    assert isinstance(exc.value.__cause__, ValueError)
    with pytest.raises(ToolCallConsumedError):
        dispatcher.execute(prepared, CONTEXT)
    assert executor.calls == 1
    dispatcher, _ = make(executor=FakeExecutor(result="not observation"))
    prepared = dispatcher.prepare(call(), CONTEXT)
    with pytest.raises(ToolExecutionError):
        dispatcher.execute(prepared, CONTEXT)
    with pytest.raises(ToolCallConsumedError):
        dispatcher.execute(prepared, CONTEXT)


def test_observation_metadata_is_strict_json():
    with pytest.raises(ToolObservationError):
        ToolObservation("bad", {"value": math.inf})
    with pytest.raises(ToolObservationError):
        ToolObservation("bad", {"value": object()})


def json_load(raw):
    import json
    return json.loads(raw)
