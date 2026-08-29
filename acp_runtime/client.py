"""AcpClientRuntime — one fresh local stdio ACP agent process per run().

IMECE owns subprocess creation (acp_runtime/stdio.py) so AcpLaunchSpec.env is
the exact child environment; the official agent-client-protocol SDK
continues to own the entire JSON-RPC/NDJSON protocol implementation. No
hand-written framing anywhere. See
docs/superpowers/specs/2026-08-29-acp-client-core-design.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import acp

from process_runtime.cleanup import ProcessTreeSnapshot, capture_process_tree, terminate_process_tree
from process_runtime.errors import ProcessCleanupError

from acp_runtime.errors import (
    AcpAuthenticationRequiredError,
    AcpCleanupError,
    AcpEventSinkError,
    AcpInputError,
    AcpLimitError,
    AcpProtocolError,
    AcpRuntimeError,
    AcpSpawnError,
    AcpTimeoutError,
)
from acp_runtime.events import (
    AcpEventSink,
    AcpPermissionRequested,
    AcpPermissionResolved,
    AcpSessionUpdateObserved,
    NullAcpEventSink,
)
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest, AcpRunResult
from acp_runtime.stdio import close_acp_agent_connection, spawn_acp_agent_connection

_AUTH_REQUIRED_CODE = -32000


def _cancelled_permission_response():
    return acp.RequestPermissionResponse(outcome=acp.schema.DeniedOutcome(outcome="cancelled"))


class _FatalSignal:
    """One-shot fatal-abort signal, set from inside SDK callback dispatch
    while conn.prompt(...) is still running concurrently. `error` is the
    authoritative state: it is written synchronously by trigger(), so any
    code that has not awaited since checking it can rely on its value being
    stable -- this is what closes the fatal-vs-prompt-success race (see
    AcpClientRuntime._run_prompt)."""

    __slots__ = ("event", "error")

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.error: AcpRuntimeError | None = None

    def trigger(self, error: AcpRuntimeError) -> None:
        if self.error is None:
            self.error = error
        self.event.set()


class _ImeceAcpClient:
    """Implements only the Client Protocol surfaces 3J2A needs.

    No fs/terminal/elicitation handlers are defined; the router maps any
    such call the agent might make to method_not_found automatically.
    """

    def __init__(self, *, limits: AcpClientLimits, event_sink: AcpEventSink, fatal: _FatalSignal) -> None:
        self._limits = limits
        self._event_sink = event_sink
        self._fatal = fatal
        self.session_id: str | None = None
        self.update_count = 0
        self.update_chars = 0
        self.permission_request_count = 0

    def bind_session(self, session_id: str) -> None:
        self.session_id = session_id

    def _emit(self, event: Any) -> bool:
        try:
            self._event_sink.emit(event)
        except Exception as exc:  # noqa: BLE001 - any sink failure is fatal
            error = AcpEventSinkError(f"AcpEventSink.emit failed: {exc}")
            error.__cause__ = exc
            self._fatal.trigger(error)
            return False
        return True

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self._fatal.event.is_set():
            return
        if self.session_id is None or session_id != self.session_id:
            self._fatal.trigger(
                AcpProtocolError(f"Received session_update for foreign session_id {session_id!r}.")
            )
            return
        try:
            serialized_chars = len(update.model_dump_json())
        except Exception as exc:  # noqa: BLE001
            error = AcpProtocolError(f"Could not serialize session update: {exc}")
            error.__cause__ = exc
            self._fatal.trigger(error)
            return
        if serialized_chars > self._limits.max_update_chars:
            self._fatal.trigger(
                AcpLimitError(
                    f"Session update exceeded max_update_chars "
                    f"({serialized_chars} > {self._limits.max_update_chars})."
                )
            )
            return
        self.update_count += 1
        self.update_chars += serialized_chars
        if self.update_count > self._limits.max_updates:
            self._fatal.trigger(AcpLimitError(f"Session update count exceeded max_updates ({self._limits.max_updates})."))
            return
        if self.update_chars > self._limits.max_total_update_chars:
            self._fatal.trigger(
                AcpLimitError(
                    f"Total session update chars exceeded max_total_update_chars "
                    f"({self._limits.max_total_update_chars})."
                )
            )
            return
        self._emit(AcpSessionUpdateObserved(session_id=session_id, update=update, serialized_chars=serialized_chars))

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any):
        if self._fatal.event.is_set():
            return _cancelled_permission_response()
        if self.session_id is None or session_id != self.session_id:
            self._fatal.trigger(
                AcpProtocolError(f"Received request_permission for foreign session_id {session_id!r}.")
            )
            return _cancelled_permission_response()

        tool_call_id = getattr(tool_call, "tool_call_id", "") or ""
        title = getattr(tool_call, "title", "") or ""
        option_ids = [getattr(option, "option_id", "") for option in (options or ())]

        if not self._emit(AcpPermissionRequested(session_id=session_id, tool_call_id=tool_call_id, title=title, option_ids=option_ids)):
            # Sink failed on the request event: fatal already triggered.
            # Never attempt the resolved event, never count this request.
            return _cancelled_permission_response()
        self.permission_request_count += 1

        self._emit(AcpPermissionResolved(session_id=session_id, tool_call_id=tool_call_id, outcome="cancelled"))

        return _cancelled_permission_response()


def _map_request_error(exc: "acp.RequestError") -> AcpRuntimeError:
    if exc.code == _AUTH_REQUIRED_CODE:
        return AcpAuthenticationRequiredError(str(exc))
    return AcpProtocolError(str(exc))


class AcpClientRuntime:
    """Async-only public entry point. One run() call = one fresh subprocess
    and one fresh session. No pooling, no reuse, no run_sync(). No mutable
    per-run state is stored on the instance itself (prompt/fatal tasks and
    process-tree snapshots are run-local), so one instance is safe for
    independent/concurrent run() invocations."""

    def __init__(self, *, _connect=None, _terminate_process_tree=None, _capture_process_tree=None) -> None:
        # Test-only constructor seams: let tests substitute fakes instead of
        # real subprocesses/psutil, without monkeypatching module globals.
        self._connect = _connect or spawn_acp_agent_connection
        self._terminate_process_tree = _terminate_process_tree or terminate_process_tree
        self._capture_process_tree = _capture_process_tree or capture_process_tree

    async def run(
        self,
        launch: AcpLaunchSpec,
        request: AcpPromptRequest,
        *,
        limits: AcpClientLimits | None = None,
        event_sink: AcpEventSink | None = None,
    ) -> AcpRunResult:
        if not isinstance(launch, AcpLaunchSpec):
            raise AcpInputError("AcpClientRuntime.run requires an AcpLaunchSpec.")
        if not isinstance(request, AcpPromptRequest):
            raise AcpInputError("AcpClientRuntime.run requires an AcpPromptRequest.")
        if limits is None:
            limits = AcpClientLimits()
        if not isinstance(limits, AcpClientLimits):
            raise AcpInputError("AcpClientRuntime.run limits must be an AcpClientLimits.")
        if event_sink is None:
            event_sink = NullAcpEventSink()
        emit = getattr(event_sink, "emit", None)
        if not callable(emit):
            raise AcpInputError("AcpClientRuntime.run event_sink must implement emit().")
        if inspect.iscoroutinefunction(emit):
            raise AcpInputError("AcpClientRuntime.run event_sink.emit must be synchronous, not a coroutine function.")
        if len(request.prompt) > limits.max_prompt_chars:
            raise AcpInputError(
                f"AcpPromptRequest.prompt exceeds max_prompt_chars ({len(request.prompt)} > {limits.max_prompt_chars})."
            )
        if not Path(request.cwd).is_dir():
            raise AcpInputError(f"AcpPromptRequest.cwd does not exist or is not a directory: {request.cwd}")

        fatal = _FatalSignal()
        client = _ImeceAcpClient(limits=limits, event_sink=event_sink, fatal=fatal)

        # Blocker 3: a real OS process may exist even if constructing the
        # SDK connection over it subsequently fails. self._connect (default
        # spawn_acp_agent_connection) owns rollback for that narrow window
        # itself and raises an already-typed AcpProtocolError/AcpCleanupError
        # in that case; only a genuine pre-process-creation OSError is
        # mapped here, since no process exists yet to roll back.
        try:
            conn, process = await self._connect(client, launch.argv, launch.env, request.cwd)
        except OSError as exc:
            raise AcpSpawnError(f"Could not spawn ACP agent process: {exc}") from exc

        # From this point on a real process/connection exists: every exit
        # path (success, any typed failure, or caller-side cancellation)
        # must run best-effort session close, connection teardown, hard
        # process-tree cleanup, and a final owned-task drain exactly once.
        owned_tasks: set[asyncio.Task] = set()
        session_id: str | None = None
        session_close_supported = False
        primary_exc: AcpRuntimeError | None = None
        cleanup_error: Exception | None = None
        result: AcpRunResult | None = None
        cleanup_cancelled = False
        try:
            init_response = await self._call_agent(conn.initialize, what="initialize", protocol_version=acp.PROTOCOL_VERSION, client_capabilities=None)
            session_capabilities = init_response.agent_capabilities.session_capabilities
            session_close_supported = session_capabilities is not None and session_capabilities.close is not None

            session_response = await self._call_agent(conn.new_session, what="new_session", cwd=request.cwd, mcp_servers=[])
            session_id = session_response.session_id
            if not isinstance(session_id, str) or not session_id:
                raise AcpProtocolError("Agent returned an empty session_id from new_session.")
            client.bind_session(session_id)

            stop_reason = await self._run_prompt(conn, session_id, request, limits, fatal, owned_tasks)
            result = AcpRunResult(
                session_id=session_id,
                stop_reason=stop_reason,
                update_count=client.update_count,
                update_chars=client.update_chars,
                permission_request_count=client.permission_request_count,
                session_close_supported=session_close_supported,
                session_close_succeeded=None,
            )
        except AcpRuntimeError as exc:
            primary_exc = exc
        finally:
            async def _cleanup() -> None:
                nonlocal cleanup_error, primary_exc, result

                # Snapshot BEFORE any graceful operation that might cause the
                # agent root to exit and orphan/reparent descendants.
                try:
                    snapshot: ProcessTreeSnapshot | None = self._capture_process_tree(process.pid)
                except ProcessCleanupError as exc:
                    snapshot = None
                    cleanup_error = exc

                session_close_succeeded: bool | None = None
                if session_id is not None and session_close_supported:
                    try:
                        await self._await_bounded(
                            conn.close_session(session_id),
                            limits.session_close_timeout_ms / 1000,
                            owned_tasks,
                        )
                        session_close_succeeded = True
                    except Exception:  # noqa: BLE001 - best-effort only
                        session_close_succeeded = False
                if result is not None:
                    result = dataclasses.replace(result, session_close_succeeded=session_close_succeeded)

                try:
                    await self._await_bounded(
                        close_acp_agent_connection(conn, process),
                        limits.session_close_timeout_ms / 1000,
                        owned_tasks,
                    )
                except Exception as exc:  # noqa: BLE001
                    if primary_exc is None:
                        primary_exc = AcpProtocolError(f"ACP connection close failed: {exc}")
                        primary_exc.__cause__ = exc

                try:
                    await asyncio.to_thread(self._terminate_process_tree, process.pid, snapshot=snapshot)
                except ProcessCleanupError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc

                # The final drain is intentionally after connection/process
                # teardown. A surviving task is an explicit cleanup failure.
                drained = await self._drain_owned_tasks(owned_tasks, limits)
                if not drained and cleanup_error is None:
                    cleanup_error = RuntimeError(
                        "A runtime-owned task survived teardown after connection close and process cleanup."
                    )

                if cleanup_error is not None:
                    raise AcpCleanupError(str(cleanup_error)) from (primary_exc or cleanup_error)

            cleanup_task = asyncio.create_task(_cleanup())
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # Keep ownership of cleanup in this run. A caller may
                    # cancel repeatedly, but no teardown stage may be skipped.
                    cleanup_cancelled = True

            try:
                cleanup_task.result()
            except asyncio.CancelledError as exc:
                raise AcpCleanupError("ACP teardown task was cancelled internally.") from exc

        if cleanup_cancelled:
            raise asyncio.CancelledError

        if primary_exc is not None:
            raise primary_exc
        assert result is not None
        return result

    async def _call_agent(self, method, *, what: str, **kwargs: Any):
        try:
            return await method(**kwargs)
        except asyncio.CancelledError:
            raise
        except acp.RequestError as exc:
            raise _map_request_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - unexpected SDK/connection/schema failure
            raise AcpProtocolError(f"ACP {what} failed: {exc}") from exc

    async def _run_prompt(
        self, conn, session_id: str, request: AcpPromptRequest, limits: AcpClientLimits,
        fatal: _FatalSignal, owned_tasks: set[asyncio.Task],
    ) -> str:
        prompt_task = asyncio.ensure_future(conn.prompt(session_id, [acp.text_block(request.prompt)]))
        fatal_task = asyncio.ensure_future(fatal.event.wait())
        owned_tasks.add(prompt_task)
        owned_tasks.add(fatal_task)
        cancel_grace_s = limits.cancel_grace_ms / 1000

        done, _pending = await asyncio.wait(
            {prompt_task, fatal_task}, timeout=limits.prompt_timeout_ms / 1000, return_when=asyncio.FIRST_COMPLETED,
        )

        # Authoritative check: fatal.error is written synchronously by
        # _FatalSignal.trigger(), with no await between that write and this
        # read, so it cannot be stale here even if fatal_task's own
        # completion has not yet been processed by the scheduler.
        if fatal.error is not None:
            await self._cancel_and_settle(conn, session_id, prompt_task, cancel_grace_s, owned_tasks)
            self._retire_if_done(fatal_task, owned_tasks)
            raise fatal.error

        if prompt_task not in done:
            await self._cancel_and_settle(conn, session_id, prompt_task, cancel_grace_s, owned_tasks)
            self._retire_if_done(fatal_task, owned_tasks)
            raise AcpTimeoutError(f"ACP prompt did not complete within {limits.prompt_timeout_ms}ms.")

        # prompt_task genuinely completed within the bound: fatal_task is no
        # longer useful, cancel it (bounded -- it is a plain Event.wait()).
        if not fatal_task.done():
            fatal_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fatal_task
        owned_tasks.discard(fatal_task)
        owned_tasks.discard(prompt_task)

        exc = prompt_task.exception()
        if exc is not None:
            if isinstance(exc, acp.RequestError):
                raise _map_request_error(exc) from exc
            raise AcpProtocolError(f"ACP prompt failed: {exc}") from exc

        return prompt_task.result().stop_reason

    @staticmethod
    def _retire_if_done(task: "asyncio.Task", owned_tasks: set["asyncio.Task"]) -> None:
        if task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                task.exception()
            owned_tasks.discard(task)

    async def _cancel_and_settle(
        self, conn, session_id: str, prompt_task: "asyncio.Task", cancel_grace_s: float,
        owned_tasks: set["asyncio.Task"],
    ) -> None:
        """Best-effort AND bounded: sending session/cancel and waiting for
        the prompt to voluntarily settle are each independently bounded by
        cancel_grace_s, so a stalled `conn.cancel(...)` call can never by
        itself prevent the caller from reaching outer teardown. The cancel
        send is a run-owned task: if it survives its deadline, it is
        cancellation-requested and retained for the final owned-task drain.
        This never awaits prompt_task unboundedly."""
        cancel_task = asyncio.create_task(conn.cancel(session_id))
        owned_tasks.add(cancel_task)
        done, _pending = await asyncio.wait({cancel_task}, timeout=cancel_grace_s)
        if cancel_task in done:
            self._retire_if_done(cancel_task, owned_tasks)
        else:
            cancel_task.cancel()
        if not prompt_task.done():
            await asyncio.wait({prompt_task}, timeout=cancel_grace_s)
        if not prompt_task.done():
            prompt_task.cancel()

    async def _await_bounded(self, awaitable, timeout_s: float, owned_tasks: set["asyncio.Task"]):
        """Await one teardown stage without waiting for cancellation to
        complete after its deadline. A stage task that resists cancellation
        remains owned and is handled by the final drain."""
        task = asyncio.create_task(awaitable)
        owned_tasks.add(task)
        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
        if task not in done:
            task.cancel()
            raise asyncio.TimeoutError
        owned_tasks.discard(task)
        if task.cancelled():
            raise RuntimeError("ACP teardown stage was cancelled internally.")
        return task.result()

    async def _drain_owned_tasks(self, owned_tasks: set["asyncio.Task"], limits: AcpClientLimits) -> bool:
        """Bounded final reap of any still-owned runtime tasks, run
        AFTER the connection and process are already gone (so a real SDK
        task should settle quickly on its own). Returns True if every task
        was reaped; False if at least one survived even this bound."""
        pending = [task for task in owned_tasks if not task.done()]
        for task in pending:
            task.cancel()
        survivors = set(pending)
        if pending:
            _done, survivors = await asyncio.wait(pending, timeout=limits.cancel_grace_ms / 1000)
        for task in list(owned_tasks):
            if task.done() and task not in survivors:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    task.exception()
                owned_tasks.discard(task)
        for task in survivors:
            if task.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    task.exception()
        return not survivors and not owned_tasks
