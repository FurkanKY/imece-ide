"""webhost.api.activity -- F1 (live agent activity) bridge streamer.

Tails one pipeline Run's canonical events (run_runtime is the single source
of truth -- see docs/ARCHITECTURE.md's "Canonical event model") and emits
compact, bounded UI items on a NEW bridge channel, `run.activity`, so the AI
panel can show what each agent is doing while a run is still in progress
instead of only at stage boundaries.

This module invents NO new event path: every item comes from
run_runtime.activity_projection.project_event(event), applied to whatever
RunRuntime.open_event_tail(run_id) durably tails -- the exact same canonical
log webhost.api.run's flush_role_metrics() already polls for per-stage
metrics. `run.activity` is purely an additional, best-effort projection of
that same log; nothing here writes evidence, and a failure/crash of this
streamer can never affect a Run's canonical settlement.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QThread, Signal, Slot

from run_runtime.activity_projection import project_event
from run_runtime.service import RunRuntime

DEFAULT_MAX_ITEMS_PER_SECOND = 20

_activity_deliveries: set["_ActivityDelivery"] = set()


class _ActivityDelivery(QObject):
    """Own a streamer until its queued final activity has reached the host.

    Activity and finished are delivered to this same main-thread QObject.
    Qt preserves their order for a given sender/receiver pair, so handling
    finished is the safe point to release the sender and its pending signals.
    """

    def __init__(self, streamer: "ActivityStreamer", on_activity) -> None:
        super().__init__()
        self._streamer = streamer
        self._on_activity = on_activity
        streamer.activity.connect(self._deliver)
        streamer.finished.connect(self._finished)

    @Slot(dict)
    def _deliver(self, item: dict) -> None:
        self._on_activity(item)

    @Slot()
    def _finished(self) -> None:
        self._streamer = None
        self._on_activity = None
        _activity_deliveries.discard(self)
        self.deleteLater()


def start_activity_streamer(streamer: "ActivityStreamer", on_activity) -> None:
    """Start a streamer and retain it through delivery of its final signals."""
    delivery = _ActivityDelivery(streamer, on_activity)
    _activity_deliveries.add(delivery)
    try:
        streamer.start()
    except Exception:
        _activity_deliveries.discard(delivery)
        delivery._streamer = None
        delivery._on_activity = None
        delivery.deleteLater()
        raise


class ActivityStreamer(QThread):
    """Background tail of one Run's canonical events -> `run.activity` items.

    Follows the same QThread + Signal pattern as _Worker/_PipelineWorker in
    webhost.api.run: this class does the blocking wait (DurableEventTail.
    next_page) off the Qt main thread; `activity` is a queued-connection
    Signal so consumption always happens on the main thread.

    Throttling/coalescing: events are drained into a per-item-id buffer (so
    a burst of updates to the SAME id, e.g. tool.requested -> tool.started ->
    tool.completed, collapses to the latest state for that id) and flushed
    at most `max_items_per_second` times per second. Because coalescing
    always keeps the newest update for a given id, and the buffer is fully
    drained both on every flush tick and once more right before the thread
    exits, throttling can delay an item's final status but never drop it.
    """

    activity = Signal(dict)

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        *,
        after_seq: int = 0,
        max_items_per_second: int = DEFAULT_MAX_ITEMS_PER_SECOND,
    ) -> None:
        super().__init__()
        self._runtime = runtime
        self._run_id = run_id
        self._after_seq = after_seq
        self._flush_interval = 1.0 / max_items_per_second if max_items_per_second > 0 else 0.05
        self._stop_requested = False

    def request_stop(self) -> None:
        """Idempotent; safe to call from the main thread. Does not block --
        call QThread.wait() afterward if you need to block until drained."""
        self._stop_requested = True

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            tail = self._runtime.open_event_tail(self._run_id, after_seq=self._after_seq)
        except Exception:
            # Run may already be gone/unavailable; nothing to stream.
            return
        pending: dict[str, dict] = {}
        last_flush = time.monotonic()
        try:
            while not self._stop_requested:
                try:
                    page = tail.next_page(timeout=self._flush_interval)
                except Exception:
                    # EventStreamClosedError or similar: stop tailing.
                    break
                if page is not None:
                    self._collect(page, pending)
                now = time.monotonic()
                if pending and (now - last_flush) >= self._flush_interval:
                    self._flush(pending)
                    last_flush = now
            # Final catch-up: drain any remaining DURABLE backlog (not just
            # the in-memory buffer) with non-blocking reads before exiting.
            # request_stop() can race ahead of this thread ever getting a
            # scheduling slice -- an entirely in-process ScriptedBackend
            # pipeline run can go start-to-finish in well under a
            # millisecond, faster than this QThread's first next_page()
            # call -- so without this loop a fast run's activity could be
            # silently dropped instead of merely delayed.
            while True:
                try:
                    page = tail.next_page(timeout=0)
                except Exception:
                    break
                if page is None:
                    break
                self._collect(page, pending)
            self._flush(pending)
        finally:
            tail.close()

    def _collect(self, page, pending: dict[str, dict]) -> None:
        for event in page.events:
            try:
                item = project_event(event)
            except Exception:
                item = None
            if item is not None:
                pending[item["id"]] = item

    def _flush(self, pending: dict[str, dict]) -> None:
        if not pending:
            return
        for item in list(pending.values()):
            self.activity.emit(item)
        pending.clear()
