"""Exact-environment ACP stdio connection with Linux subreaper supervision."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import sys
from pathlib import Path
from typing import Any, Mapping

import acp

from process_runtime.runner import _read_windows_receipt, _validated_receipt
from process_runtime.supervisor_launch import supervisor_argv
from acp_runtime.errors import AcpCleanupError, AcpProtocolError


class SupervisedAcpProcess:
    """Async process facade plus the private positive-quiescence receipt."""
    def __init__(self, process, receipt_fd, nonce, *, receipt_path=None, cancel_path=None):
        self._process = process
        self._receipt_fd = receipt_fd
        self._receipt_path = receipt_path
        self._cancel_path = cancel_path
        self._nonce = nonce
        self.pid = process.pid
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.producer_quiescent = False

    async def wait(self):
        return await self._process.wait()

    async def wait_quiescent(self):
        code = await self._process.wait()
        if self._receipt_path is not None:
            raw = await asyncio.to_thread(_read_windows_receipt, self._receipt_path)
        else:
            raw = await asyncio.to_thread(os.read, self._receipt_fd, 1024)
            os.close(self._receipt_fd)
            self._receipt_fd = -1
        try:
            receipt = _validated_receipt(raw, self._nonce, code)
        finally:
            if self._receipt_path is not None:
                import shutil
                shutil.rmtree(self._receipt_path.parent, ignore_errors=True)
        self.producer_quiescent = True
        return receipt["exit_code"]

    def terminate_supervisor(self):
        if self._process.returncode is None:
            if self._cancel_path is not None:
                self._cancel_path.write_text("cancelled", encoding="ascii")
            else:
                self._process.send_signal(signal.SIGTERM)


async def spawn_acp_agent_connection(
    client: Any, argv: tuple[str, ...], env: Mapping[str, str], cwd: str,
    *, supervision_lease_fd: int | None = None,
) -> tuple[Any, Any]:
    """Spawn ACP directly on unsupported platforms (not sealable), or through
    the Linux subreaper proxy, preserving exact stdio protocol bytes."""
    if sys.platform.startswith("linux"):
        config_read, config_write = os.pipe()
        receipt_read, receipt_write = os.pipe()
        os.set_inheritable(config_read, True)
        os.set_inheritable(receipt_write, True)
        nonce = secrets.token_hex(32)
        wrapper_argv = supervisor_argv(str(config_read), str(receipt_write), nonce)
        inherited = [config_read, receipt_write]
        if supervision_lease_fd is not None:
            inherited.append(supervision_lease_fd)
        try:
            process = await asyncio.create_subprocess_exec(
                *wrapper_argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=dict(env), cwd=cwd, pass_fds=tuple(inherited),
                start_new_session=True,
            )
        except BaseException:
            for fd in (config_read, config_write, receipt_read, receipt_write):
                with contextlib.suppress(OSError): os.close(fd)
            raise
        os.close(config_read)
        os.close(receipt_write)
        config = json.dumps({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdio": "acp"},
                            separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with os.fdopen(config_write, "wb") as stream:
                stream.write(config)
            if process.stdin is None or process.stdout is None:
                raise AcpProtocolError("ACP supervisor did not provide stdio pipes.")
            supervised = SupervisedAcpProcess(process, receipt_read, nonce)
            conn = acp.connect_to_agent(client, process.stdin, process.stdout)
            return conn, supervised
        except BaseException as exc:
            with contextlib.suppress(Exception): process.send_signal(signal.SIGTERM)
            with contextlib.suppress(Exception): await process.wait()
            with contextlib.suppress(OSError): os.close(receipt_read)
            if isinstance(exc, asyncio.CancelledError): raise
            if isinstance(exc, AcpProtocolError): raise
            raise AcpProtocolError(f"Could not construct ACP connection: {exc}") from exc

    if os.name == "nt":
        import msvcrt
        import subprocess
        import tempfile
        supervisor_dir = Path(tempfile.mkdtemp(prefix="imece-acp-job-"))
        receipt_path = supervisor_dir / "receipt.json"
        cancel_path = supervisor_dir / "cancel"
        config_read, config_write = os.pipe()
        raw_config_handle = msvcrt.get_osfhandle(config_read)
        os.set_handle_inheritable(raw_config_handle, True)
        nonce = secrets.token_hex(32)
        # asyncio creates redirected stdio handles inside Popen, so they are
        # unavailable to an explicit HANDLE_LIST here. The config handle is the
        # only additional inheritable handle; Python's PEP 446 defaults keep
        # unrelated descriptors non-inheritable. The supervisor consumes and
        # closes this config handle before launching the contained agent.
        startup = subprocess.STARTUPINFO()
        wrapper_argv = supervisor_argv("--windows-acp", str(raw_config_handle),
                                       str(receipt_path), str(cancel_path), nonce)
        try:
            process = await asyncio.create_subprocess_exec(
                *wrapper_argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=dict(env), cwd=cwd,
                startupinfo=startup, close_fds=False,
            )
        except BaseException:
            for fd in (config_read, config_write):
                with contextlib.suppress(OSError): os.close(fd)
            import shutil
            shutil.rmtree(supervisor_dir, ignore_errors=True)
            raise
        os.close(config_read)
        payload = json.dumps({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdio": "acp"},
                             separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with os.fdopen(config_write, "wb") as stream:
                stream.write(payload)
            if process.stdin is None or process.stdout is None:
                raise AcpProtocolError("ACP Job supervisor did not provide stdio pipes.")
            supervised = SupervisedAcpProcess(process, None, nonce,
                receipt_path=receipt_path, cancel_path=cancel_path)
            return acp.connect_to_agent(client, process.stdin, process.stdout), supervised
        except BaseException as exc:
            with contextlib.suppress(Exception): process.kill()
            with contextlib.suppress(Exception): await process.wait()
            import shutil
            shutil.rmtree(supervisor_dir, ignore_errors=True)
            if isinstance(exc, asyncio.CancelledError): raise
            raise AcpProtocolError(f"Could not construct Windows ACP Job connection: {exc}") from exc

    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=dict(env), cwd=cwd,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise AcpProtocolError("ACP agent subprocess did not provide stdin/stdout pipes.")
        return acp.connect_to_agent(client, process.stdin, process.stdout), process
    except BaseException:
        with contextlib.suppress(Exception): process.kill()
        with contextlib.suppress(Exception): await process.wait()
        raise


async def close_acp_agent_connection(conn: Any, process: Any) -> None:
    await conn.close()
    stdin = getattr(process, "stdin", None)
    if stdin is not None and not stdin.is_closing():
        with contextlib.suppress(Exception): stdin.write_eof()
        with contextlib.suppress(Exception): await stdin.drain()
        with contextlib.suppress(Exception): stdin.close()


async def finish_acp_supervisor(process: Any, timeout_s: float) -> bool:
    """Wait for normal drain, then request supervised kill/reap if needed."""
    if not isinstance(process, SupervisedAcpProcess):
        return False
    try:
        await asyncio.wait_for(process.wait_quiescent(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.terminate_supervisor()
        try:
            await asyncio.wait_for(process.wait_quiescent(), timeout=max(8, timeout_s))
        except Exception as exc:
            raise AcpCleanupError(f"ACP producer tree did not quiesce: {exc}") from exc
    return process.producer_quiescent
