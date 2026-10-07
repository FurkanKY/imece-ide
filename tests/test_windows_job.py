"""Portable launch-order contracts and native Windows Job Object checks."""
import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from process_runtime import ProcessRequest, ProcessRunner
from process_runtime import windows_job


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def test_createprocess_uses_windows_search_and_assigns_before_resume(monkeypatch):
    calls = []
    def create(app, command, _pa, _ta, inherit, flags, _env, cwd, _startup, _info):
        calls.append(("create", app, command.value, inherit, flags, cwd))
        return 1
    k = SimpleNamespace(
        CreateProcessW=FakeFunction(create),
        AssignProcessToJobObject=FakeFunction(lambda job, proc: calls.append(("assign", job, proc)) or 1),
        ResumeThread=FakeFunction(lambda thread: calls.append(("resume", thread)) or 1),
    )
    startup = windows_job._STARTUPINFO()
    pi = windows_job._PROCESS_INFORMATION()
    pi.hProcess, pi.hThread = 101, 202
    env = ctypes.create_unicode_buffer("PATH=C:\\tools\0\0")
    windows_job._launch_suspended(k, ["python", "-c", "print('ok')"], r"C:\work", env, startup, pi, 77)
    assert calls[0][0:2] == ("create", None)
    assert calls[0][3] is True
    assert calls[0][4] & windows_job.CREATE_SUSPENDED
    assert calls[0][4] & windows_job.CREATE_UNICODE_ENVIRONMENT
    assert calls[1:] == [("assign", 77, 101), ("resume", 202)]


def test_assign_failure_never_resumes_and_terminates_suspended_root(monkeypatch):
    calls = []
    monkeypatch.setattr(windows_job, "_winerror", lambda: OSError("assign failed"))
    def failure():
        return 0
    k = SimpleNamespace(
        CreateProcessW=FakeFunction(lambda *args: calls.append("create") or 1),
        AssignProcessToJobObject=FakeFunction(lambda *args: failure()),
        TerminateProcess=FakeFunction(lambda *args: calls.append("terminate-root") or 1),
        WaitForSingleObject=FakeFunction(lambda *args: calls.append("wait-root") or 0),
        ResumeThread=FakeFunction(lambda *args: calls.append("resume") or 1),
    )
    pi = windows_job._PROCESS_INFORMATION(); pi.hProcess, pi.hThread = 101, 202
    with pytest.raises(OSError):
        windows_job._launch_suspended(k, ["python"], ".", ctypes.create_unicode_buffer("\0\0"),
                                      windows_job._STARTUPINFO(), pi, 77)
    assert calls == ["create", "terminate-root", "wait-root"]


def test_resume_failure_terminates_job_without_claiming_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(windows_job, "_winerror", lambda: OSError("resume failed"))
    k = SimpleNamespace(
        CreateProcessW=FakeFunction(lambda *args: calls.append("create") or 1),
        AssignProcessToJobObject=FakeFunction(lambda *args: calls.append("assign") or 1),
        ResumeThread=FakeFunction(lambda *args: calls.append("resume") or 0xFFFFFFFF),
        TerminateJobObject=FakeFunction(lambda *args: calls.append("terminate-job") or 1),
        WaitForSingleObject=FakeFunction(lambda *args: calls.append("wait-root") or 0),
    )
    pi = windows_job._PROCESS_INFORMATION(); pi.hProcess, pi.hThread = 101, 202
    with pytest.raises(OSError, match="resume failed"):
        windows_job._launch_suspended(k, ["python"], ".", ctypes.create_unicode_buffer("\0\0"),
                                      windows_job._STARTUPINFO(), pi, 77)
    assert calls == ["create", "assign", "resume", "terminate-job", "wait-root"]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows launch regression")
def test_windows_job_resolves_bare_executable_on_path(workspace):
    # `python` is intentionally not sys.executable: CreateProcess must apply
    # Windows PATH and executable-extension lookup just as Popen does.
    result = ProcessRunner().run(workspace, ProcessRequest(("python", "-c", "print('path-ok')")))
    assert result.exit_code == 0 and "path-ok" in result.stdout
    assert result.producer_quiescent is True


@pytest.mark.skipif(os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8,
                    reason="Native 64-bit Windows ABI layout")
def test_windows_job_structures_match_win32_x64_abi():
    assert ctypes.sizeof(windows_job._STARTUPINFO) == 104
    assert windows_job._STARTUPINFO.hStdInput.offset == 80
    assert ctypes.sizeof(windows_job._PROCESS_INFORMATION) == 24
    assert windows_job._PROCESS_INFORMATION.dwProcessId.offset == 16
    assert ctypes.sizeof(windows_job._BASIC_ACCOUNTING) == 48
    assert windows_job._BASIC_ACCOUNTING.ActiveProcesses.offset == 40
    assert ctypes.sizeof(windows_job._EXTENDED_LIMIT) == 144
    assert windows_job._EXTENDED_LIMIT.IoInfo.offset == 64
