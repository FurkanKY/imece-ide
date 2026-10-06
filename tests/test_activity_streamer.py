"""webhost.api.activity.ActivityStreamer -- tails canonical events into
`run.activity` items with coalescing/throttling (Aşama 3 F1 decision 1).

Verifies: (a) items stream live while the run is still in progress (not
just at stage boundaries), (b) a burst of updates to the SAME id (e.g. a
tool call's requested -> started -> completed lifecycle) coalesces so the
FINAL status for that id is always what's last observed, never dropped by
throttling, and (c) request_stop()+wait() drains any still-buffered item
before the thread exits.
"""

from __future__ import annotations

import sys
import time
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication

from run_runtime import RunEventType, RunRuntime, RunStore
from run_runtime.agent_activity import record_agent_activity
from webhost.api.activity import ActivityStreamer, start_activity_streamer


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _running_runtime(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return runtime, run.run_id


def _pump_until(qapp, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_streamer_emits_live_items_while_run_is_in_progress(qapp, tmp_path):
    runtime, run_id = _running_runtime(tmp_path)
    seen = []
    streamer = ActivityStreamer(runtime, run_id, max_items_per_second=20)
    streamer.activity.connect(lambda item: seen.append(item))
    streamer.start()
    try:
        record_agent_activity(runtime, run_id, role="worker", kind="tool", title="Okundu: a.py", status="running")
        assert _pump_until(qapp, lambda: len(seen) >= 1)
        assert seen[0]["title"] == "Okundu: a.py"
        assert seen[0]["status"] == "running"
    finally:
        streamer.request_stop()
        streamer.wait(2000)


def test_burst_to_same_id_coalesces_but_never_drops_the_final_status(qapp, tmp_path):
    runtime, run_id = _running_runtime(tmp_path)
    seen = []
    # Slow flush cadence (2/s) so many rapid updates land inside ONE flush
    # window and must coalesce -- this is the throttling case the F1 plan
    # explicitly asks to cover.
    streamer = ActivityStreamer(runtime, run_id, max_items_per_second=2)
    streamer.activity.connect(lambda item: seen.append(item))
    streamer.start()
    try:
        # Requested -> started -> completed, all sharing the same id (see
        # run_runtime.activity_projection: tool:<execution_id>:<call_id>).
        runtime.record(run_id=run_id, type=RunEventType.TOOL_REQUESTED, payload={
            "call_id": "c1", "tool_name": "read_file", "arguments": {"path": "a.py"},
        }, execution_id="e1")
        runtime.record(run_id=run_id, type=RunEventType.TOOL_STARTED, payload={
            "call_id": "c1", "tool_name": "read_file",
        }, execution_id="e1")
        runtime.record(run_id=run_id, type=RunEventType.TOOL_COMPLETED, payload={
            "call_id": "c1", "tool_name": "read_file", "content": "ok", "metadata": {},
        }, execution_id="e1")

        assert _pump_until(qapp, lambda: any(i["status"] == "ok" for i in seen), timeout=5.0)
        matching = [i for i in seen if i["id"] == "tool:e1:c1"]
        # Coalescing means fewer emissions than raw canonical events for
        # this id are possible, but the LAST one seen must be the final ok.
        assert matching[-1]["status"] == "ok"
        # No stale "running" arrives AFTER the final "ok" (update-in-place,
        # not a duplicate append).
        ok_index = next(i for i, item in enumerate(matching) if item["status"] == "ok")
        assert all(item["status"] == "ok" for item in matching[ok_index:])
    finally:
        streamer.request_stop()
        streamer.wait(2000)


def test_request_stop_drains_buffered_items_before_exit(qapp, tmp_path):
    runtime, run_id = _running_runtime(tmp_path)
    seen = []
    # A slow flush cadence so an item recorded just before stop is still
    # sitting in the buffer, unflushed, when request_stop() is called.
    streamer = ActivityStreamer(runtime, run_id, max_items_per_second=1)
    streamer.activity.connect(lambda item: seen.append(item))
    streamer.start()
    try:
        # Let the streamer's tail actually attach before recording, so the
        # event isn't missed by a lost-event race at startup.
        time.sleep(0.05)
        record_agent_activity(runtime, run_id, role="fix", kind="stage", title="Son deneme", status="ok")
        time.sleep(0.05)  # well inside the 1s flush window: still buffered
    finally:
        streamer.request_stop()
        assert streamer.wait(2000)
    # The final drain's signal is delivered via a queued cross-thread
    # connection -- it sits in the main thread's event queue until pumped,
    # even though the emitting QThread has already fully exited.
    for _ in range(50):
        qapp.processEvents()
        if any(item["title"] == "Son deneme" for item in seen):
            break
        time.sleep(0.01)
    assert any(item["title"] == "Son deneme" for item in seen)


def test_sender_is_retained_until_queued_final_activity_is_delivered(qapp, tmp_path):
    runtime, run_id = _running_runtime(tmp_path)
    record_agent_activity(runtime, run_id, role="reviewer", kind="stage", title="İnceleme tamamlandı", status="ok")
    seen = []
    streamer = ActivityStreamer(runtime, run_id, max_items_per_second=1)
    streamer_ref = weakref.ref(streamer)
    start_activity_streamer(streamer, seen.append)

    streamer.request_stop()
    assert streamer.wait(2000)
    del streamer

    # Final signals are already emitted but still queued for the main thread.
    # Dropping _active's reference must not destroy the sender/lose the item.
    assert streamer_ref() is not None
    assert _pump_until(
        qapp,
        lambda: any(item["title"] == "İnceleme tamamlandı" for item in seen)
        and streamer_ref() is None,
    )
    item = next(item for item in seen if item["title"] == "İnceleme tamamlandı")
    assert item["role"] == "reviewer" and item["status"] == "ok"
