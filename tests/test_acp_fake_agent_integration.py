"""Real official-SDK, real-subprocess integration tests for AcpClientRuntime,
driven against tests/fixtures/acp_fake_agent.py. See spec sections 41-46."""

import asyncio
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp_runtime.client import AcpClientRuntime  # noqa: E402
from acp_runtime.errors import AcpCleanupError, AcpProtocolError, AcpTimeoutError  # noqa: E402
from acp_runtime.events import AcpPermissionRequested, AcpPermissionResolved, AcpSessionUpdateObserved  # noqa: E402
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest  # noqa: E402

_FAKE_AGENT = str(Path(__file__).resolve().parent / "fixtures" / "acp_fake_agent.py")


def _launch(mode: str, *, env: dict[str, str] | None = None) -> AcpLaunchSpec:
    return AcpLaunchSpec(argv=(sys.executable, _FAKE_AGENT, mode), env=env or {})


class _RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_echo_mode_end_to_end(tmp_path):
    runtime = AcpClientRuntime()
    result = asyncio.run(
        runtime.run(
            _launch("echo"),
            AcpPromptRequest(cwd=str(tmp_path), prompt="hello there"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
        )
    )
    assert result.session_id == "fake-session-1"
    assert result.stop_reason == "end_turn"
    assert result.update_count == 1
    assert result.update_chars > 0
    assert result.permission_request_count == 0
    assert result.session_close_supported is True
    assert result.session_close_succeeded is True


def test_permission_mode_resolves_cancelled_despite_offered_options(tmp_path):
    sink = _RecordingSink()
    runtime = AcpClientRuntime()
    result = asyncio.run(
        runtime.run(
            _launch("permission"),
            AcpPromptRequest(cwd=str(tmp_path), prompt="do something risky"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
            event_sink=sink,
        )
    )
    assert result.permission_request_count == 1
    requested = [e for e in sink.events if isinstance(e, AcpPermissionRequested)]
    resolved = [e for e in sink.events if isinstance(e, AcpPermissionResolved)]
    assert len(requested) == 1
    assert set(requested[0].option_ids) == {"allow-once", "allow-always"}
    assert len(resolved) == 1
    assert resolved[0].outcome == "cancelled"
    # Prompt still completed normally afterward.
    assert result.stop_reason == "end_turn"


def test_hang_mode_times_out_and_root_process_is_gone(tmp_path):
    runtime = AcpClientRuntime()
    limits = AcpClientLimits(prompt_timeout_ms=1500, cancel_grace_ms=500)

    pids_before = {p.pid for p in psutil.process_iter()}
    with pytest.raises(AcpTimeoutError):
        asyncio.run(
            runtime.run(
                _launch("hang"),
                AcpPromptRequest(cwd=str(tmp_path), prompt="hang please"),
                limits=limits,
            )
        )
    # No new fake_agent.py process should remain alive.
    time.sleep(0.2)
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        assert not any("acp_fake_agent.py" in part for part in cmdline), f"leaked process: {proc.info}"


def test_child_process_mode_descendant_cleanup(tmp_path):
    pid_file = tmp_path / "child.pid"
    runtime = AcpClientRuntime()
    child_pid = None
    try:
        result = asyncio.run(
            runtime.run(
                _launch("child_process", env={"ACP_FAKE_AGENT_CHILD_PID_FILE": str(pid_file)}),
                AcpPromptRequest(cwd=str(tmp_path), prompt="spawn a child"),
                limits=AcpClientLimits(prompt_timeout_ms=10_000),
            )
        )
        assert result.stop_reason == "end_turn"
        assert pid_file.exists()
        child_pid = int(pid_file.read_text().strip())
        assert not psutil.pid_exists(child_pid)
    finally:
        if child_pid is not None and psutil.pid_exists(child_pid):
            with_kill = psutil.Process(child_pid)
            with_kill.kill()
            with_kill.wait(timeout=2)


def test_no_fs_terminal_elicitation_capability_advertised_over_real_connection(tmp_path):
    from acp_runtime.stdio import spawn_acp_agent_connection

    captured = {}

    async def _spy_connect(client, argv, env, cwd):
        conn, process = await spawn_acp_agent_connection(client, argv, env, cwd)
        real_initialize = conn.initialize

        async def _spy_initialize(protocol_version, client_capabilities=None, client_info=None, **kwargs):
            captured["client_capabilities"] = client_capabilities
            return await real_initialize(
                protocol_version, client_capabilities=client_capabilities, client_info=client_info, **kwargs
            )

        conn.initialize = _spy_initialize
        return conn, process

    runtime = AcpClientRuntime(_connect=_spy_connect)
    asyncio.run(
        runtime.run(
            _launch("echo"),
            AcpPromptRequest(cwd=str(tmp_path), prompt="hi"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
        )
    )
    assert captured["client_capabilities"] is None


def test_exact_child_environment_host_var_not_inherited_unless_supplied(tmp_path):
    """The official SDK's own spawn_agent_process/spawn_stdio_transport would
    normally inherit LOGNAME (and other DEFAULT_INHERITED_ENV_VARS) from the
    host even when AcpLaunchSpec.env omits it. acp_runtime must not: this
    test would FAIL against that old spawn_agent_process-based
    implementation, and only passes now that acp_runtime owns subprocess
    creation directly (acp_runtime/stdio.py). LOGNAME (rather than HOME) is
    probed here because HOME also drives the child's own Python user-site
    resolution -- overriding it would break the fake agent's ability to
    import `acp` at all, which is a orthogonal concern to this test."""
    sentinel_file = tmp_path / "sentinel_seen.txt"
    marker = "acp-hardening-sentinel-3f9c9b1e"
    old_logname = os.environ.get("LOGNAME")
    os.environ["LOGNAME"] = marker
    try:
        runtime = AcpClientRuntime()
        result = asyncio.run(
            runtime.run(
                _launch(
                    "env_probe",
                    env={"ACP_FAKE_AGENT_SENTINEL_FILE": str(sentinel_file)},
                ),
                AcpPromptRequest(cwd=str(tmp_path), prompt="probe env"),
                limits=AcpClientLimits(prompt_timeout_ms=10_000),
            )
        )
    finally:
        if old_logname is None:
            os.environ.pop("LOGNAME", None)
        else:
            os.environ["LOGNAME"] = old_logname

    assert result.stop_reason == "end_turn"
    assert sentinel_file.exists()
    reported = sentinel_file.read_text(encoding="utf-8")
    assert marker not in reported, (
        f"host LOGNAME leaked into ACP child despite being absent from AcpLaunchSpec.env: {reported!r}"
    )
    assert reported.strip() == "LOGNAME=<absent>"


def test_exact_child_environment_supplied_value_reaches_child_exactly(tmp_path):
    sentinel_file = tmp_path / "sentinel_seen.txt"
    marker = "acp-hardening-exact-value-7a1d2c"
    runtime = AcpClientRuntime()
    result = asyncio.run(
        runtime.run(
            _launch(
                "env_probe",
                env={"ACP_FAKE_AGENT_SENTINEL_FILE": str(sentinel_file), "LOGNAME": marker},
            ),
            AcpPromptRequest(cwd=str(tmp_path), prompt="probe env"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
        )
    )
    assert result.stop_reason == "end_turn"
    reported = sentinel_file.read_text(encoding="utf-8")
    assert reported.strip() == f"LOGNAME={marker}"


def test_no_transcript_accumulation_over_real_connection(tmp_path):
    sink = _RecordingSink()
    runtime = AcpClientRuntime()
    result = asyncio.run(
        runtime.run(
            _launch("many_updates", env={"ACP_FAKE_AGENT_UPDATE_COUNT": "40"}),
            AcpPromptRequest(cwd=str(tmp_path), prompt="send many updates"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000),
        )
    )
    assert result.update_count == 40
    # AcpClientRuntime/AcpRunResult hold no transcript, only counters.
    for field_name in ("session_id", "stop_reason", "update_count", "update_chars", "permission_request_count", "session_close_supported", "session_close_succeeded"):
        assert hasattr(result, field_name)
    assert not hasattr(result, "updates")
    assert not hasattr(result, "transcript")


# ---------------- Hardening Round 2 ----------------


def _no_process_with_marker_alive(marker: str) -> bool:
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(marker in part for part in cmdline):
            return False
    return True


def test_connection_construction_failure_rolls_back_real_process(tmp_path, monkeypatch):
    """Blocker 3: acp.connect_to_agent raising AFTER a real OS process was
    already created must not leak that process. Only acp.connect_to_agent is
    monkeypatched -- subprocess creation itself is real."""
    import acp as acp_sdk

    class _Sentinel(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _Sentinel("connect_to_agent exploded")

    monkeypatch.setattr(acp_sdk, "connect_to_agent", _boom)

    marker = "acp-h2-blocker3-marker-9f21c4"
    runtime = AcpClientRuntime()
    launch = AcpLaunchSpec(argv=(sys.executable, "-c", f"import time; time.sleep(30)  # {marker}"))
    request = AcpPromptRequest(cwd=str(tmp_path), prompt="hi")

    with pytest.raises(AcpProtocolError) as excinfo:
        asyncio.run(runtime.run(launch, request))
    assert isinstance(excinfo.value.__cause__, _Sentinel)

    deadline = time.monotonic() + 5
    while not _no_process_with_marker_alive(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _no_process_with_marker_alive(marker), "connect_to_agent failure leaked the real child process"


def test_snapshot_before_close_catches_descendant_orphaned_by_root_exit(tmp_path, monkeypatch):
    """Blocker 1: force the real ACP root process to fully exit (via the
    real production close_acp_agent_connection, which sends stdin EOF) BEFORE
    hard process-tree cleanup runs, using the child_process fake-agent mode.
    A detached grandchild that existed before root exit must still be
    reaped even though it is no longer discoverable from the (now dead)
    root pid by the time cleanup runs."""
    import acp_runtime.client as client_module
    from acp_runtime.stdio import close_acp_agent_connection as real_close_acp_agent_connection

    async def _close_and_wait_for_real_exit(conn, process):
        await real_close_acp_agent_connection(conn, process)
        await asyncio.wait_for(process.wait(), timeout=5)

    monkeypatch.setattr(client_module, "close_acp_agent_connection", _close_and_wait_for_real_exit)

    pid_file = tmp_path / "child.pid"
    runtime = AcpClientRuntime()
    result = asyncio.run(
        runtime.run(
            _launch("child_process", env={"ACP_FAKE_AGENT_CHILD_PID_FILE": str(pid_file)}),
            AcpPromptRequest(cwd=str(tmp_path), prompt="spawn a child"),
            limits=AcpClientLimits(prompt_timeout_ms=10_000, session_close_timeout_ms=8_000),
        )
    )
    assert result.stop_reason == "end_turn"
    assert pid_file.exists()
    child_pid = int(pid_file.read_text().strip())

    deadline = time.monotonic() + 3
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid), (
        "descendant orphaned by real root exit survived cleanup -- snapshot must be captured "
        "before conn.close()/close_session, not only rescanned afterward"
    )
