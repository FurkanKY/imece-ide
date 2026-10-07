import io
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime import ProcessInputError, ProcessRequest, ProcessResult, ProcessRunner  # noqa: E402
from process_runtime.capture import BoundedCapture, CAPTURE_LIMIT  # noqa: E402
from process_runtime.errors import ProcessCancelledError, ProcessSpawnError  # noqa: E402
from agent_runtime.cancellation import CancellationToken  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


def py(*args):
    return (sys.executable, "-c", *args)


@pytest.fixture
def workspace(tmp_path):
    return LocalWorkspace(tmp_path)


def test_success_nonzero_and_separate_streams(workspace):
    runner = ProcessRunner()
    success = runner.run(workspace, ProcessRequest(py("print('hello')")))
    assert success.exit_code == 0
    assert success.timed_out is False
    assert "hello" in success.stdout
    assert success.stderr == ""
    assert success.duration_ms >= 0
    if sys.platform.startswith("linux"):
        assert success.producer_quiescent is True

    nonzero = runner.run(
        workspace,
        ProcessRequest(py("import sys; print('bad'); print('err', file=sys.stderr); sys.exit(7)")),
    )
    assert nonzero.exit_code == 7
    assert nonzero.timed_out is False
    assert "bad" in nonzero.stdout
    assert "err" in nonzero.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper supervisor")
def test_supervisor_preserves_negative_signal_returncode(workspace):
    import signal
    result = ProcessRunner().run(workspace, ProcessRequest(py(
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    )))
    assert result.exit_code == -signal.SIGTERM


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Object integration")
def test_windows_job_contains_detached_child_and_proves_drain(workspace):
    import subprocess
    child = "import time; time.sleep(1)"
    root = ("import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}], "
            "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)")
    started = time.monotonic()
    result = ProcessRunner().run(workspace, ProcessRequest(py(root), timeout_ms=5000))
    assert result.exit_code == 0 and result.producer_quiescent
    assert time.monotonic() - started >= .7


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Object cancellation")
def test_windows_job_cancellation_kills_root_and_detached_descendants(workspace):
    import threading
    token = CancellationToken()
    script = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
              "'import time; time.sleep(30)'], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | "
              "subprocess.DETACHED_PROCESS)")
    timer = threading.Timer(.7, token.cancel)
    timer.start()
    try:
        with pytest.raises(ProcessCancelledError) as exc:
            ProcessRunner().run(workspace, ProcessRequest(py(script), timeout_ms=5000), cancel_token=token)
        assert exc.value.producer_quiescent is True
    finally:
        timer.cancel()


def test_windows_receipt_reader_enforces_small_bound(tmp_path):
    from process_runtime.errors import ProcessCleanupError
    from process_runtime.runner import _read_windows_receipt
    path = tmp_path / "receipt.json"
    path.write_bytes(b"{}")
    assert _read_windows_receipt(path) == b"{}"
    path.write_bytes(b"x" * 1025)
    with pytest.raises(ProcessCleanupError, match="size bound"):
        _read_windows_receipt(path)


def test_supervisor_receipt_rejects_tampering():
    import json
    from process_runtime.errors import ProcessCleanupError
    from process_runtime.runner import _validated_receipt
    nonce = "a" * 64
    valid = json.dumps({"nonce": nonce, "exit_code": 7, "quiescent": True}).encode()
    assert _validated_receipt(valid, nonce, 0)["exit_code"] == 7
    win_receipt = json.dumps({"nonce": nonce, "exit_code": 1, "quiescent": True,
                              "cancelled": False}).encode()
    assert _validated_receipt(win_receipt, nonce, 0)["cancelled"] is False
    forged_cancel = json.dumps({"nonce": nonce, "exit_code": 1, "quiescent": True,
                                "cancelled": 1}).encode()
    with pytest.raises(ProcessCleanupError):
        _validated_receipt(forged_cancel, nonce, 0)
    forged = json.dumps({"nonce": "b" * 64, "exit_code": 0, "quiescent": True}).encode()
    with pytest.raises(ProcessCleanupError):
        _validated_receipt(forged, nonce, 0)
    with pytest.raises(ProcessCleanupError):
        _validated_receipt(valid, nonce, 1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper supervisor")
def test_acp_relay_large_bidirectional_payload_and_final_input_before_eof():
    import json
    import subprocess
    config_read, config_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    os.set_inheritable(config_read, True)
    os.set_inheritable(receipt_write, True)
    nonce = "c" * 64
    script = Path(__file__).resolve().parents[1] / "process_runtime" / "supervisor.py"
    command = (sys.executable, "-c", "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)")
    payload = json.dumps({"argv": command, "cwd": os.getcwd(), "env": dict(os.environ), "stdio": "acp"}).encode() + b"\n"
    try:
        proc = subprocess.Popen([sys.executable, str(script), str(config_read), str(receipt_write), nonce],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(config_read, receipt_write))
        os.close(config_read); config_read = -1
        os.close(receipt_write); receipt_write = -1
        os.write(config_write, payload); os.close(config_write); config_write = -1
        message = (b"large-acp-frame:" + b"x" * (2 * 1024 * 1024) + b":final-frame")
        writer_error = []
        def writer():
            try:
                proc.stdin.write(message); proc.stdin.close()
            except Exception as exc:
                writer_error.append(exc)
        import threading
        thread = threading.Thread(target=writer)
        thread.start()
        received = bytearray()
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk: break
            received.extend(chunk)
        thread.join(timeout=5)
        assert not thread.is_alive() and not writer_error
        assert bytes(received) == message
        assert proc.wait(timeout=5) == 0
        receipt = os.read(receipt_read, 1024)
        assert json.loads(receipt) == {"nonce": nonce, "exit_code": 0, "quiescent": True}
    finally:
        for fd in (config_read, config_write, receipt_read, receipt_write):
            if fd >= 0:
                try: os.close(fd)
                except OSError: pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper supervisor")
def test_acp_relay_delivers_output_eof_when_agent_exits_before_parent_stdin():
    import json
    import subprocess
    config_read, config_write = os.pipe()
    receipt_read, receipt_write = os.pipe()
    os.set_inheritable(config_read, True); os.set_inheritable(receipt_write, True)
    nonce = "d" * 64
    script = Path(__file__).resolve().parents[1] / "process_runtime" / "supervisor.py"
    command = (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'agent-exited')")
    payload = json.dumps({"argv": command, "cwd": os.getcwd(), "env": dict(os.environ), "stdio": "acp"}).encode() + b"\n"
    proc = None
    try:
        proc = subprocess.Popen([sys.executable, str(script), str(config_read), str(receipt_write), nonce],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(config_read, receipt_write))
        os.close(config_read); config_read = -1
        os.close(receipt_write); receipt_write = -1
        os.write(config_write, payload); os.close(config_write); config_write = -1
        assert proc.stdout.read() == b"agent-exited"
        # Parent stdin deliberately remains open: root stdout EOF is sufficient
        # to close the protocol stream and let the host observe agent death.
        assert proc.wait(timeout=5) == 0
        assert json.loads(os.read(receipt_read, 1024)) == {"nonce": nonce, "exit_code": 0, "quiescent": True}
    finally:
        if proc is not None:
            if proc.stdin and not proc.stdin.closed: proc.stdin.close()
        for fd in (config_read, config_write, receipt_read, receipt_write):
            if fd >= 0:
                try: os.close(fd)
                except OSError: pass


def test_reap_after_root_exit_never_signals_reusable_process_group(monkeypatch):
    import process_runtime.supervisor as supervisor

    calls = iter([(4242, 0), None])
    def waitpid(_pid, _options):
        result = next(calls)
        if result is None:
            raise ChildProcessError()
        return result
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(supervisor, "_kill_children", lambda: None)
    monkeypatch.setattr(supervisor.os, "killpg", lambda *_args: pytest.fail("stale process group was signaled"))
    monkeypatch.setattr(supervisor, "_shutdown", True)
    status, complete = supervisor._reap_all(4242)
    assert status == 0 and complete


def test_subreaper_children_inspection_failure_is_not_empty_tree(monkeypatch):
    import builtins
    from process_runtime.supervisor import _children
    real_open = builtins.open
    def fail(path, *args, **kwargs):
        if str(path).endswith("/children"):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", fail)
    with pytest.raises(RuntimeError, match="quiescence is unproven"):
        _children()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper supervisor")
def test_config_pipe_broken_pipe_reaps_started_supervisor(monkeypatch, workspace):
    import process_runtime.runner as runner
    real_fdopen = runner.os.fdopen
    spawned = []
    real_popen = runner.subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def broken_config_pipe(fd, mode="r", *args, **kwargs):
        if mode == "wb":
            raise BrokenPipeError("simulated config pipe failure")
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(runner.os, "fdopen", broken_config_pipe)
    with pytest.raises(ProcessSpawnError):
        ProcessRunner().run(workspace, ProcessRequest(py("raise SystemExit('must not run')")))
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_supervisor_failure_does_not_produce_receipt():
    import os
    import subprocess
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    script = Path(__file__).resolve().parents[1] / "process_runtime" / "supervisor.py"
    try:
        child = subprocess.run([sys.executable, str(script), str(write_fd), "a" * 64],
                               input=b"not-json\\n", pass_fds=(write_fd,), capture_output=True)
        os.close(write_fd)
        assert child.returncode != 0
        assert os.read(read_fd, 1024) == b""
    finally:
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_subreaper_waits_for_detached_descendant_before_receipt(workspace, tmp_path):
    child_pid_file = tmp_path / "detached.pid"
    code = (
        "import pathlib, subprocess, sys; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.35)'], "
        "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); print('parent done')"
    )
    started = time.monotonic()
    result = ProcessRunner().run(workspace, ProcessRequest(py(code, str(child_pid_file))))
    assert result.exit_code == 0 and result.producer_quiescent is True
    assert "parent done" in result.stdout
    assert time.monotonic() - started >= .25
    child_pid = int(child_pid_file.read_text())
    assert not psutil.pid_exists(child_pid)


def test_large_output_is_drained_and_bounded(workspace):
    result = ProcessRunner().run(
        workspace,
        ProcessRequest(py(
            "import sys; sys.stdout.write('A'*200000); sys.stderr.write('B'*200000)"
        )),
    )
    assert result.exit_code == 0
    assert result.stdout_bytes == 200000
    assert result.stderr_bytes == 200000
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert len(result.stdout.encode("utf-8")) <= CAPTURE_LIMIT + 80
    assert len(result.stderr.encode("utf-8")) <= CAPTURE_LIMIT + 80
    assert result.stdout.startswith("A") and result.stdout.endswith("A")
    assert result.stderr.startswith("B") and result.stderr.endswith("B")


def test_timeout_terminates_process_tree(workspace, tmp_path):
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    deadline = time.monotonic() + 2
    result = ProcessRunner().run(
        workspace,
        ProcessRequest(py(code, str(child_pid_file)), timeout_ms=500),
    )
    assert result.timed_out is True
    assert result.exit_code is not None
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not psutil.pid_exists(child_pid)


def test_cancel_token_terminates_process_tree(workspace, tmp_path):
    """Cancellation authenticates quiescence after reaping a detached child."""
    child_pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    token = CancellationToken()

    import threading

    def _cancel_soon():
        deadline = time.monotonic() + 2
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        token.cancel()

    canceller = threading.Thread(target=_cancel_soon)
    canceller.start()
    try:
        with pytest.raises(ProcessCancelledError) as cancelled:
            ProcessRunner().run(
                workspace,
                ProcessRequest(py(code, str(child_pid_file)), timeout_ms=30_000),
                cancel_token=token,
            )
        assert cancelled.value.producer_quiescent is True
    finally:
        canceller.join(timeout=5)

    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not psutil.pid_exists(child_pid)


def test_cancel_token_never_cancelled_runs_normally(workspace):
    token = CancellationToken()
    result = ProcessRunner().run(workspace, ProcessRequest(py("print('hi')")), cancel_token=token)
    assert result.exit_code == 0
    assert "hi" in result.stdout


def test_safe_environment_filters_parent_secret_and_allows_explicit_value(workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "supersecret-imece-test")
    code = "import os; print(os.getenv('OPENAI_API_KEY', 'ABSENT')); print(os.getenv('IMECE_TEST_VALUE', 'MISSING'))"
    result = ProcessRunner().run(
        workspace,
        ProcessRequest(py(code), env={"IMECE_TEST_VALUE": "present"}),
    )
    assert "supersecret-imece-test" not in result.stdout
    assert "ABSENT" in result.stdout
    assert "present" in result.stdout


@pytest.mark.parametrize("cwd", [
    "", "..", "../x", "a/../b", "/etc", "C:\\foo", "C:foo", "D:dir/file.txt",
    "\\\\server\\share", "~", "$HOME/x", "bad\x00path",
])
def test_process_request_rejects_unsafe_cwd(cwd):
    with pytest.raises(ProcessInputError):
        ProcessRequest(py("print('no')"), cwd=cwd)


def test_cwd_is_workspace_relative_and_symlink_directory_rejected(workspace, tmp_path):
    (tmp_path / "subdir").mkdir()
    result = ProcessRunner().run(
        workspace,
        ProcessRequest(py("import os; print(os.getcwd())"), cwd="subdir"),
    )
    assert Path(result.stdout.strip()) == tmp_path / "subdir"
    if hasattr(os, "symlink"):
        outside = tmp_path.parent / "process-outside"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
        with pytest.raises(ProcessSpawnError):
            ProcessRunner().run(workspace, ProcessRequest(py("print('no')"), cwd="link"))


def test_request_is_immutable_and_protects_core_environment():
    env = {"SAFE": "before"}
    request = ProcessRequest((sys.executable, "-c", "pass"), env=env)
    env["SAFE"] = "after"
    assert request.env["SAFE"] == "before"
    with pytest.raises(ProcessInputError):
        ProcessRequest((sys.executable,), env={"PATH": "bad"})
    with pytest.raises(ProcessInputError):
        ProcessRequest((sys.executable,), timeout_ms=True)


def test_process_request_requires_nonempty_executable():
    with pytest.raises(ProcessInputError):
        ProcessRequest(("", "valid-argument"))


def test_process_result_validates_and_defensively_copies_contract():
    argv = [sys.executable, "-c", "print('ok')"]
    result = ProcessResult(
        argv=argv,
        cwd="./",
        exit_code=7,
        timed_out=False,
        duration_ms=1,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_bytes=0,
        stderr_bytes=0,
    )
    argv.append("changed")
    assert result.argv == (sys.executable, "-c", "print('ok')")
    assert result.cwd == "."

    invalid = dict(
        cwd=".", exit_code=0, timed_out=False, duration_ms=0, stdout="", stderr="",
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )
    with pytest.raises(ProcessInputError):
        ProcessResult(argv=(), **invalid)
    with pytest.raises(ProcessInputError):
        ProcessResult(argv=("",), **invalid)
    with pytest.raises(ProcessInputError):
        ProcessResult(argv=("ok", "bad\x00arg"), **invalid)
    with pytest.raises(ProcessInputError):
        ProcessResult(argv=("ok",), cwd="../outside", **{k: v for k, v in invalid.items() if k != "cwd"})
    with pytest.raises(ProcessInputError):
        ProcessResult(argv=("ok",), exit_code=True, **{k: v for k, v in invalid.items() if k != "exit_code"})


def test_bounded_capture_custom_limit_retains_head_tail_and_marker():
    capture = BoundedCapture(limit=32)
    capture.consume(io.BytesIO(b"0123456789abcdefghijklmnopqrstuvwxyz"))
    assert capture.truncated is True
    assert capture.total == 36
    assert len(capture._head) + len(capture._tail) == 32
    rendered = capture.text()
    assert rendered.startswith("0123456789abcdef")
    assert rendered.endswith("uvwxyz")
    assert "<4 bytes omitted>" in rendered

    with pytest.raises(ValueError):
        BoundedCapture(limit=0)
    with pytest.raises(ValueError):
        BoundedCapture(limit=True)


def test_missing_executable_is_typed_spawn_failure(workspace):
    with pytest.raises(ProcessSpawnError):
        ProcessRunner().run(workspace, ProcessRequest(("imece-command-that-does-not-exist",)))
