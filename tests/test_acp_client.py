"""AcpClientRuntime unit tests, driven through injected fake connect/cleanup
seams (constructor-level) rather than real subprocesses. See spec section 40
and the 3J2A hardening-round-1 requirements.
"""

import asyncio
import contextlib
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import acp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp_runtime.client import AcpClientRuntime as _AcpClientRuntime  # noqa: E402
from acp_runtime.errors import (  # noqa: E402
    AcpAuthenticationRequiredError,
    AcpCleanupError,
    AcpEventSinkError,
    AcpInputError,
    AcpLimitError,
    AcpProtocolError,
    AcpTimeoutError,
)
from acp_runtime.events import (  # noqa: E402
    AcpPermissionRequested,
    AcpPermissionResolved,
    AcpSessionUpdateObserved,
)
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest  # noqa: E402
from process_runtime.errors import ProcessCleanupError  # noqa: E402


# ---------------- fakes ----------------


class _FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


@dataclass
class _NewSessionResult:
    session_id: str


@dataclass
class _PromptResult:
    stop_reason: str = "end_turn"


class _FakeConnection:
    def __init__(
        self,
        client: Any,
        *,
        session_id: str = "sess-1",
        session_close_supported: bool = True,
        initialize_error: Exception | None = None,
        new_session_error: Exception | None = None,
        prompt_behavior=None,
        close_session_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self._session_close_supported = session_close_supported
        self._initialize_error = initialize_error
        self._new_session_error = new_session_error
        self._prompt_behavior = prompt_behavior
        self._close_session_error = close_session_error
        self._close_error = close_error
        self.calls: list[tuple[str, dict]] = []
        self.cancel_called = False
        self.close_session_called = False
        self.close_called = False

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        self.calls.append(("initialize", dict(protocol_version=protocol_version, client_capabilities=client_capabilities)))
        if self._initialize_error is not None:
            raise self._initialize_error
        close_cap = acp.schema.SessionCloseCapabilities() if self._session_close_supported else None
        return acp.InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=acp.schema.AgentCapabilities(
                session_capabilities=acp.schema.SessionCapabilities(close=close_cap),
            ),
            auth_methods=[],
        )

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        self.calls.append(("new_session", dict(cwd=cwd, mcp_servers=mcp_servers)))
        if self._new_session_error is not None:
            raise self._new_session_error
        return _NewSessionResult(session_id=self.session_id)

    async def prompt(self, session_id, prompt, **kwargs):
        self.calls.append(("prompt", dict(session_id=session_id, prompt=list(prompt))))
        if self._prompt_behavior is None:
            return _PromptResult()
        return await self._prompt_behavior(self)

    async def cancel(self, session_id, **kwargs):
        self.cancel_called = True

    async def close_session(self, session_id, **kwargs):
        self.close_session_called = True
        if self._close_session_error is not None:
            raise self._close_session_error
        return None

    async def close(self) -> None:
        self.close_called = True
        if self._close_error is not None:
            raise self._close_error


def _make_connect(build_connection, *, spawn_calls: list | None = None, pid: int = 4242):
    async def _connect(client, argv, env, cwd):
        if spawn_calls is not None:
            spawn_calls.append({"client": client, "argv": argv, "env": env, "cwd": cwd})
        conn = build_connection(client)
        process = _FakeProcess(pid)
        return conn, process

    return _connect


def _fake_capture_process_tree(pid):
    return None


def _fake_terminate_process_tree(pid, snapshot=None):
    return None


def _make_runtime(
    *, _connect, _terminate_process_tree=None, _capture_process_tree=None,
):
    """Build a fake-process runtime that never touches production psutil.

    Tests that verify cleanup replace the default no-op with an explicit
    recording or failing seam.
    """
    return _AcpClientRuntime(
        _connect=_connect,
        _terminate_process_tree=(
            _fake_terminate_process_tree if _terminate_process_tree is None else _terminate_process_tree
        ),
        _capture_process_tree=(
            _fake_capture_process_tree if _capture_process_tree is None else _capture_process_tree
        ),
    )


# Keep the existing unit-test call shape while making its default fake process
# cleanup seams explicit and safe.
AcpClientRuntime = _make_runtime


def _valid_launch():
    return AcpLaunchSpec(argv=("/usr/bin/fake-agent",))


def _valid_request(tmp_path):
    return AcpPromptRequest(cwd=str(tmp_path), prompt="do the thing")


class _RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _agent_message_update(text="hello"):
    return acp.update_agent_message_text(text)


async def _echo_prompt(conn):
    await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update())
    return _PromptResult(stop_reason="end_turn")


# ---------------- input validation (no spawn) ----------------


def test_malformed_launch_spec_causes_zero_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run("not a launch spec", _valid_request(tmp_path)))
    assert spawn_calls == []


def test_malformed_prompt_request_causes_zero_spawn():
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), "not a request"))
    assert spawn_calls == []


def test_nonexistent_cwd_causes_zero_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    request = AcpPromptRequest(cwd=str(tmp_path / "does-not-exist"), prompt="task")
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), request))
    assert spawn_calls == []


def test_cwd_that_is_a_file_causes_zero_spawn(tmp_path):
    file_path = tmp_path / "afile"
    file_path.write_text("x")
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    request = AcpPromptRequest(cwd=str(file_path), prompt="task")
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), request))
    assert spawn_calls == []


def test_oversized_prompt_causes_zero_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    limits = AcpClientLimits(max_prompt_chars=5)
    request = AcpPromptRequest(cwd=str(tmp_path), prompt="way too long")
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), request, limits=limits))
    assert spawn_calls == []


def test_invalid_limits_type_causes_zero_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))
    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits="not limits"))
    assert spawn_calls == []


def test_event_sink_without_emit_causes_zero_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))

    class NoEmit:
        pass

    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), event_sink=NoEmit()))
    assert spawn_calls == []


def test_async_emit_event_sink_rejected_before_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c), spawn_calls=spawn_calls))

    class AsyncSink:
        async def emit(self, event):
            pass

    with pytest.raises(AcpInputError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), event_sink=AsyncSink()))
    assert spawn_calls == []


# ---------------- success path ----------------


def test_fresh_run_means_fresh_spawn(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt), spawn_calls=spawn_calls)
    )
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert len(spawn_calls) == 2


def test_fake_process_success_path_never_reaches_real_process_cleanup(tmp_path, monkeypatch):
    import acp_runtime.client as client_module

    def forbidden_capture(pid):
        raise AssertionError("fake-process unit test reached real capture_process_tree")

    def forbidden_terminate(pid, snapshot=None):
        raise AssertionError("fake-process unit test reached real terminate_process_tree")

    monkeypatch.setattr(client_module, "capture_process_tree", forbidden_capture)
    monkeypatch.setattr(client_module, "terminate_process_tree", forbidden_terminate)

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)))
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.stop_reason == "end_turn"


def test_protocol_version_used(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    init_call = conns[0].calls[0]
    assert init_call[0] == "initialize"
    assert init_call[1]["protocol_version"] == acp.PROTOCOL_VERSION


def test_no_capability_advertised():
    # ClientCapabilities() default (used when client_capabilities=None is
    # passed through) advertises no fs/terminal/auth-terminal capability.
    caps = acp.schema.ClientCapabilities()
    dumped = caps.model_dump(by_alias=True, exclude_none=True)
    assert dumped["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert dumped["terminal"] is False


def test_client_capabilities_none_passed_through(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert conns[0].calls[0][1]["client_capabilities"] is None


def test_no_fs_terminal_elicitation_handlers_on_client(tmp_path):
    spawned_client = {}

    def build(client):
        spawned_client["client"] = client
        return _FakeConnection(client, prompt_behavior=_echo_prompt)

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    client = spawned_client["client"]
    for attr in ("read_text_file", "write_text_file", "create_terminal", "create_elicitation"):
        assert not hasattr(client, attr)


def test_new_session_uses_exact_absolute_cwd(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    request = _valid_request(tmp_path)
    asyncio.run(runtime.run(_valid_launch(), request))
    new_session_call = conns[0].calls[1]
    assert new_session_call[0] == "new_session"
    assert new_session_call[1]["cwd"] == request.cwd


def test_mcp_servers_empty_list(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert conns[0].calls[1][1]["mcp_servers"] == []


def test_exact_prompt_passed_as_one_text_block(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    request = _valid_request(tmp_path)
    asyncio.run(runtime.run(_valid_launch(), request))
    prompt_call = conns[0].calls[2]
    assert prompt_call[0] == "prompt"
    blocks = prompt_call[1]["prompt"]
    assert len(blocks) == 1
    assert blocks[0].text == request.prompt


def test_session_id_and_stop_reason_propagated(tmp_path):
    async def custom_stop(conn):
        return _PromptResult(stop_reason="max_turn_requests")

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, session_id="sess-xyz", prompt_behavior=custom_stop))
    )
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.session_id == "sess-xyz"
    assert result.stop_reason == "max_turn_requests"


def test_argv_and_env_passed_to_connect_exactly(tmp_path):
    spawn_calls = []
    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt), spawn_calls=spawn_calls)
    )
    launch = AcpLaunchSpec(argv=("/usr/bin/fake-agent", "--flag"), env={"K": "v"})
    asyncio.run(runtime.run(launch, _valid_request(tmp_path)))
    assert spawn_calls[0]["argv"] == ("/usr/bin/fake-agent", "--flag")
    assert dict(spawn_calls[0]["env"]) == {"K": "v"}


# ---------------- permissions ----------------


async def _prompt_with_permission(conn, *, options=None, session_id=None):
    if options is None:
        options = [
            acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
            acp.schema.PermissionOption(option_id="allow-always", name="Allow always", kind="allow_always"),
        ]
    response = await conn.client.request_permission(
        session_id=session_id if session_id is not None else conn.session_id,
        tool_call=acp.schema.ToolCallUpdate(tool_call_id="tc-1", title="Do a thing"),
        options=options,
    )
    return response


def test_permission_request_resolves_cancelled(tmp_path):
    captured = {}

    async def behavior(conn):
        captured["response"] = await _prompt_with_permission(conn)
        return _PromptResult()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert captured["response"].outcome.outcome == "cancelled"


def test_permission_first_option_not_selected(tmp_path):
    captured = {}

    async def behavior(conn):
        captured["response"] = await _prompt_with_permission(
            conn,
            options=[acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once")],
        )
        return _PromptResult()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert not hasattr(captured["response"].outcome, "option_id")
    assert captured["response"].outcome.outcome == "cancelled"


def test_permission_meta_ignored_for_policy(tmp_path):
    captured = {}

    async def behavior(conn):
        response = await conn.client.request_permission(
            session_id=conn.session_id,
            tool_call=acp.schema.ToolCallUpdate(
                tool_call_id="tc-1", title="Do a thing",
                field_meta={"provider_hint": "safe_to_allow"},
            ),
            options=[acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once")],
        )
        captured["response"] = response
        return _PromptResult()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert captured["response"].outcome.outcome == "cancelled"


def test_permission_events_emitted_in_order(tmp_path):
    async def behavior(conn):
        await _prompt_with_permission(conn)
        return _PromptResult()

    sink = _RecordingSink()
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), event_sink=sink))
    kinds = [type(e) for e in sink.events]
    assert kinds == [AcpPermissionRequested, AcpPermissionResolved]
    assert sink.events[1].outcome == "cancelled"


def test_permission_request_count_tracked(tmp_path):
    async def behavior(conn):
        await _prompt_with_permission(conn)
        await _prompt_with_permission(conn)
        return _PromptResult()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.permission_request_count == 2


def test_foreign_session_permission_request_aborts_without_events(tmp_path):
    async def behavior(conn):
        await _prompt_with_permission(conn, session_id="some-other-session")
        await asyncio.Event().wait()

    sink = _RecordingSink()
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits, event_sink=sink))
    assert sink.events == []


def test_permission_request_event_and_resolved_event_never_incremented_on_foreign_session(tmp_path):
    async def behavior(conn):
        await _prompt_with_permission(conn, session_id="foreign")
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


def test_permission_sink_failure_on_first_event_prevents_second(tmp_path):
    class FirstFailsSink:
        def __init__(self):
            self.calls = []

        def emit(self, event):
            self.calls.append(event)
            if len(self.calls) == 1:
                raise RuntimeError("sink exploded on first event")

    sink = FirstFailsSink()

    async def behavior(conn):
        await _prompt_with_permission(conn)
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpEventSinkError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits, event_sink=sink))
    assert len(sink.calls) == 1
    assert isinstance(sink.calls[0], AcpPermissionRequested)


def test_permission_sink_failure_preserves_cause(tmp_path):
    class ExplodingSink:
        def emit(self, event):
            raise RuntimeError("sink boom")

    async def behavior(conn):
        await _prompt_with_permission(conn)
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpEventSinkError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits, event_sink=ExplodingSink()))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "sink boom"


def test_permission_count_not_incremented_when_request_event_sink_fails(tmp_path):
    class ExplodingSink:
        def emit(self, event):
            raise RuntimeError("boom")

    captured_client = {}

    async def behavior(conn):
        captured_client["client"] = conn.client
        await _prompt_with_permission(conn)
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpEventSinkError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits, event_sink=ExplodingSink()))
    assert captured_client["client"].permission_request_count == 0


# ---------------- update events + bounds ----------------


def test_update_event_emitted(tmp_path):
    sink = _RecordingSink()
    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), event_sink=sink))
    assert len(sink.events) == 1
    assert isinstance(sink.events[0], AcpSessionUpdateObserved)


def test_update_count_tracked(tmp_path):
    async def behavior(conn):
        for _ in range(5):
            await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update())
        return _PromptResult()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.update_count == 5


def test_per_update_size_limit_aborts(tmp_path):
    async def behavior(conn):
        await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x" * 1000))
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(max_update_chars=10, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


def test_update_count_limit_aborts(tmp_path):
    async def behavior(conn):
        for _ in range(10):
            await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x"))
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(max_updates=3, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


def test_total_update_chars_limit_aborts(tmp_path):
    async def behavior(conn):
        for _ in range(10):
            await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x" * 50))
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(max_total_update_chars=200, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


def test_limit_breach_sends_cancel(tmp_path):
    async def behavior(conn):
        await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x" * 1000))
        await asyncio.Event().wait()

    conn_holder = {}

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=behavior)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    limits = AcpClientLimits(max_update_chars=10, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
    assert conn_holder["conn"].cancel_called is True


def test_foreign_session_update_aborts(tmp_path):
    async def behavior(conn):
        await conn.client.session_update(session_id="some-other-session", update=_agent_message_update())
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


def test_event_sink_exception_aborts(tmp_path):
    class ExplodingSink:
        def emit(self, event):
            raise RuntimeError("sink is broken")

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)))
    limits = AcpClientLimits(prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpEventSinkError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits, event_sink=ExplodingSink()))


def test_event_sink_exception_preserves_cause(tmp_path):
    class ExplodingSink:
        def emit(self, event):
            raise RuntimeError("update sink boom")

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)))
    with pytest.raises(AcpEventSinkError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), event_sink=ExplodingSink()))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "update sink boom"


def test_no_transcript_accumulation(tmp_path):
    async def behavior(conn):
        for index in range(50):
            await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update(f"chunk-{index}"))
        return _PromptResult()

    captured_client = {}

    def build(client):
        captured_client["client"] = client
        return _FakeConnection(client, prompt_behavior=behavior)

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    client = captured_client["client"]
    for attr_name in dir(client):
        if attr_name.startswith("_"):
            continue
        value = getattr(client, attr_name)
        if isinstance(value, (list, tuple)):
            assert len(value) == 0, f"{attr_name} unexpectedly retained {len(value)} items"


# ---------------- simultaneous fatal + prompt success race ----------------


def test_simultaneous_prompt_success_and_fatal_state_always_returns_fatal(tmp_path):
    async def behavior(conn):
        # Triggers a limit-breach fatal synchronously (no intervening
        # await inside the limit-breach path of session_update), then
        # returns a *successful* PromptResult in the very same tick, so
        # prompt_task and fatal_task become ready in the same scheduling
        # window. The implementation must still treat this as fatal.
        await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x" * 1000))
        return _PromptResult(stop_reason="end_turn")

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior)))
    limits = AcpClientLimits(max_update_chars=10, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))


# ---------------- auth / protocol errors ----------------


def test_auth_required_maps_to_specific_error(tmp_path):
    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, initialize_error=acp.RequestError.auth_required()))
    )
    with pytest.raises(AcpAuthenticationRequiredError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))


def test_other_request_error_maps_to_protocol_error(tmp_path):
    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, new_session_error=acp.RequestError.internal_error()))
    )
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))


def test_no_auth_retry_no_authenticate_call(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, initialize_error=acp.RequestError.auth_required())
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    with pytest.raises(AcpAuthenticationRequiredError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert len(conns) == 1
    assert not hasattr(conns[0], "authenticate_called")
    initialize_calls = [c for c in conns[0].calls if c[0] == "initialize"]
    assert len(initialize_calls) == 1


def test_unexpected_initialize_exception_maps_to_protocol_error_and_cleans_up(tmp_path):
    cleanup_calls = []

    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, initialize_error=ConnectionError("connection died")), pid=7001,
        ),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    with pytest.raises(AcpProtocolError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, ConnectionError)
    assert cleanup_calls == [7001]


def test_unexpected_new_session_exception_maps_to_protocol_error_and_cleans_up(tmp_path):
    cleanup_calls = []

    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, new_session_error=RuntimeError("schema broke")), pid=7002,
        ),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    with pytest.raises(AcpProtocolError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert cleanup_calls == [7002]


def test_unexpected_prompt_exception_maps_to_protocol_error_and_cleans_up(tmp_path):
    cleanup_calls = []

    async def behavior(conn):
        raise RuntimeError("prompt exploded")

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=behavior), pid=7003),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    with pytest.raises(AcpProtocolError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert cleanup_calls == [7003]


# ---------------- spawn failure ----------------


def test_os_spawn_error_maps_to_acp_spawn_error(tmp_path):
    async def _connect(client, argv, env, cwd):
        raise OSError("no such file or directory")

    runtime = AcpClientRuntime(_connect=_connect)
    from acp_runtime.errors import AcpSpawnError

    with pytest.raises(AcpSpawnError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, OSError)


def test_spawn_failure_does_not_attempt_cleanup(tmp_path):
    cleanup_calls = []

    async def _connect(client, argv, env, cwd):
        raise OSError("nope")

    runtime = AcpClientRuntime(_connect=_connect, _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid))
    from acp_runtime.errors import AcpSpawnError

    with pytest.raises(AcpSpawnError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert cleanup_calls == []


# ---------------- timeout / cancel ----------------


def test_prompt_timeout_sends_cancel_and_raises(tmp_path):
    conn_holder = {}

    async def hang(conn):
        await asyncio.Event().wait()

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=hang)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    limits = AcpClientLimits(prompt_timeout_ms=100, cancel_grace_ms=100)
    with pytest.raises(AcpTimeoutError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
    assert conn_holder["conn"].cancel_called is True


def test_timeout_after_session_creation_attempts_capability_gated_close(tmp_path):
    conn_holder = {}

    async def hang(conn):
        await asyncio.Event().wait()

    def build(client):
        conn = _FakeConnection(client, session_close_supported=True, prompt_behavior=hang)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    limits = AcpClientLimits(prompt_timeout_ms=100, cancel_grace_ms=100)
    with pytest.raises(AcpTimeoutError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
    assert conn_holder["conn"].close_session_called is True
    assert conn_holder["conn"].close_called is True


def test_fatal_update_after_session_creation_attempts_capability_gated_close(tmp_path):
    conn_holder = {}

    async def behavior(conn):
        await conn.client.session_update(session_id=conn.session_id, update=_agent_message_update("x" * 1000))
        await asyncio.Event().wait()

    def build(client):
        conn = _FakeConnection(client, session_close_supported=True, prompt_behavior=behavior)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    limits = AcpClientLimits(max_update_chars=10, prompt_timeout_ms=5000, cancel_grace_ms=200)
    with pytest.raises(AcpLimitError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
    assert conn_holder["conn"].close_session_called is True
    assert conn_holder["conn"].close_called is True


def test_cancel_wait_is_bounded(tmp_path):
    async def hang(conn):
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(_connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=hang)))
    limits = AcpClientLimits(prompt_timeout_ms=100, cancel_grace_ms=100)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop_start = loop.time()
        with pytest.raises(AcpTimeoutError):
            loop.run_until_complete(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        elapsed = loop.time() - loop_start
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert elapsed < 2.0


def test_hanging_cancel_send_does_not_block_teardown(tmp_path):
    """Hardening round 2, Blocker 2 defect A: conn.cancel(...) itself must
    be bounded. A connection whose cancel() never returns must not prevent
    AcpClientRuntime from reaching its typed timeout result and tearing down
    the connection/process within a deterministic bound."""

    async def hang_prompt(conn):
        await asyncio.Event().wait()

    class _HangingCancelConnection(_FakeConnection):
        async def cancel(self, session_id, **kwargs):
            self.cancel_called = True
            await asyncio.Event().wait()  # never returns

    cleanup_calls = []
    conn_holder = {}

    def build(client):
        conn = _HangingCancelConnection(client, prompt_behavior=hang_prompt)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(
        _connect=_make_connect(build, pid=31337),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    limits = AcpClientLimits(prompt_timeout_ms=200, cancel_grace_ms=200)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        started = loop.time()
        with pytest.raises(AcpTimeoutError):
            loop.run_until_complete(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        elapsed = loop.time() - started
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert elapsed < 5.0, f"hanging conn.cancel() blocked teardown for {elapsed}s"
    assert conn_holder["conn"].cancel_called is True
    assert conn_holder["conn"].close_called is True
    assert cleanup_calls == [31337]


def test_prompt_requiring_outer_close_does_not_deadlock_teardown(tmp_path):
    """Hardening round 2, Blocker 2 defect B: a prompt coroutine that
    survives local task cancellation and can only finish once the outer
    connection is closed must not be awaited unboundedly -- that would
    create a circular teardown dependency (the prompt waits for conn.close,
    but conn.close never runs until the prompt settles)."""

    class _GatedConnection(_FakeConnection):
        def __init__(self, client, **kwargs):
            super().__init__(client, **kwargs)
            self.release_event = asyncio.Event()

        async def close(self):
            self.close_called = True
            self.release_event.set()

    async def gated_prompt(conn):
        while not conn.release_event.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue  # survives local cancellation on purpose
        return _PromptResult(stop_reason="end_turn")

    conn_holder = {}
    cleanup_calls = []

    def build(client):
        conn = _GatedConnection(client, prompt_behavior=gated_prompt)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(
        _connect=_make_connect(build, pid=41414),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    limits = AcpClientLimits(prompt_timeout_ms=150, cancel_grace_ms=150)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        started = loop.time()
        with pytest.raises(AcpTimeoutError):
            loop.run_until_complete(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        elapsed = loop.time() - started
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert elapsed < 5.0, f"prompt requiring outer close deadlocked teardown for {elapsed}s"
    assert conn_holder["conn"].close_called is True
    assert cleanup_calls == [41414]


def test_final_owned_task_drain_reports_survivor_without_hanging(tmp_path):
    """A cancellation-resistant prompt must become an explicit cleanup
    failure at the final owned-task deadline, not hold run() forever."""

    async def scenario():
        release_prompt = asyncio.Event()
        prompt_finished = asyncio.Event()

        async def stubborn_prompt(conn):
            try:
                while not release_prompt.is_set():
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        continue
                return _PromptResult()
            finally:
                prompt_finished.set()

        conn_holder = {}
        cleanup_calls = []

        def build(client):
            conn = _FakeConnection(client, prompt_behavior=stubborn_prompt)
            conn_holder["conn"] = conn
            return conn

        runtime = AcpClientRuntime(
            _connect=_make_connect(build, pid=51515),
            _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
        )
        limits = AcpClientLimits(prompt_timeout_ms=50, cancel_grace_ms=50)
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        try:
            done, _ = await asyncio.wait({run_task}, timeout=1.0)
            assert done, "final owned-task drain did not return by its hard deadline"
            with pytest.raises(AcpCleanupError):
                await run_task
            assert conn_holder["conn"].close_called is True
            assert cleanup_calls == [51515]
        finally:
            release_prompt.set()
            if not run_task.done():
                with contextlib.suppress(BaseException):
                    await run_task
            await asyncio.wait_for(prompt_finished.wait(), timeout=1.0)

    asyncio.run(scenario())


def test_cancellation_resistant_cancel_send_cannot_block_teardown(tmp_path):
    """A cancellation-resistant session/cancel send is tracked and bounded;
    it cannot prevent connection close, hard cleanup, or final reaping."""

    async def scenario():
        release_cancel = asyncio.Event()
        cancel_finished = asyncio.Event()
        cancel_started = asyncio.Event()
        cancel_gate = asyncio.Event()

        async def hang_prompt(conn):
            await asyncio.Event().wait()

        class _CancellationResistantCancelConnection(_FakeConnection):
            async def cancel(self, session_id, **kwargs):
                self.cancel_called = True
                cancel_started.set()
                try:
                    while not release_cancel.is_set():
                        try:
                            await cancel_gate.wait()
                        except asyncio.CancelledError:
                            continue
                finally:
                    cancel_finished.set()

        conn_holder = {}
        cleanup_calls = []

        def build(client):
            conn = _CancellationResistantCancelConnection(client, prompt_behavior=hang_prompt)
            conn_holder["conn"] = conn
            return conn

        runtime = AcpClientRuntime(
            _connect=_make_connect(build, pid=61616),
            _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
        )
        limits = AcpClientLimits(prompt_timeout_ms=50, cancel_grace_ms=50)
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        try:
            await asyncio.wait_for(cancel_started.wait(), timeout=1.0)
            done, _ = await asyncio.wait({run_task}, timeout=1.0)
            assert done, "cancellation-resistant cancel send blocked teardown past its hard deadline"
            assert not cancel_finished.is_set(), "test cancel send settled before its release event"
            with pytest.raises((AcpTimeoutError, AcpLimitError, AcpCleanupError)):
                await run_task
            assert conn_holder["conn"].close_called is True
            assert cleanup_calls == [61616]
        finally:
            release_cancel.set()
            cancel_gate.set()
            if not run_task.done():
                with contextlib.suppress(BaseException):
                    await run_task
            await asyncio.wait_for(cancel_finished.wait(), timeout=1.0)

    asyncio.run(scenario())


def test_caller_cancellation_still_cleans_up_process_tree(tmp_path):
    cleanup_calls = []

    async def hang(conn):
        await asyncio.Event().wait()

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=hang), pid=8123),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    limits = AcpClientLimits(prompt_timeout_ms=30_000, cancel_grace_ms=200)

    async def scenario():
        task = asyncio.ensure_future(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cleanup_calls == [8123]


def test_cancellation_arriving_during_close_session_still_finishes_teardown(tmp_path):
    async def scenario():
        close_session_started = asyncio.Event()
        close_session_gate = asyncio.Event()
        drain_finished = asyncio.Event()
        cleanup_calls = []
        conn_holder = {}

        class _CloseSessionWaitConnection(_FakeConnection):
            async def close_session(self, session_id, **kwargs):
                self.close_session_called = True
                close_session_started.set()
                await close_session_gate.wait()

        def build(client):
            conn = _CloseSessionWaitConnection(client, prompt_behavior=_echo_prompt)
            conn_holder["conn"] = conn
            return conn

        class _RecordingRuntime(_AcpClientRuntime):
            async def _drain_owned_tasks(self, owned_tasks, limits):
                drained = await super()._drain_owned_tasks(owned_tasks, limits)
                drain_finished.set()
                return drained

        runtime = _RecordingRuntime(
            _connect=_make_connect(
                build, pid=71717,
            ),
            _capture_process_tree=lambda pid: None,
            _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
        )
        limits = AcpClientLimits(session_close_timeout_ms=50, cancel_grace_ms=50)
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path), limits=limits))
        await asyncio.wait_for(close_session_started.wait(), timeout=1.0)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        close_session_gate.set()
        assert conn_holder["conn"].close_called is True
        assert cleanup_calls == [71717]
        assert drain_finished.is_set()

    asyncio.run(scenario())


def test_cancellation_arriving_during_hard_cleanup_waits_for_cleanup_before_propagating(tmp_path):
    async def scenario():
        cleanup_started = threading.Event()
        cleanup_release = threading.Event()
        cleanup_finished = threading.Event()
        drain_finished = asyncio.Event()
        cleanup_calls = []

        def blocking_cleanup(pid, snapshot=None):
            cleanup_calls.append(pid)
            cleanup_started.set()
            cleanup_release.wait(timeout=2.0)
            cleanup_finished.set()

        class _RecordingRuntime(_AcpClientRuntime):
            async def _drain_owned_tasks(self, owned_tasks, limits):
                drained = await super()._drain_owned_tasks(owned_tasks, limits)
                drain_finished.set()
                return drained

        runtime = _RecordingRuntime(
            _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt), pid=81818),
            _capture_process_tree=lambda pid: None,
            _terminate_process_tree=blocking_cleanup,
        )
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path)))
        while not cleanup_started.is_set():
            await asyncio.sleep(0)
        run_task.cancel()
        await asyncio.sleep(0)
        assert not run_task.done(), "caller cancellation returned before hard cleanup finished"
        assert not cleanup_finished.is_set()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert cleanup_finished.is_set()
        assert cleanup_calls == [81818]
        assert drain_finished.is_set()

    asyncio.run(scenario())


def test_internal_close_session_cancelled_error_does_not_cancel_cleanup_task(tmp_path):
    async def scenario():
        drain_finished = asyncio.Event()
        cleanup_calls = []
        conn_holder = {}

        class _CancelledCloseSessionConnection(_FakeConnection):
            async def close_session(self, session_id, **kwargs):
                self.close_session_called = True
                raise asyncio.CancelledError

        def build(client):
            conn = _CancelledCloseSessionConnection(client, prompt_behavior=_echo_prompt)
            conn_holder["conn"] = conn
            return conn

        class _RecordingRuntime(_AcpClientRuntime):
            async def _drain_owned_tasks(self, owned_tasks, limits):
                drained = await super()._drain_owned_tasks(owned_tasks, limits)
                drain_finished.set()
                return drained

        runtime = _RecordingRuntime(
            _connect=_make_connect(build, pid=91919),
            _capture_process_tree=lambda pid: None,
            _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
        )
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path)))
        done, _ = await asyncio.wait({run_task}, timeout=1.0)
        assert done, "cancelled close_session stage cancelled cleanup and left shield loop spinning"
        result = run_task.result()
        assert result.session_close_succeeded is False
        assert conn_holder["conn"].close_called is True
        assert cleanup_calls == [91919]
        assert drain_finished.is_set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_internal_connection_close_cancelled_error_does_not_spin_or_skip_hard_cleanup(tmp_path):
    async def scenario():
        drain_finished = asyncio.Event()
        cleanup_calls = []
        conn_holder = {}

        class _CancelledConnectionCloseConnection(_FakeConnection):
            async def close(self):
                self.close_called = True
                raise asyncio.CancelledError

        def build(client):
            conn = _CancelledConnectionCloseConnection(client, prompt_behavior=_echo_prompt)
            conn_holder["conn"] = conn
            return conn

        class _RecordingRuntime(_AcpClientRuntime):
            async def _drain_owned_tasks(self, owned_tasks, limits):
                drained = await super()._drain_owned_tasks(owned_tasks, limits)
                drain_finished.set()
                return drained

        runtime = _RecordingRuntime(
            _connect=_make_connect(build, pid=92929),
            _capture_process_tree=lambda pid: None,
            _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
        )
        run_task = asyncio.create_task(runtime.run(_valid_launch(), _valid_request(tmp_path)))
        done, _ = await asyncio.wait({run_task}, timeout=1.0)
        assert done, "cancelled connection-close stage left the shield loop spinning"
        with pytest.raises(AcpProtocolError):
            run_task.result()
        assert conn_holder["conn"].close_called is True
        assert cleanup_calls == [92929]
        assert drain_finished.is_set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


# ---------------- session close ----------------


def test_close_not_called_when_capability_absent(tmp_path):
    conn_holder = {}

    def build(client):
        conn = _FakeConnection(client, session_close_supported=False, prompt_behavior=_echo_prompt)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.session_close_supported is False
    assert result.session_close_succeeded is None
    assert conn_holder["conn"].close_session_called is False


def test_close_attempted_when_capability_present(tmp_path):
    conn_holder = {}

    def build(client):
        conn = _FakeConnection(client, session_close_supported=True, prompt_behavior=_echo_prompt)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.session_close_supported is True
    assert result.session_close_succeeded is True
    assert conn_holder["conn"].close_session_called is True


def test_close_failure_does_not_falsify_successful_result(tmp_path):
    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(
                c, session_close_supported=True, prompt_behavior=_echo_prompt,
                close_session_error=RuntimeError("close failed"),
            )
        )
    )
    result = asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert result.session_close_supported is True
    assert result.session_close_succeeded is False
    assert result.stop_reason == "end_turn"


def test_no_close_attempt_when_no_session_created(tmp_path):
    conn_holder = {}

    def build(client):
        conn = _FakeConnection(client, session_close_supported=True, initialize_error=acp.RequestError.internal_error())
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert conn_holder["conn"].close_session_called is False


# ---------------- connection teardown ----------------


def test_connection_close_always_attempted_on_success(tmp_path):
    conn_holder = {}

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conn_holder["conn"] = conn
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert conn_holder["conn"].close_called is True


def test_connection_close_failure_still_runs_hard_cleanup(tmp_path):
    cleanup_calls = []

    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt, close_error=RuntimeError("close broke")),
            pid=9101,
        ),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert cleanup_calls == [9101]


def test_connection_close_failure_maps_to_protocol_error_when_no_primary_failure(tmp_path):
    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt, close_error=RuntimeError("close broke")),
        )
    )
    with pytest.raises(AcpProtocolError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_connection_close_failure_does_not_mask_earlier_primary_failure(tmp_path):
    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(
                c, new_session_error=acp.RequestError.auth_required(), close_error=RuntimeError("close broke"),
            )
        )
    )
    with pytest.raises(AcpAuthenticationRequiredError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))


# ---------------- no reuse / cleanup precedence ----------------


def test_no_connection_reuse_across_two_runs(tmp_path):
    conns = []

    def build(client):
        conn = _FakeConnection(client, prompt_behavior=_echo_prompt)
        conns.append(conn)
        return conn

    runtime = AcpClientRuntime(_connect=_make_connect(build))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert conns[0] is not conns[1]


def test_cleanup_called_on_success(tmp_path):
    cleanup_calls = []

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt), pid=9999),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert cleanup_calls == [9999]


def test_cleanup_called_on_protocol_failure(tmp_path):
    cleanup_calls = []

    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, new_session_error=acp.RequestError.internal_error()), pid=9999,
        ),
        _terminate_process_tree=lambda pid, snapshot=None: cleanup_calls.append(pid),
    )
    with pytest.raises(AcpProtocolError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert cleanup_calls == [9999]


def test_cleanup_survivor_raises_acp_cleanup_error(tmp_path):
    def fake_terminate(pid, snapshot=None):
        raise ProcessCleanupError("survivor left behind")

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)),
        _terminate_process_tree=fake_terminate,
    )
    with pytest.raises(AcpCleanupError):
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))


def test_cleanup_survivor_after_primary_failure_chains_cause(tmp_path):
    def fake_terminate(pid, snapshot=None):
        raise ProcessCleanupError("survivor left behind")

    runtime = AcpClientRuntime(
        _connect=_make_connect(
            lambda c: _FakeConnection(c, new_session_error=acp.RequestError.internal_error()),
        ),
        _terminate_process_tree=fake_terminate,
    )
    with pytest.raises(AcpCleanupError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert isinstance(excinfo.value.__cause__, AcpProtocolError)


def test_cleanup_survivor_alone_preserves_process_cleanup_error_cause(tmp_path):
    underlying = ProcessCleanupError("survivor left behind")

    def fake_terminate(pid, snapshot=None):
        raise underlying

    runtime = AcpClientRuntime(
        _connect=_make_connect(lambda c: _FakeConnection(c, prompt_behavior=_echo_prompt)),
        _terminate_process_tree=fake_terminate,
    )
    with pytest.raises(AcpCleanupError) as excinfo:
        asyncio.run(runtime.run(_valid_launch(), _valid_request(tmp_path)))
    assert excinfo.value.__cause__ is underlying


def test_no_run_sync_or_asyncio_run_inside_module():
    import acp_runtime.client as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "def run_sync" not in source
    assert "asyncio.run(" not in source


def test_client_module_never_calls_spawn_agent_process():
    import acp_runtime.client as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "spawn_agent_process" not in source
