"""Internal: exact-environment subprocess creation + official ACP stream
connection.

`acp.spawn_agent_process` is deliberately NOT used in production
`acp_runtime` code: its transport (`acp.spawn_stdio_transport`) always
merges the caller's env on top of `acp.default_environment()` (a curated
host-env subset), which violates the approved invariant that
`AcpLaunchSpec.env` is the EXACT child environment.

Instead, IMECE owns subprocess creation directly via
`asyncio.create_subprocess_exec` (never `shell=True`), and hands the child's
stdin/stdout streams to the official SDK's `acp.connect_to_agent(...)`, so
the JSON-RPC/NDJSON protocol implementation itself remains entirely
SDK-owned. Not exported from acp_runtime/__init__.py — private to
acp_runtime/client.py.

Because IMECE (not the SDK) owns process/stream lifecycle here,
`close_acp_agent_connection` also owns tearing that down: the SDK's
`Connection.close()` (invoked via `acp.connect_to_agent`'s returned
`ClientSideConnection`) only stops its own internal send loop -- unlike
`acp.spawn_agent_process`'s own transport, it does not close the underlying
`process.stdin` stream, so a well-behaved agent would otherwise never
observe end-of-input and exit on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Mapping

import acp

from process_runtime.cleanup import terminate_process_tree
from process_runtime.errors import ProcessCleanupError

from acp_runtime.errors import AcpCleanupError, AcpProtocolError

__all__ = ["spawn_acp_agent_connection", "close_acp_agent_connection"]


async def spawn_acp_agent_connection(
    client: Any, argv: tuple[str, ...], env: Mapping[str, str], cwd: str,
) -> tuple[Any, "asyncio.subprocess.Process"]:
    """Spawn one ACP agent subprocess with the EXACT given env, and return
    an official-SDK ClientSideConnection bound to its stdio streams.

    Raises OSError (uncaught) on OS-level spawn failure -- the caller maps
    that to AcpSpawnError before any usable process/connection exists.

    Once `create_subprocess_exec` succeeds, this function itself owns
    rollback until the (conn, process) handoff to the caller succeeds: a
    failure while validating the pipes or constructing the SDK connection
    terminates the already-created process tree before propagating, so a
    real OS process is never returned to the caller without a live
    connection over it (and never silently leaked either).
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
        cwd=cwd,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise AcpProtocolError("ACP agent subprocess did not provide stdin/stdout pipes.")
        conn = acp.connect_to_agent(client, process.stdin, process.stdout)
    except asyncio.CancelledError:
        try:
            await asyncio.to_thread(terminate_process_tree, process.pid)
        except ProcessCleanupError as cleanup_exc:
            raise AcpCleanupError(str(cleanup_exc)) from cleanup_exc
        raise
    except Exception as exc:  # noqa: BLE001 - post-spawn, pre-handoff construction failure
        if isinstance(exc, AcpProtocolError):
            construction_error: Exception = exc
        else:
            construction_error = AcpProtocolError(f"Could not construct ACP connection: {exc}")
            construction_error.__cause__ = exc
        try:
            await asyncio.to_thread(terminate_process_tree, process.pid)
        except ProcessCleanupError as cleanup_exc:
            raise AcpCleanupError(str(cleanup_exc)) from construction_error
        raise construction_error
    return conn, process


async def close_acp_agent_connection(conn: Any, process: "asyncio.subprocess.Process") -> None:
    """Official-SDK connection close, then explicit stdin EOF/close.

    `conn.close()` alone does not close `process.stdin` (see module
    docstring), so a graceful agent would otherwise be starved of the
    end-of-input signal it needs to exit on its own before hard
    process-tree cleanup runs.
    """
    await conn.close()
    stdin = getattr(process, "stdin", None)
    if stdin is not None and not stdin.is_closing():
        with contextlib.suppress(Exception):
            stdin.write_eof()
        with contextlib.suppress(Exception):
            await stdin.drain()
        with contextlib.suppress(Exception):
            stdin.close()
