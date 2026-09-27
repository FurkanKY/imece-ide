"""agent.activity (Aşama 3 F1) must be a purely advisory canonical event:
projector/readmodels ignore it, RunStore durably accepts it, and
RunCompletionGate's evidence re-derivation is unaffected by its presence.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from run_runtime import RunEventType, RunRuntime, RunStatus, RunStore
from run_runtime.agent_activity import record_agent_activity
from run_runtime.completion import RunCompletionGate
from run_runtime.errors import EventValidationError
from run_runtime.projector import project_run


def _runtime(tmp_path):
    return RunRuntime(RunStore(tmp_path / "runs.sqlite3"))


def _running_run(runtime):
    task = runtime.create_task(project_root="/tmp/project", prompt="do it")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return run.run_id


def test_projector_leaves_run_record_untouched_by_agent_activity(tmp_path):
    runtime = _runtime(tmp_path)
    run_id = _running_run(runtime)
    before = runtime.get_run(run_id)

    record_agent_activity(runtime, run_id, role="worker", kind="tool", title="Okundu: a.py", status="ok")

    after = runtime.get_run(run_id)
    # Only last_event_seq/updated_at-shaped bookkeeping may differ; status/
    # phase (the fields RunCompletionGate and readmodels actually branch on)
    # must be byte-identical -- project_run's dict-dispatch has no handler
    # for agent.activity, so it returns `current` unchanged.
    assert after.status == before.status
    assert after.phase == before.phase


def test_project_run_pure_function_ignores_agent_activity_event(tmp_path):
    runtime = _runtime(tmp_path)
    run_id = _running_run(runtime)
    current = runtime.get_run(run_id)
    event = record_agent_activity(runtime, run_id, role="worker", kind="tool", title="x", status="ok")
    projected = project_run(current, event)
    assert projected is current  # unchanged object, not just equal


def test_run_store_durably_accepts_agent_activity(tmp_path):
    runtime = _runtime(tmp_path)
    run_id = _running_run(runtime)
    event = record_agent_activity(
        runtime, run_id, role="verification", kind="check", title="Kontrol: unit",
        status="running", detail="python3 -m pytest -q",
    )
    page = runtime.events(run_id, after_seq=0)
    types = [e.type for e in page.events]
    assert RunEventType.AGENT_ACTIVITY in types
    stored = next(e for e in page.events if e.type == RunEventType.AGENT_ACTIVITY)
    assert stored.payload["title"] == "Kontrol: unit"
    assert stored.event_id == event.event_id


def test_record_agent_activity_rejects_unknown_vocabulary(tmp_path):
    runtime = _runtime(tmp_path)
    run_id = _running_run(runtime)
    with pytest.raises(EventValidationError):
        record_agent_activity(runtime, run_id, role="bogus", kind="tool", title="x", status="ok")
    with pytest.raises(EventValidationError):
        record_agent_activity(runtime, run_id, role="worker", kind="bogus", title="x", status="ok")
    with pytest.raises(EventValidationError):
        record_agent_activity(runtime, run_id, role="worker", kind="tool", title="x", status="bogus")


def test_completion_gate_evidence_unaffected_by_interleaved_agent_activity(tmp_path):
    """Reproduces the exact evidence chain RunCompletionGate.complete_reviewed
    re-derives (verification PASS -> review APPROVED for the same
    diff_sha256), with agent.activity events interleaved throughout -- the
    gate must settle exactly as it would without them."""
    runtime = _runtime(tmp_path)
    run_id = _running_run(runtime)

    record_agent_activity(runtime, run_id, role="worker", kind="tool", title="noise 1", status="running")

    diff_sha = "a" * 64
    runtime.record(run_id=run_id, type=RunEventType.EXECUTION_STARTED, payload={"task": "t"}, execution_id="e1")
    runtime.record(run_id=run_id, type=RunEventType.EXECUTION_COMPLETED, payload={
        "final_text": "", "model_turns": 1, "tool_calls": 0, "tool_errors": 0,
        "input_tokens": 1, "output_tokens": 1, "cost_usd": None,
    }, execution_id="e1")

    record_agent_activity(runtime, run_id, role="verification", kind="check", title="noise 2", status="ok")

    runtime.record(run_id=run_id, type=RunEventType.VERIFICATION_STARTED,
                    payload={"verification_id": "v1", "plan_id": "p1", "check_count": 1})
    runtime.record(run_id=run_id, type=RunEventType.VERIFICATION_COMPLETED, payload={
        "verification_id": "v1", "plan_id": "p1", "status": "pass", "duration_ms": 1,
        "counts": {"pass": 1, "fail": 0, "timeout": 0, "error": 0, "total": 1},
    })

    record_agent_activity(runtime, run_id, role="reviewer", kind="note", title="noise 3", status="info")

    runtime.record(run_id=run_id, type=RunEventType.REVIEW_STARTED, payload={"review_id": "r1"})
    runtime.record(run_id=run_id, type=RunEventType.REVIEW_COMPLETED, payload={
        "review_id": "r1", "verdict": "APPROVED", "note": "ok", "summary": "ok", "findings": [],
        "repository_fingerprint": "fp", "diff_sha256": diff_sha,
        "verification_id": "v1", "verification_status": "pass",
    })

    record_agent_activity(runtime, run_id, role="system", kind="note", title="noise 4", status="info")

    gate = RunCompletionGate(runtime, settlement="await_user")
    gate.complete_reviewed(run_id, verification_id="v1", review_id="r1", current_diff_sha256=diff_sha)

    final = runtime.get_run(run_id)
    assert final.status == RunStatus.WAITING_USER
