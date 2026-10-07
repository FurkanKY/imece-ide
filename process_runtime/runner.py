"""Synchronous host process runner with bounded capture and tree cleanup."""

from __future__ import annotations

import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

from process_runtime.capture import BoundedCapture
from process_runtime.cleanup import terminate_process_tree
from process_runtime.errors import (
    ProcessCancelledError,
    ProcessCleanupError,
    ProcessRuntimeError,
    ProcessSpawnError,
)
from process_runtime.models import ProcessRequest, ProcessResult
from process_runtime.supervisor_launch import supervisor_argv
from workspace.base import resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError

_CANCEL_POLL_S = 0.15

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


def _read_windows_receipt(path: Path) -> bytes:
    """Read a small receipt without trusting a target-writable temp path size."""
    with path.open("rb") as stream:
        raw = stream.read(1025)
    if len(raw) > 1024:
        raise ProcessCleanupError("Supervisor receipt exceeded its size bound")
    return raw


def _validated_receipt(raw: bytes, nonce: str, supervisor_exit: int) -> dict:
    try:
        receipt = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessCleanupError("Supervisor did not issue a valid quiescence receipt") from exc
    if (not isinstance(receipt, dict)
            or set(receipt) not in ({"nonce", "exit_code", "quiescent"},
                                     {"nonce", "exit_code", "quiescent", "cancelled"})
            or receipt["nonce"] != nonce or receipt["quiescent"] is not True
            or ("cancelled" in receipt and type(receipt["cancelled"]) is not bool)
            or type(receipt["exit_code"]) is not int or supervisor_exit != 0):
        raise ProcessCleanupError("Supervisor quiescence receipt failed validation")
    return receipt


class ProcessRunner:
    def run(self, workspace, request: ProcessRequest, *, cancel_token=None) -> ProcessResult:
        """`cancel_token` is duck-typed (only `.cancelled` is read) so
        process_runtime never needs to import agent_runtime -- pass an
        agent_runtime.cancellation.CancellationToken or anything exposing
        the same boolean property. Checked in bounded polling slices
        (`_CANCEL_POLL_S`) while waiting on the process; on cancellation the
        process tree is killed exactly like a timeout and
        ProcessCancelledError is raised instead of a ProcessResult."""
        if not isinstance(request, ProcessRequest):
            raise ProcessRuntimeError("ProcessRunner requires ProcessRequest")
        cwd = _cwd_path(workspace, request.cwd)
        environment = _safe_environment(request.env)
        executable = _resolve_executable(request.argv[0], workspace, environment)
        argv = (executable, *request.argv[1:])
        started = time.monotonic()
        creationflags = 0
        popen_kwargs = {}
        linux_supervised = sys.platform.startswith("linux")
        windows_supervised = os.name == "nt"
        supervisor_dir = None
        receipt_path = cancel_path = None
        receipt_read = receipt_write = config_read = config_write = None
        nonce = None
        if linux_supervised:
            receipt_read, receipt_write = os.pipe()
            config_read, config_write = os.pipe()
            os.set_inheritable(receipt_write, True)
            os.set_inheritable(config_read, True)
            nonce = secrets.token_hex(32)
            wrapper_argv = supervisor_argv(str(config_read), str(receipt_write), nonce)
            ownership = getattr(workspace, "ownership", None)
            lease_fd = getattr(getattr(ownership, "lease", None), "fd", None)
            inherited_fds = (receipt_write, config_read) if lease_fd is None else (receipt_write, config_read, lease_fd)
            popen_kwargs.update(start_new_session=True, pass_fds=inherited_fds)
            stdin = subprocess.DEVNULL
            argv_to_spawn = wrapper_argv
        elif windows_supervised:
            import tempfile
            supervisor_dir = Path(tempfile.mkdtemp(prefix="imece-job-"))
            receipt_path = supervisor_dir / "receipt.json"
            cancel_path = supervisor_dir / "cancel"
            nonce = secrets.token_hex(32)
            wrapper_argv = supervisor_argv("--windows-process", str(receipt_path), str(cancel_path), nonce)
            stdin = subprocess.PIPE
            argv_to_spawn = wrapper_argv
        else:
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            stdin = subprocess.DEVNULL
            argv_to_spawn = argv
        process = None
        try:
            process = subprocess.Popen(
                argv_to_spawn,
                cwd=str(cwd),
                env=environment,
                shell=False,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                **popen_kwargs,
            )
            if linux_supervised:
                os.close(receipt_write)
                receipt_write = None
                os.close(config_read)
                config_read = None
                payload = json.dumps({"argv": list(argv), "cwd": str(cwd), "env": environment,
                                      "stdio": "process"}, separators=(",", ":")).encode("utf-8")
                with os.fdopen(config_write, "wb") as stream:
                    config_write = None
                    stream.write(payload + b"\n")
            elif windows_supervised:
                payload = json.dumps({"argv": list(argv), "cwd": str(cwd), "env": environment,
                                      "stdio": "process"}, separators=(",", ":")).encode("utf-8")
                process.stdin.write(payload + b"\n")
                process.stdin.close()
        except (OSError, ValueError) as exc:
            if process is not None:
                # Configuration-pipe failure after Popen must not leave an
                # untracked supervisor, zombie, or unread pipe behind.
                try:
                    process.send_signal(signal.SIGTERM)
                except OSError:
                    pass  # It may have exited as the config pipe closed.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if supervisor_dir is not None:
                import shutil
                shutil.rmtree(supervisor_dir, ignore_errors=True)
            for fd in (receipt_read, receipt_write, config_read, config_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            raise ProcessSpawnError(f"Could not spawn executable: {request.argv[0]}") from exc

        stdout_capture = BoundedCapture()
        stderr_capture = BoundedCapture()
        import threading

        stdout_thread = threading.Thread(target=stdout_capture.consume, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.consume, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        cancelled = False
        deadline = started + request.timeout_ms / 1000
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if cancel_token is not None and cancel_token.cancelled:
                    cancelled = True
                    break
                try:
                    process.wait(timeout=min(_CANCEL_POLL_S, remaining))
                    break  # process exited on its own
                except subprocess.TimeoutExpired:
                    continue
            if timed_out or cancelled:
                cleanup_error = None
                try:
                    if linux_supervised:
                        process.send_signal(signal.SIGTERM)
                    elif windows_supervised:
                        cancel_path.write_text("cancelled" if cancelled else "timeout", encoding="ascii")
                    else:
                        terminate_process_tree(process.pid)
                except (ProcessCleanupError, ProcessLookupError, OSError) as exc:
                    cleanup_error = (exc if isinstance(exc, ProcessCleanupError)
                                     else ProcessCleanupError("Could not signal process supervisor"))
                try:
                    process.wait(timeout=None if linux_supervised else 10 if windows_supervised else 2)
                except subprocess.TimeoutExpired as exc:
                    if cleanup_error is None:
                        cleanup_error = ProcessCleanupError(
                            "Process did not terminate after cleanup"
                        )
                        cleanup_error.__cause__ = exc
                if cleanup_error is not None:
                    if receipt_read is not None:
                        os.close(receipt_read)
                    if supervisor_dir is not None:
                        import shutil
                        shutil.rmtree(supervisor_dir, ignore_errors=True)
                    raise cleanup_error
        finally:
            stdout_thread.join(timeout=3)
            stderr_thread.join(timeout=3)
        # Authenticate the private receipt before exposing cancellation proof.
        # Even capture errors do not erase a valid process-tree quiescence fact.
        producer_quiescent = False
        exit_code = process.returncode
        receipt = None
        if linux_supervised:
            try:
                raw = os.read(receipt_read, 1024)
                receipt = _validated_receipt(raw, nonce, process.returncode)
                exit_code = receipt["exit_code"]
                producer_quiescent = True
            finally:
                os.close(receipt_read)
                receipt_read = None
        elif windows_supervised:
            try:
                raw = _read_windows_receipt(receipt_path)
                receipt = _validated_receipt(raw, nonce, process.returncode)
                exit_code = receipt["exit_code"]
                producer_quiescent = True
            except OSError as exc:
                raise ProcessCleanupError("Windows Job supervisor did not provide a receipt") from exc
            finally:
                import shutil
                shutil.rmtree(supervisor_dir, ignore_errors=True)
        if cancelled or (receipt and receipt.get("cancelled")):
            raise ProcessCancelledError(
                f"Process cancelled while waiting: {request.argv[0]!r}",
                producer_quiescent=producer_quiescent,
            )
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
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=max(duration_ms, 0),
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            stdout_bytes=stdout_capture.total,
            stderr_bytes=stderr_capture.total,
            producer_quiescent=producer_quiescent,
        )
