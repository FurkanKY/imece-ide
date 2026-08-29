"""Synchronous host process runner with bounded capture and tree cleanup."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

from process_runtime.capture import BoundedCapture
from process_runtime.cleanup import terminate_process_tree
from process_runtime.errors import ProcessCleanupError, ProcessRuntimeError, ProcessSpawnError
from process_runtime.models import ProcessRequest, ProcessResult
from workspace.base import resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError

_SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "USER", "USERNAME", "LANG", "VIRTUAL_ENV",
}


def _safe_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    inherited: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _SAFE_ENV_KEYS or upper.startswith("LC_"):
            inherited[key] = value
    inherited.update(overrides)
    return inherited


def _cwd_path(workspace, relative_cwd: str) -> Path:
    try:
        if relative_cwd == ".":
            path = workspace.root.resolve(strict=True)
        else:
            path = resolve_within_workspace(
                workspace.root,
                relative_cwd,
                reject_symlinks=True,
            )
    except (WorkspaceBoundaryError, FileNotFoundError, OSError) as exc:
        raise ProcessSpawnError(f"Invalid process cwd: {relative_cwd!r}") from exc
    if not path.is_dir():
        raise ProcessSpawnError(f"Process cwd is not a directory: {relative_cwd!r}")
    return path


def _resolve_executable(executable: str, workspace, environment: Mapping[str, str]) -> str:
    if "/" in executable or "\\" in executable:
        try:
            normalized = executable.replace("\\", "/")
            if not Path(normalized).is_absolute():
                path = resolve_within_workspace(workspace.root, normalized, reject_symlinks=True)
                if not path.is_file():
                    raise ProcessSpawnError(f"Workspace executable not found: {executable}")
                return str(path)
        except WorkspaceBoundaryError as exc:
            raise ProcessSpawnError(f"Invalid workspace executable: {executable}") from exc
    resolved = shutil.which(executable, path=environment.get("PATH"))
    if resolved is None:
        raise ProcessSpawnError(f"Executable not found: {executable}")
    return resolved


class ProcessRunner:
    def run(self, workspace, request: ProcessRequest) -> ProcessResult:
        if not isinstance(request, ProcessRequest):
            raise ProcessRuntimeError("ProcessRunner requires ProcessRequest")
        cwd = _cwd_path(workspace, request.cwd)
        environment = _safe_environment(request.env)
        executable = _resolve_executable(request.argv[0], workspace, environment)
        argv = (executable, *request.argv[1:])
        started = time.monotonic()
        creationflags = 0
        popen_kwargs = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=environment,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            raise ProcessSpawnError(f"Could not spawn executable: {request.argv[0]}") from exc

        stdout_capture = BoundedCapture()
        stderr_capture = BoundedCapture()
        import threading

        stdout_thread = threading.Thread(target=stdout_capture.consume, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.consume, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=request.timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_error = None
            try:
                terminate_process_tree(process.pid)
            except ProcessCleanupError as exc:
                cleanup_error = exc
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                if cleanup_error is None:
                    cleanup_error = ProcessCleanupError(
                        "Timed-out process did not terminate after cleanup"
                    )
                    cleanup_error.__cause__ = exc
            if cleanup_error is not None:
                raise cleanup_error
        finally:
            stdout_thread.join(timeout=3)
            stderr_thread.join(timeout=3)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise ProcessRuntimeError("Process output capture did not terminate")
        if stdout_capture.error is not None:
            raise ProcessRuntimeError("stdout capture failed") from stdout_capture.error
        if stderr_capture.error is not None:
            raise ProcessRuntimeError("stderr capture failed") from stderr_capture.error
        duration_ms = int((time.monotonic() - started) * 1000)
        return ProcessResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=process.returncode,
            timed_out=timed_out,
            duration_ms=max(duration_ms, 0),
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            stdout_bytes=stdout_capture.total,
            stderr_bytes=stderr_capture.total,
        )
