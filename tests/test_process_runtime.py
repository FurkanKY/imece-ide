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
from process_runtime.errors import ProcessSpawnError  # noqa: E402
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

    nonzero = runner.run(
        workspace,
        ProcessRequest(py("import sys; print('bad'); print('err', file=sys.stderr); sys.exit(7)")),
    )
    assert nonzero.exit_code == 7
    assert nonzero.timed_out is False
    assert "bad" in nonzero.stdout
    assert "err" in nonzero.stderr


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
