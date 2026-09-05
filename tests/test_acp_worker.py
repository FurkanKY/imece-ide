"""Task 1 tests for the ACP Worker launch profile and resolver."""

from __future__ import annotations

import asyncio
import os
import threading
import warnings
from pathlib import Path

import acp
import pytest

from acp_runtime.client import _FatalSignal, _ImeceAcpClient
from acp_runtime.errors import (
    AcpAuthenticationRequiredError,
    AcpCleanupError,
    AcpEventSinkError,
    AcpLimitError,
    AcpProtocolError,
    AcpSpawnError,
    AcpTimeoutError,
)
from acp_runtime.events import (
    AcpPermissionRequested,
    AcpPermissionResolved,
    AcpSessionUpdateObserved,
)
from acp_runtime.models import AcpClientLimits, AcpRunResult
from acp_runtime.models import AcpLaunchSpec
from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError
from executor_runtime.acp_worker import (
    SAFE_REDACTED_DIAGNOSTIC_MESSAGE,
    AcpWorkerAttemptAdapter,
    AcpWorkerLaunchProfile,
    resolve_acp_worker_launch,
)
from fix_runtime.models import FixTrigger, FixTriggerKind, FixWorkerRequest
from process_runtime.models import ProcessResult
from run_runtime import RunEventType, RunRuntime, RunStore
from run_runtime.errors import EventSequenceError
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus
from workspace.base import Workspace
from workspace.local import LocalWorkspace
from workspace.worktree import GitWorktreeWorkspace


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return str(path)


def test_absolute_command_is_accepted_and_validated(tmp_path):
    executable = _executable(tmp_path / "agent")
    profile = AcpWorkerLaunchProfile(command=executable, env={"ONLY": "this"})

    resolved = resolve_acp_worker_launch(profile)

    assert isinstance(resolved, AcpLaunchSpec)
    assert resolved.argv == (executable,)
    assert dict(resolved.env) == {"ONLY": "this"}


def test_relative_command_resolves_once_with_shutil_which(tmp_path, monkeypatch):
    executable = _executable(tmp_path / "agent")
    calls = []

    def fake_which(command):
        calls.append(command)
        return executable

    monkeypatch.setattr("executor_runtime.acp_worker.shutil.which", fake_which)
    profile = AcpWorkerLaunchProfile(command="fake-agent")

    resolved = resolve_acp_worker_launch(profile)

    assert resolved.argv == (executable,)
    assert calls == ["fake-agent"]


def test_missing_command_raises_input_error(monkeypatch):
    monkeypatch.setattr("executor_runtime.acp_worker.shutil.which", lambda command: None)

    with pytest.raises(ExecutorAdapterInputError):
        resolve_acp_worker_launch(AcpWorkerLaunchProfile(command="missing-agent"))


def test_profile_preserves_args_in_order_and_exactly(tmp_path):
    executable = _executable(tmp_path / "agent")
    args = ("--stdio", "--mode", "worker", "argument with spaces")

    resolved = resolve_acp_worker_launch(
        AcpWorkerLaunchProfile(command=executable, args=args)
    )

    assert resolved.argv == (executable, *args)


def test_profile_freezes_caller_args_and_env_mutation(tmp_path):
    executable = _executable(tmp_path / "agent")
    args = ["--stdio"]
    env = {"WORKER_MODE": "initial"}
    profile = AcpWorkerLaunchProfile(command=executable, args=args, env=env)

    args.append("--changed")
    env["WORKER_MODE"] = "changed"
    env["NEW"] = "value"

    resolved = resolve_acp_worker_launch(profile)

    assert profile.args == ("--stdio",)
    assert resolved.argv == (executable, "--stdio")
    assert dict(resolved.env) == {"WORKER_MODE": "initial"}


def test_profile_does_not_merge_host_environment(tmp_path):
    executable = _executable(tmp_path / "agent")
    profile = AcpWorkerLaunchProfile(command=executable, env={"PROFILE_ONLY": "yes"})

    resolved = resolve_acp_worker_launch(profile)

    assert dict(resolved.env) == {"PROFILE_ONLY": "yes"}
    assert "PATH" not in resolved.env or os.environ.get("PATH") != resolved.env["PATH"]


@pytest.mark.parametrize(
    "profile_kwargs",
    [
        {"command": ""},
        {"command": "agent\x00name"},
        {"command": "agent", "args": "--stdio"},
        {"command": "agent", "args": ("",)},
        {"command": "agent", "args": ("bad\x00arg",)},
        {"command": "agent", "env": []},
        {"command": "agent", "env": {"KEY": 1}},
        {"command": "agent", "env": {"KEY\x00": "value"}},
    ],
)
def test_invalid_profile_values_are_input_errors(profile_kwargs):
    with pytest.raises(ExecutorAdapterInputError):
        AcpWorkerLaunchProfile(**profile_kwargs)


def test_profile_rejects_empty_environment_key():
    with pytest.raises(ExecutorAdapterInputError):
        AcpWorkerLaunchProfile(command="agent", env={"": "value"})


def test_profile_rejects_environment_key_containing_equals():
    with pytest.raises(ExecutorAdapterInputError):
        AcpWorkerLaunchProfile(command="agent", env={"A=B": "value"})


def test_profile_empty_env_remains_empty():
    profile = AcpWorkerLaunchProfile(command="agent")
    assert dict(profile.env) == {}


def test_profile_empty_environment_value_is_valid():
    profile = AcpWorkerLaunchProfile(command="agent", env={"KEY": ""})
    assert dict(profile.env) == {"KEY": ""}


def _running_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run


def _acp_result(session_id="session-1"):
    return AcpRunResult(
        session_id=session_id,
        stop_reason="completed",
        update_count=1,
        update_chars=5,
        permission_request_count=0,
        session_close_supported=True,
        session_close_succeeded=True,
    )


def _start_sink(runtime, run, execution_id="execution-1"):
    from run_runtime.acp import CanonicalAcpEventSink

    sink = CanonicalAcpEventSink(runtime, run.run_id, execution_id=execution_id)
    sink.start("fix task")
    return sink


def test_sink_requires_running_run(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)

    from run_runtime.acp import CanonicalAcpEventSink

    with pytest.raises(ValueError, match="RUNNING"):
        CanonicalAcpEventSink(runtime, run.run_id, execution_id="execution-1")


def test_sink_start_records_exactly_one_execution_started(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    _start_sink(runtime, run)

    events = runtime.events(run.run_id, limit=20).events
    assert [event.type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.EXECUTION_STARTED,
    ]


def test_execution_started_payload_is_exact_transport_and_task(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    _start_sink(runtime, run)

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.payload == {"transport": "acp", "task": "fix task"}


def test_sink_rejects_duplicate_start_and_terminal_events(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    with pytest.raises(RuntimeError, match="start"):
        sink.start("another task")

    sink.complete(_acp_result())
    with pytest.raises(RuntimeError, match="terminal"):
        sink.complete(_acp_result())
    with pytest.raises(RuntimeError, match="terminal"):
        sink.fail(RuntimeError("late failure"))


def test_sink_uses_supplied_execution_id_on_every_event(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run, execution_id="supplied-execution")
    sink.complete(_acp_result())

    events = runtime.events(run.run_id, limit=20).events[1:]
    assert len(events) == 2
    assert all(event.execution_id == "supplied-execution" for event in events)
    assert all(event.correlation_id == "supplied-execution" for event in events)
    assert all(event.source == "acp_worker" for event in events)


def test_first_transient_event_binds_session_id(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 5))
    sink.complete(_acp_result("session-1"))

    events = runtime.events(run.run_id, limit=20).events
    assert events[-2].payload["session_id"] == "session-1"
    assert events[-1].payload["session_id"] == "session-1"


def test_foreign_later_session_id_is_rejected(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 5))

    with pytest.raises(ValueError, match="session_id"):
        sink.emit(AcpSessionUpdateObserved("foreign-session", _sdk_update("no"), 2))

    assert len(runtime.events(run.run_id, limit=20).events) == 3


def test_completion_binds_session_when_no_updates_occurred(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    sink.complete(_acp_result("session-without-updates"))

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.EXECUTION_COMPLETED
    assert event.payload["session_id"] == "session-without-updates"


def test_completion_rejects_result_session_mismatch(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 5))

    with pytest.raises(ValueError, match="session_id"):
        sink.complete(_acp_result("foreign-session"))

    assert runtime.events(run.run_id, limit=20).events[-1].type == RunEventType.EXECUTION_OUTPUT


def test_sink_never_refreshes_after_event_sequence_conflict(tmp_path, monkeypatch):
    runtime, run = _running_runtime(tmp_path)
    from run_runtime.acp import CanonicalAcpEventSink

    sink = CanonicalAcpEventSink(runtime, run.run_id, execution_id="execution-1")
    get_run_calls = []
    original_get_run = runtime.get_run

    def tracked_get_run(run_id):
        get_run_calls.append(run_id)
        return original_get_run(run_id)

    monkeypatch.setattr(runtime, "get_run", tracked_get_run)
    runtime.record(run_id=run.run_id, type="future.external", payload={})

    with pytest.raises(EventSequenceError):
        sink.start("fix task")

    assert get_run_calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [
        RunEventType.RUN_STARTED,
        "future.external",
    ]


def _sdk_update(text="hello"):
    return acp.schema.AgentMessageChunk(
        sessionUpdate="agent_message_chunk",
        content=acp.schema.TextContentBlock(type="text", text=text),
    )


def test_session_update_maps_to_execution_output_with_json_data(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    update = _sdk_update()

    sink.emit(AcpSessionUpdateObserved("session-1", update, 154))

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.EXECUTION_OUTPUT
    assert event.payload == {
        "transport": "acp",
        "session_id": "session-1",
        "update": update.model_dump(mode="json", by_alias=True, exclude_none=True),
        "serialized_chars": 154,
    }


def test_session_update_rejects_nul_session_id(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    with pytest.raises(ValueError, match="session_id|NUL"):
        sink.emit(AcpSessionUpdateObserved("bad\x00session", _sdk_update(), 5))

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_session_update_rejects_oversized_session_id(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    oversized = "s" * 2001

    with pytest.raises(ValueError, match="session_id|2000"):
        sink.emit(AcpSessionUpdateObserved(oversized, _sdk_update(), 5))

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_completion_rejects_oversized_session_id(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    oversized = "s" * 2001

    with pytest.raises(ValueError, match="session_id|2000"):
        sink.complete(_acp_result(oversized))

    assert runtime.events(run.run_id, limit=20).events[-1].type == RunEventType.EXECUTION_STARTED


def test_nonserializable_update_is_rejected_without_stringification(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    arbitrary = object()

    with pytest.raises(TypeError, match="JSON|serial"):
        sink.emit(AcpSessionUpdateObserved("session-1", arbitrary, 1))

    events = runtime.events(run.run_id, limit=20).events
    assert len(events) == 2
    assert all("object at" not in str(event.payload) for event in events)


def test_json_shaped_non_sdk_update_is_rejected(tmp_path):
    """A plain dict/list/str/int/float/bool update must never bypass the
    official SDK/Pydantic model_dump() serialization surface, even though it
    is already JSON-compatible on its own."""
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    raw_dict_update = {"text": "hello", "sessionUpdate": "agent_message_chunk"}

    with pytest.raises(TypeError, match="model_dump"):
        sink.emit(AcpSessionUpdateObserved("session-1", raw_dict_update, 5))

    events = runtime.events(run.run_id, limit=20).events
    assert len(events) == 2
    assert all("hello" not in str(event.payload) for event in events)


def test_update_persistence_failure_surfaces_underlying_canonical_error(tmp_path, monkeypatch):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    expected = RuntimeError("canonical append failed")

    def fail_record_many(**kwargs):
        raise expected

    monkeypatch.setattr(runtime, "record_many", fail_record_many)

    with pytest.raises(RuntimeError) as raised:
        sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 154))

    assert raised.value is expected
    assert not isinstance(raised.value, AcpEventSinkError)


def test_acp_client_wraps_canonical_sink_failure_as_acp_event_sink_error():
    expected = RuntimeError("canonical append failed")

    class RaisingSink:
        def emit(self, event):
            raise expected

    fatal = _FatalSignal()
    client = _ImeceAcpClient(
        limits=AcpClientLimits(),
        event_sink=RaisingSink(),
        fatal=fatal,
    )

    assert client._emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 154)) is False
    assert isinstance(fatal.error, AcpEventSinkError)
    assert fatal.error.__cause__ is expected


def test_failure_diagnostic_removes_nul_before_bounding(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    error = RuntimeError("a" * 1_999 + "\x00" + "b" * 10)

    sink.fail(error)

    event = runtime.events(run.run_id, limit=20).events[-1]
    message = event.payload["message"]
    assert message == "a" * 1_999 + "b"
    assert len(message) == 2_000
    assert "\x00" not in message


def test_permission_request_with_empty_optional_title_is_persisted(tmp_path):
    """ACP ToolCallUpdate.title is optional/nullable; the 3J2A client already
    normalizes an absent title to "". Persisting title="" must not be a
    fatal sink failure -- only session_id/tool_call_id/option_id/outcome
    remain non-empty-required."""
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    sink.emit(
        AcpPermissionRequested(
            session_id="session-1",
            tool_call_id="tool-1",
            title="",
            option_ids=["allow-once"],
        )
    )

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.PERMISSION_REQUESTED
    assert event.payload["title"] == ""


def test_permission_requested_maps_without_waiting_user(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    sink.emit(
        AcpPermissionRequested(
            session_id="session-1",
            tool_call_id="tool-1",
            title="Write file",
            option_ids=["allow-once", "reject-once"],
        )
    )

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.PERMISSION_REQUESTED
    assert event.payload == {
        "transport": "acp",
        "session_id": "session-1",
        "tool_call_id": "tool-1",
        "title": "Write file",
        "option_ids": ["allow-once", "reject-once"],
    }
    assert all(event.type not in {RunEventType.RUN_WAITING_USER, RunEventType.RUN_RESUMED} for event in runtime.events(run.run_id, limit=20).events)


def test_permission_resolved_preserves_cancelled_outcome(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    sink.emit(
        AcpPermissionResolved(
            session_id="session-1",
            tool_call_id="tool-1",
            outcome="cancelled",
        )
    )

    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.PERMISSION_RESOLVED
    assert event.payload == {
        "transport": "acp",
        "session_id": "session-1",
        "tool_call_id": "tool-1",
        "outcome": "cancelled",
    }
    assert all(event.type not in {RunEventType.RUN_WAITING_USER, RunEventType.RUN_RESUMED} for event in runtime.events(run.run_id, limit=20).events)


def test_permission_title_over_limit_is_rejected_without_truncation(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    title = "t" * 2001

    with pytest.raises(ValueError, match="title|2000"):
        sink.emit(AcpPermissionRequested("session-1", "tool-1", title, ["allow"]))

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_permission_option_count_over_limit_is_rejected(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    with pytest.raises(ValueError, match="option"):
        sink.emit(
            AcpPermissionRequested(
                "session-1", "tool-1", "Write file", [f"option-{index}" for index in range(129)]
            )
        )

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_permission_option_id_over_limit_is_rejected_without_truncation(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)
    option_id = "o" * 2001

    with pytest.raises(ValueError, match="option_id|2000"):
        sink.emit(AcpPermissionRequested("session-1", "tool-1", "Write file", [option_id]))

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_permission_nul_fact_is_rejected(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    with pytest.raises(ValueError, match="NUL"):
        sink.emit(AcpPermissionRequested("session-1", "tool\x00-1", "Write file", ["allow"]))

    assert len(runtime.events(run.run_id, limit=20).events) == 2


def test_acp_updates_do_not_emit_native_lifecycle_events(tmp_path):
    runtime, run = _running_runtime(tmp_path)
    sink = _start_sink(runtime, run)

    sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 154))
    sink.emit(AcpPermissionRequested("session-1", "tool-1", "Write file", ["allow"]))
    sink.emit(AcpPermissionResolved("session-1", "tool-1", "cancelled"))

    event_types = {event.type for event in runtime.events(run.run_id, limit=20).events}
    assert RunEventType.EXECUTION_OUTPUT in event_types
    assert RunEventType.PERMISSION_REQUESTED in event_types
    assert RunEventType.PERMISSION_RESOLVED in event_types
    assert not event_types.intersection(
        {
            RunEventType.TURN_STARTED,
            RunEventType.TURN_COMPLETED,
            RunEventType.MODEL_STARTED,
            RunEventType.MODEL_COMPLETED,
            RunEventType.MODEL_FAILED,
            RunEventType.TOOL_REQUESTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
            RunEventType.TOOL_FAILED,
            RunEventType.USAGE_RECORDED,
            RunEventType.RUN_WAITING_USER,
            RunEventType.RUN_RESUMED,
        }
    )


class _FakeAcpClient:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or _acp_result()

    async def run(self, launch, request, *, limits=None, event_sink=None):
        self.calls.append({"launch": launch, "request": request, "limits": limits, "event_sink": event_sink})
        return self.result


class _RaisingAcpClient(_FakeAcpClient):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def run(self, launch, request, *, limits=None, event_sink=None):
        self.calls.append({"launch": launch, "request": request, "limits": limits, "event_sink": event_sink})
        raise self.error


class _ArbitraryWorkspace(Workspace):
    @property
    def root(self):
        return Path("/arbitrary")

    def dispose(self):
        return None


def _fake_worktree(root: Path) -> GitWorktreeWorkspace:
    workspace = object.__new__(GitWorktreeWorkspace)
    workspace._root = root
    return workspace


def _worker_request(rendered_input="rendered fix input"):
    report = VerificationReport(
        verification_id="ver-1",
        plan_id="plan-1",
        results=(
            VerificationCheckResult(
                "check-1", "Check", VerificationStatus.FAIL,
                process_result=ProcessResult(
                    argv=("true",), cwd=".", exit_code=1, timed_out=False,
                    duration_ms=1, stdout="", stderr="", stdout_truncated=False,
                    stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
                ),
            ),
        ),
        duration_ms=1,
    )
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=report)
    return FixWorkerRequest(
        task="fix task",
        trigger=trigger,
        attempt_index=1,
        rendered_input=rendered_input,
    )


def _adapter(tmp_path, client=None, *, limits=None):
    runtime, run = _running_runtime(tmp_path)
    client = client or _FakeAcpClient()
    profile = AcpWorkerLaunchProfile(command=_executable(tmp_path / "agent"))
    return (
        AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client, limits=limits),
        runtime,
        run,
        client,
    )


def test_non_fix_request_rejected_before_any_side_effect(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_fake_worktree(tmp_path), object(), execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_local_workspace_rejected_before_any_side_effect(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)
    workspace = LocalWorkspace(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, _worker_request(), execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_arbitrary_workspace_rejected_before_any_side_effect(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_ArbitraryWorkspace(), _worker_request(), execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_invalid_execution_id_rejected_before_any_side_effect(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="bad id")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_unresolved_executable_rejected_before_any_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr("executor_runtime.acp_worker.shutil.which", lambda command: None)
    runtime, run = _running_runtime(tmp_path)
    client = _FakeAcpClient()
    adapter = AcpWorkerAttemptAdapter(
        runtime, run.run_id, AcpWorkerLaunchProfile(command="missing"), client,
    )

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


@pytest.mark.parametrize("bad_root,bad_prompt", [("relative", "valid"), ("absolute", "   ")])
def test_invalid_cwd_or_prompt_rejected_before_any_side_effect(tmp_path, bad_root, bad_prompt):
    adapter, runtime, run, client = _adapter(tmp_path)
    root = Path(bad_root) if bad_root == "relative" else tmp_path / "missing"
    workspace = _fake_worktree(root)
    request = _worker_request("valid")
    if bad_prompt != "valid":
        object.__setattr__(request, "rendered_input", bad_prompt)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(workspace, request, execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_prompt_over_effective_acp_limit_is_rejected_before_execution_started(tmp_path):
    configured = AcpClientLimits(max_prompt_chars=3)
    adapter, runtime, run, client = _adapter(tmp_path, limits=configured)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_fake_worktree(tmp_path), _worker_request("too long"), execution_id="execution-1")

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_prompt_request_uses_exact_worktree_root_and_rendered_input(tmp_path):
    client = _FakeAcpClient()
    adapter, runtime, run, client = _adapter(tmp_path, client=client)
    request = _worker_request("EXACT rendered input")
    workspace = _fake_worktree(tmp_path)

    result = adapter.run(workspace, request, execution_id="execution-1")

    assert result.execution_id == "execution-1"
    assert client.calls[0]["request"].cwd == str(workspace.root)
    assert client.calls[0]["request"].prompt == request.rendered_input


def test_injected_structural_fake_acp_client_is_supported(tmp_path):
    client = _FakeAcpClient()
    adapter, _, _, _ = _adapter(tmp_path, client=client)

    result = adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert result.execution_id == "execution-1"
    assert len(client.calls) == 1


def test_adapter_passes_exact_effective_limits_instance_to_acp_client(tmp_path):
    configured = AcpClientLimits(max_prompt_chars=500)
    adapter, _, _, client = _adapter(tmp_path, limits=configured)

    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert client.calls[0]["limits"] is configured


def test_adapter_reuses_one_default_limits_instance_for_validation_and_acp_call(tmp_path, monkeypatch):
    constructed = []
    original = AcpClientLimits

    class TrackingLimits(original):
        def __post_init__(self):
            constructed.append(self)
            super().__post_init__()

    monkeypatch.setattr("executor_runtime.acp_worker.AcpClientLimits", TrackingLimits)
    client = _FakeAcpClient()
    adapter, _, _, _ = _adapter(tmp_path, client=client, limits=None)

    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert len(constructed) == 1
    assert client.calls[0]["limits"] is constructed[0]


def test_supplied_execution_id_is_returned_exactly(tmp_path):
    adapter, _, _, _ = _adapter(tmp_path)
    result = adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="supplied-execution")
    assert result.execution_id == "supplied-execution"


def test_run_id_property_matches_constructor_value(tmp_path):
    adapter, _, run, _ = _adapter(tmp_path)
    assert adapter.run_id == run.run_id


def test_non_running_run_rejected_before_execution_started(tmp_path):
    """A non-RUNNING Run makes CanonicalAcpEventSink's constructor raise a
    raw ValueError; the adapter must translate that to
    ExecutorAdapterInputError since no execution has begun and no side
    effect has occurred yet."""
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)  # never started -> not RUNNING
    client = _FakeAcpClient()
    profile = AcpWorkerLaunchProfile(command=_executable(tmp_path / "agent"))
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    with pytest.raises(ExecutorAdapterInputError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert client.calls == []
    assert list(runtime.events(run.run_id, limit=20).events) == []


def test_sync_run_invokes_acp_runtime_once(tmp_path):
    adapter, _, _, client = _adapter(tmp_path)

    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert len(client.calls) == 1


def test_second_adapter_call_invokes_a_fresh_acp_runtime_run(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)

    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")
    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-2")

    assert len(client.calls) == 2
    assert client.calls[0]["event_sink"] is not client.calls[1]["event_sink"]
    assert [event.type for event in runtime.events(run.run_id, limit=20).events].count(
        RunEventType.EXECUTION_STARTED
    ) == 2


def test_running_event_loop_is_rejected_before_start_or_connect(tmp_path):
    adapter, runtime, run, client = _adapter(tmp_path)

    async def invoke():
        with pytest.raises(ExecutorAdapterExecutionError):
            adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    asyncio.run(invoke())

    assert client.calls == []
    assert [event.type for event in runtime.events(run.run_id, limit=20).events] == [RunEventType.RUN_STARTED]


def test_running_event_loop_does_not_create_unawaited_coroutine_warning(tmp_path):
    adapter, _, _, _ = _adapter(tmp_path)
    captured = []

    async def invoke():
        with pytest.raises(ExecutorAdapterExecutionError):
            adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(invoke())
        captured.extend(caught)

    assert not [warning for warning in captured if warning.category is RuntimeWarning]


def test_no_background_loop_thread_remains_after_run(tmp_path):
    adapter, _, _, _ = _adapter(tmp_path)
    before = {thread.ident for thread in threading.enumerate()}

    adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    after = {thread.ident for thread in threading.enumerate()}
    assert after == before


def test_success_records_only_real_acp_result_facts_and_returns_worker_result(tmp_path):
    result = _acp_result()
    client = _FakeAcpClient(result=result)
    adapter, runtime, run, _ = _adapter(tmp_path, client=client)

    worker_result = adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert worker_result.execution_id == "execution-1"
    event = runtime.events(run.run_id, limit=20).events[-1]
    assert event.type == RunEventType.EXECUTION_COMPLETED
    assert event.payload == {
        "transport": "acp",
        "session_id": result.session_id,
        "stop_reason": result.stop_reason,
        "update_count": result.update_count,
        "update_chars": result.update_chars,
        "permission_request_count": result.permission_request_count,
        "session_close_supported": result.session_close_supported,
        "session_close_succeeded": result.session_close_succeeded,
    }
    assert "final_text" not in event.payload
    assert "model_turns" not in event.payload
    assert "tool_calls" not in event.payload
    assert "input_tokens" not in event.payload
    assert "output_tokens" not in event.payload
    assert "cost_usd" not in event.payload


@pytest.mark.parametrize(
    "error_type",
    [
        AcpSpawnError,
        AcpProtocolError,
        AcpAuthenticationRequiredError,
        AcpTimeoutError,
        AcpLimitError,
        AcpEventSinkError,
        AcpCleanupError,
    ],
)
def test_each_expected_acp_error_records_failure_and_translates(tmp_path, error_type):
    client = _RaisingAcpClient(error_type("acp failed"))
    adapter, runtime, run, _ = _adapter(tmp_path, client=client)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    events = runtime.events(run.run_id, limit=20).events
    assert [event.type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.EXECUTION_STARTED,
        RunEventType.EXECUTION_FAILED,
    ]
    assert events[-1].payload["transport"] == "acp"


def test_translated_error_preserves_original_acp_error_as_cause(tmp_path):
    original = AcpProtocolError("protocol failed")
    adapter, _, _, _ = _adapter(tmp_path, client=_RaisingAcpClient(original))

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert raised.value.__cause__ is original


def test_failure_payload_is_bounded_and_excludes_prompt_and_environment(tmp_path):
    request = _worker_request("SECRET_PROMPT")
    profile = AcpWorkerLaunchProfile(
        command=_executable(tmp_path / "agent"),
        env={"TOKEN": "SECRET_ENV"},
    )
    runtime, run = _running_runtime(tmp_path)
    # The original exception message actually CONTAINS the prompt and the
    # environment key/value, so this test genuinely proves redaction rather
    # than merely proving that unrelated padding text is absent.
    error = RuntimeError(
        f"failed while processing {request.rendered_input}; TOKEN=SECRET_ENV " + "x" * 2000
    )
    client = _RaisingAcpClient(error)
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), request, execution_id="execution-1")

    payload = runtime.events(run.run_id, limit=20).events[-1].payload
    # The raw message contains real sensitive material, so the fail-closed
    # policy replaces the whole diagnostic rather than attempting partial
    # substitution.
    assert payload["message"] == SAFE_REDACTED_DIAGNOSTIC_MESSAGE
    assert "SECRET_PROMPT" not in str(payload)
    assert "SECRET_ENV" not in str(payload)
    assert "TOKEN" not in str(payload)


def test_failure_diagnostic_redaction_is_safe_when_env_key_is_prefix_of_value(tmp_path):
    """A naive sequential str.replace(key) then str.replace(value) leaves a
    fragment behind when the key is a prefix of the value: replacing "TOKEN"
    first turns "TOKEN_SECRET" into "[...redacted...]_SECRET", after which
    the later exact-value replacement for "TOKEN_SECRET" can never match."""
    profile = AcpWorkerLaunchProfile(
        command=_executable(tmp_path / "agent"),
        env={"TOKEN": "TOKEN_SECRET"},
    )
    runtime, run = _running_runtime(tmp_path)
    error = RuntimeError("failed with TOKEN_SECRET rejected by agent")
    client = _RaisingAcpClient(error)
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    message = runtime.events(run.run_id, limit=20).events[-1].payload["message"]
    assert "TOKEN" not in message
    assert "TOKEN_SECRET" not in message
    assert "_SECRET" not in message
    assert message == SAFE_REDACTED_DIAGNOSTIC_MESSAGE


def test_failure_diagnostic_redaction_is_safe_when_prompt_overlaps_env_value(tmp_path):
    """Proves no fragment of a known secret survives merely because a
    different, longer sensitive literal was matched/replaced first."""
    rendered_input = "prompt-secret"
    env_value = "prefix-prompt-secret-suffix"
    request = _worker_request(rendered_input)
    profile = AcpWorkerLaunchProfile(
        command=_executable(tmp_path / "agent"),
        env={"ENVKEY": env_value},
    )
    runtime, run = _running_runtime(tmp_path)
    error = RuntimeError(f"agent rejected value: {env_value}")
    client = _RaisingAcpClient(error)
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), request, execution_id="execution-1")

    message = runtime.events(run.run_id, limit=20).events[-1].payload["message"]
    for forbidden in ("prompt-secret", "prefix-prompt-secret-suffix", "prefix-", "-suffix"):
        assert forbidden not in message
    assert message == SAFE_REDACTED_DIAGNOSTIC_MESSAGE


def test_failure_diagnostic_redacts_sensitive_launch_argument(tmp_path):
    """Launch argv members are opaque at this layer and may carry
    provider/authentication material, so they are sensitive literals too."""
    profile = AcpWorkerLaunchProfile(
        command=_executable(tmp_path / "agent"),
        args=("--token=LAUNCH_SECRET",),
    )
    runtime, run = _running_runtime(tmp_path)
    error = RuntimeError("agent rejected argument --token=LAUNCH_SECRET")
    client = _RaisingAcpClient(error)
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    message = runtime.events(run.run_id, limit=20).events[-1].payload["message"]
    assert "--token=LAUNCH_SECRET" not in message
    assert "LAUNCH_SECRET" not in message
    assert message == SAFE_REDACTED_DIAGNOSTIC_MESSAGE


def test_failure_diagnostic_is_exactly_bounded_to_2000_chars(tmp_path):
    error = RuntimeError("a" * 3000)
    adapter, runtime, run, _ = _adapter(tmp_path, client=_RaisingAcpClient(error))

    with pytest.raises(ExecutorAdapterExecutionError):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    message = runtime.events(run.run_id, limit=20).events[-1].payload["message"]
    assert message == "a" * 2000


def test_ordinary_post_start_dependency_failure_gets_execution_failed(tmp_path):
    original = ValueError("dependency failed")
    adapter, runtime, run, _ = _adapter(tmp_path, client=_RaisingAcpClient(original))

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert raised.value.__cause__ is original
    assert runtime.events(run.run_id, limit=20).events[-1].type == RunEventType.EXECUTION_FAILED


def test_sink_complete_failure_never_returns_worker_result(tmp_path, monkeypatch):
    expected = RuntimeError("completion persistence failed")

    def fail_complete(self, result):
        raise expected

    monkeypatch.setattr("run_runtime.acp.CanonicalAcpEventSink.complete", fail_complete)
    adapter, runtime, run, _ = _adapter(tmp_path)

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert raised.value.__cause__ is expected
    assert runtime.events(run.run_id, limit=20).events[-1].type == RunEventType.EXECUTION_FAILED


def test_terminal_failure_persistence_error_wins_without_retry(tmp_path, monkeypatch):
    original = AcpProtocolError("original ACP failure")
    terminal = RuntimeError("terminal canonical failure")
    fail_calls = []

    def fail_terminal(self, error, **kwargs):
        fail_calls.append(error)
        raise terminal

    monkeypatch.setattr("run_runtime.acp.CanonicalAcpEventSink.fail", fail_terminal)
    adapter, _, _, _ = _adapter(tmp_path, client=_RaisingAcpClient(original))

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert len(fail_calls) == 1
    assert raised.value.__cause__ is terminal
    assert raised.value.__cause__.__context__ is original


def test_completion_sequence_conflict_skips_execution_failed_append(tmp_path, monkeypatch):
    """When sink.complete() itself fails because canonical persistence is
    already sequence-conflicted (a concurrent writer advanced the Run's
    event log after execution.started), the adapter must NOT attempt a
    second, stale-sequence execution.failed append."""

    class _ExternalWriterAcpClient(_FakeAcpClient):
        def __init__(self, runtime, run_id):
            super().__init__()
            self._runtime = runtime
            self._run_id = run_id

        async def run(self, launch, request, *, limits=None, event_sink=None):
            self.calls.append({"launch": launch, "request": request, "limits": limits, "event_sink": event_sink})
            # Simulate a concurrent external canonical writer advancing the
            # Run's event sequence before this attempt's sink can complete.
            self._runtime.record(run_id=self._run_id, type="external.concurrent", payload={})
            return self.result

    runtime, run = _running_runtime(tmp_path)
    client = _ExternalWriterAcpClient(runtime, run.run_id)
    profile = AcpWorkerLaunchProfile(command=_executable(tmp_path / "agent"))
    adapter = AcpWorkerAttemptAdapter(runtime, run.run_id, profile, client)

    record_many_calls = []
    original_record_many = runtime.record_many

    def tracked_record_many(**kwargs):
        record_many_calls.append(kwargs["expected_last_event_seq"])
        return original_record_many(**kwargs)

    monkeypatch.setattr(runtime, "record_many", tracked_record_many)

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert isinstance(raised.value.__cause__, EventSequenceError)
    # Exactly two attempts: execution.started, then the one failed
    # sink.complete() append. No third (fail()) attempt was made.
    assert len(record_many_calls) == 2

    types = [event.type for event in runtime.events(run.run_id, limit=20).events]
    assert RunEventType.EXECUTION_FAILED not in types
    assert types.count(RunEventType.EXECUTION_STARTED) == 1


def test_streaming_persistence_failure_does_not_attempt_terminal_append(tmp_path, monkeypatch):
    """When CanonicalAcpEventSink.emit() fails because canonical persistence
    itself is unavailable, and the ACP core wraps that as AcpEventSinkError
    (reproducing the real _ImeceAcpClient._emit() wrapping shape), the
    adapter must not attempt a stale-sequence execution.failed append, and
    the underlying canonical failure -- not the ACP wrapper -- must be the
    primary infrastructure defect."""
    underlying = RuntimeError("canonical append failed")

    class _EmitFailingAcpClient(_FakeAcpClient):
        async def run(self, launch, request, *, limits=None, event_sink=None):
            self.calls.append({"launch": launch, "request": request, "limits": limits, "event_sink": event_sink})
            try:
                event_sink.emit(AcpSessionUpdateObserved("session-1", _sdk_update(), 10))
            except Exception as exc:
                wrapped = AcpEventSinkError(f"AcpEventSink.emit failed: {exc}")
                wrapped.__cause__ = exc
                raise wrapped
            return self.result

    adapter, runtime, run, client = _adapter(tmp_path, client=_EmitFailingAcpClient())

    call_count = {"n": 0}
    original_record_many = runtime.record_many

    def flaky_record_many(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original_record_many(**kwargs)
        raise underlying

    monkeypatch.setattr(runtime, "record_many", flaky_record_many)

    with pytest.raises(ExecutorAdapterExecutionError) as raised:
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")

    assert raised.value.__cause__ is underlying
    assert isinstance(raised.value.__context__, AcpEventSinkError)
    assert call_count["n"] == 2  # execution.started + one failed emit append; no retry

    types = [event.type for event in runtime.events(run.run_id, limit=20).events]
    assert RunEventType.EXECUTION_FAILED not in types


@pytest.mark.parametrize("control_exception", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_cancellation_keyboard_interrupt_and_system_exit_are_not_translated(tmp_path, control_exception):
    original = control_exception()
    adapter, _, _, _ = _adapter(tmp_path, client=_RaisingAcpClient(original))

    with pytest.raises(control_exception):
        adapter.run(_fake_worktree(tmp_path), _worker_request(), execution_id="execution-1")
