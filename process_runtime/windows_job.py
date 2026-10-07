"""Windows Job Object containment used by the process supervisor.

Targets are created suspended, assigned to a non-breakaway kill-on-close Job,
and only then resumed. A quiescence receipt is valid only after the root exits
and the kernel reports ActiveProcesses == 0.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p),
                ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
                ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32),
                ("dwXCountChars", ctypes.c_uint32), ("dwYCountChars", ctypes.c_uint32),
                ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]


def _winerror():
    return ctypes.WinError(ctypes.get_last_error())


def _launch_suspended(k, argv, cwd, env_buf, startup, pi, job):
    """Launch with Popen-compatible executable lookup, then contain before run."""
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    # A NULL lpApplicationName delegates PATH, cwd and executable-extension
    # resolution to CreateProcessW, matching subprocess.Popen on Windows.
    if not k.CreateProcessW(None, command, None, None, True,
                            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                            ctypes.cast(env_buf, ctypes.c_void_p), cwd,
                            ctypes.byref(startup), ctypes.byref(pi)):
        raise _winerror()
    if not k.AssignProcessToJobObject(job, pi.hProcess):
        error = _winerror()  # cleanup APIs may overwrite thread-local last-error
        k.TerminateProcess(pi.hProcess, 125)
        k.WaitForSingleObject(pi.hProcess, INFINITE)
        raise error
    if k.ResumeThread(pi.hThread) == 0xFFFFFFFF:
        error = _winerror()
        k.TerminateJobObject(job, 125)
        k.WaitForSingleObject(pi.hProcess, INFINITE)
        raise error


def run_in_job(payload: dict, cancel_path: Path):
    if os.name != "nt":
        raise OSError("Windows Job Objects are only available on Windows")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]; k.CreateJobObjectW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k.CreateFileW.restype = ctypes.c_void_p
    k.GetStdHandle.argtypes = [ctypes.c_int]; k.GetStdHandle.restype = ctypes.c_void_p
    k.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    k.SetHandleInformation.restype = ctypes.c_int
    k.CreateProcessW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(_STARTUPINFO), ctypes.POINTER(_PROCESS_INFORMATION)]
    k.CreateProcessW.restype = ctypes.c_int
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]; k.TerminateProcess.restype = ctypes.c_int
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k.SetInformationJobObject.restype = ctypes.c_int
    k.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = ctypes.c_int
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]; k.AssignProcessToJobObject.restype = ctypes.c_int
    k.ResumeThread.argtypes = [ctypes.c_void_p]; k.ResumeThread.restype = ctypes.c_uint32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]; k.WaitForSingleObject.restype = ctypes.c_uint32
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]; k.GetExitCodeProcess.restype = ctypes.c_int
    k.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]; k.TerminateJobObject.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]; k.CloseHandle.restype = ctypes.c_int
    job = k.CreateJobObjectW(None, None)
    if not job: raise _winerror()
    pi = _PROCESS_INFORMATION()
    nul = None
    try:
        limits = _EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                         ctypes.byref(limits), ctypes.sizeof(limits)):
            raise _winerror()
        startup = _STARTUPINFO(); startup.cb = ctypes.sizeof(startup); startup.dwFlags = STARTF_USESTDHANDLES
        if payload["stdio"] == "process":
            nul = k.CreateFileW("NUL", 0x80000000, 3, None, 3, 0x80, None)
            if nul == ctypes.c_void_p(-1).value: raise _winerror()
            startup.hStdInput = nul
        else:
            startup.hStdInput = k.GetStdHandle(-10)
        startup.hStdOutput = k.GetStdHandle(-11)
        startup.hStdError = k.GetStdHandle(-12)
        for std_handle in (startup.hStdInput, startup.hStdOutput, startup.hStdError):
            if not std_handle or std_handle == ctypes.c_void_p(-1).value:
                raise OSError("Required supervisor stdio handle is unavailable")
            if not k.SetHandleInformation(std_handle, 1, 1):
                raise _winerror()
        env = "\0".join(f"{key}={value}" for key, value in sorted(payload["env"].items(), key=lambda x: x[0].upper())) + "\0\0"
        env_buf = ctypes.create_unicode_buffer(env)
        _launch_suspended(k, payload["argv"], payload["cwd"], env_buf, startup, pi, job)
        k.CloseHandle(pi.hThread); pi.hThread = None
        # The target inherited these standard handles while suspended. Close
        # the supervisor's copies so EOF reaches the host as soon as the whole
        # Job drains (especially important for ACP stdio framing).
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            try: stream.close()
            except OSError: pass
        cancelled = False
        cancel_applied = False
        while True:
            wait_result = k.WaitForSingleObject(pi.hProcess, 50)
            if wait_result == WAIT_OBJECT_0: break
            if wait_result != WAIT_TIMEOUT: raise _winerror()
            if cancel_path.exists():
                try: reason = cancel_path.read_text(encoding="ascii")
                except OSError: reason = "invalid"
                if reason not in {"timeout", "cancelled"}:
                    raise OSError("Invalid supervisor cancellation control")
                cancelled = reason == "cancelled"
                if not k.TerminateJobObject(job, 1): raise _winerror()
                cancel_applied = True
                break
        if k.WaitForSingleObject(pi.hProcess, INFINITE) != WAIT_OBJECT_0: raise _winerror()
        # This is a kernel-maintained membership count. Job breakaway is not
        # enabled; descendants cannot escape the assigned job. Check control
        # while draining even if the root exited before its children.
        while True:
            if not cancel_applied and cancel_path.exists():
                try: reason = cancel_path.read_text(encoding="ascii")
                except OSError: reason = "invalid"
                if reason not in {"timeout", "cancelled"}:
                    raise OSError("Invalid supervisor cancellation control")
                cancelled = reason == "cancelled"
                if not k.TerminateJobObject(job, 1): raise _winerror()
                cancel_applied = True
            accounting = _BASIC_ACCOUNTING()
            if not k.QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                               ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise _winerror()
            if accounting.ActiveProcesses == 0: break
            time.sleep(.01)
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(pi.hProcess, ctypes.byref(code)): raise _winerror()
        return (code.value, cancelled)
    finally:
        if pi.hThread: k.CloseHandle(pi.hThread)
        if pi.hProcess: k.CloseHandle(pi.hProcess)
        if nul and nul != ctypes.c_void_p(-1).value: k.CloseHandle(nul)
        k.CloseHandle(job)
