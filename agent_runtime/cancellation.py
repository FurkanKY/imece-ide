"""Shared cooperative-cancellation primitive (F7 "real cancellation").

CancellationToken wraps a plain threading.Event so it stays trivially
compatible with every existing `cancel_event: threading.Event | None` call
site in pipeline_runtime/fix_runtime (a bare Event can be wrapped with
`CancellationToken.from_event(...)`, and the token's own `.event` is a real
threading.Event any legacy caller can still `.set()`/`.is_set()` directly).

It is deliberately dependency-free (stdlib only) so every layer that needs to
observe cancellation -- agent_runtime (native AgentSession), process_runtime
(ProcessRunner), acp_runtime (AcpClientRuntime), verification_runtime,
executor_runtime, fix_runtime, pipeline_runtime -- can import
`OperationCancelledError`/`CancellationToken` without creating a layering
cycle; those modules never need to import anything else from agent_runtime.

`OperationCancelledError` is the one exception every layer's own typed
Cancelled subclass multiply-inherits from (e.g.
`agent_runtime.errors.AgentCancelledError(AgentRuntimeError,
OperationCancelledError)`), so a caller several layers up can catch
`OperationCancelledError` alone to detect "this failed because of
cancellation" without knowing which layer raised it, while a caller that only
cares about one layer's own error hierarchy still catches its usual base
class unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class OperationCancelledError(Exception):
    """Raised when code observes a CancellationToken has been cancelled."""


class CancellationToken:
    """Cooperative cancellation signal, checked between (and, where the
    underlying primitive supports it, during) discrete units of work.

    Not a substitute for hard preemption: an in-flight blocking call with no
    cooperative check of its own (e.g. a single outbound HTTP request to a
    model provider) cannot be interrupted mid-call by this token alone --
    callers document that limit at the call site (see agent_runtime.session.
    AgentSession) rather than pretending otherwise.
    """

    __slots__ = ("_event", "_callbacks", "_lock")

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event if event is not None else threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @classmethod
    def from_event(cls, event: threading.Event | None) -> "CancellationToken | None":
        """Wrap an existing threading.Event, or return None if `event` is
        None -- lets every existing `cancel_event: threading.Event | None`
        call site adopt CancellationToken without changing its own optionality."""
        if event is None:
            return None
        return cls(event)

    @property
    def event(self) -> threading.Event:
        """The underlying Event -- legacy callers may still .set() it directly."""
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        already = self._event.is_set()
        self._event.set()
        if already:
            return
        with self._lock:
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass  # a waking callback is best-effort; never break cancel()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelledError("Operation was cancelled.")

    def wait(self, timeout: float | None = None) -> bool:
        """Block (the calling thread) until cancelled or `timeout` elapses.
        Safe to call from a worker thread handed off via
        `loop.run_in_executor(...)` to wake an asyncio side without a busy
        poll loop tighter than the timeout granularity the caller chooses."""
        return self._event.wait(timeout)

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Register `callback` to run (once) when this token is cancelled.
        If already cancelled, runs it immediately instead."""
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                self._callbacks.append(callback)
                run_now = False
        if run_now:
            callback()
