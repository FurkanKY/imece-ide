"""Restart-safe, read-only projection of persisted native-agent run history."""

from run_runtime.events import RunEventType
from run_runtime.events import RunEventSpec
from run_runtime.service import RunRuntime
from run_runtime.store import RunStore
from webhost.run_history import get_history, list_history, retry_source

import pytest


def test_history_survives_runtime_restart_and_excludes_other_run_kinds(tmp_path):
    root = str(tmp_path.resolve())
    db = tmp_path / "history.sqlite3"
    runtime = RunRuntime(RunStore(db))
    task = runtime.create_task(project_root=root, prompt="Inspect without changing files")
    run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "provider-x"})
    runtime.record(run_id=run.run_id, type=RunEventType.PROPOSAL_READY,
                   payload={"execution_id": "exec-a", "agent_message": "Reviewed", "verification": {"outcome": "pass"}})
    legacy_task = runtime.create_task(project_root=root, prompt="legacy")
    runtime.create_run(task_id=legacy_task.task_id, routing={"coder": "provider-x"})
    runtime.create_task(project_root=str(tmp_path / "elsewhere"), prompt="secret elsewhere")

    # Simulate process restart: reopen the same on-disk store with a new runtime.
    reopened = RunRuntime(RunStore(db))
    rows = list_history(reopened, root)
    detail = get_history(reopened, root, run.run_id)
    assert len(rows) == 1
    assert rows[0]["taskId"] == task.task_id
    assert rows[0]["readOnly"] is True
    assert rows[0]["retryAvailable"] is False
    assert detail["runId"] == run.run_id
    assert detail["task"] == "Inspect without changing files"
    assert detail["providerId"] == "provider-x"
    assert detail["evidence"]["execution_id"] == "exec-a"
    assert detail["proposals"] == []
    assert detail["checkpointId"] is None
    assert detail["retryAvailable"] is False
    assert detail["totals"] == {"latency_s": None, "tokens": None, "cost_usd": None}
    assert get_history(reopened, str(tmp_path / "elsewhere"), run.run_id) is None


def test_retry_source_is_bounded_terminal_same_provider_chain_and_keeps_full_prompt(tmp_path):
    root = str(tmp_path.resolve())
    runtime = RunRuntime(RunStore(tmp_path / "retry.sqlite3"))
    prompt = "x" * 9_000
    task = runtime.create_task(project_root=root, prompt=prompt)
    first = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    runtime.record(run_id=first.run_id, type=RunEventType.RUN_STARTED, payload={})
    assert retry_source(runtime, root, first.run_id) is None
    runtime.record(run_id=first.run_id, type=RunEventType.EXECUTION_STARTED,
                   payload={"role": "worker"}, execution_id="exec-old")
    runtime.record(run_id=first.run_id, type=RunEventType.PROPOSAL_READY,
                   payload={"proposals": [{"path": "x", "new": "secret"}]})
    runtime.record(run_id=first.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
    assert retry_source(runtime, root, first.run_id) is None  # old proposal authority

    fresh = RunRuntime(RunStore(tmp_path / "retry-fresh.sqlite3"))
    fresh_task = fresh.create_task(project_root=root, prompt=prompt)
    failed = fresh.create_run(task_id=fresh_task.task_id, routing={"agent_provider": "p"})
    fresh.record(run_id=failed.run_id, type=RunEventType.RUN_STARTED, payload={})
    fresh.record(run_id=failed.run_id, type=RunEventType.EXECUTION_STARTED,
                 payload={"role": "worker"}, execution_id="exec-failed")
    fresh.record(run_id=failed.run_id, type=RunEventType.EXECUTION_FAILED,
                 payload={}, execution_id="exec-failed")
    fresh.record(run_id=failed.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
    assert retry_source(fresh, root, failed.run_id)["task"] == prompt
    assert list_history(fresh, root)[0]["retryAvailable"] is True
    assert get_history(fresh, root, failed.run_id)["retryAvailable"] is True

    second = runtime.create_run(task_id=task.task_id, attempt=2, retry_of_run_id=first.run_id,
                                routing={"agent_provider": "p"})
    runtime.record(run_id=second.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=second.run_id, type=RunEventType.EXECUTION_STARTED,
                   payload={"role": "worker"}, execution_id="exec-2")
    runtime.record(run_id=second.run_id, type=RunEventType.EXECUTION_FAILED,
                   payload={}, execution_id="exec-2")
    runtime.record(run_id=second.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
    assert retry_source(runtime, root, first.run_id) is None
    assert retry_source(runtime, root, second.run_id)["attempt"] == 2
    third = runtime.create_run(task_id=task.task_id, attempt=3, retry_of_run_id=second.run_id,
                               routing={"agent_provider": "other"})
    runtime.record(run_id=third.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=third.run_id, type=RunEventType.EXECUTION_STARTED,
                   payload={"role": "worker"}, execution_id="exec-3")
    runtime.record(run_id=third.run_id, type=RunEventType.EXECUTION_FAILED,
                   payload={}, execution_id="exec-3")
    runtime.record(run_id=third.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
    assert retry_source(runtime, root, third.run_id) is None
    assert retry_source(runtime, str(tmp_path / "elsewhere"), second.run_id) is None


def test_retry_source_rejects_interrupted_gapped_branched_and_overbound_history(tmp_path):
    root = str(tmp_path.resolve())
    interrupted = RunRuntime(RunStore(tmp_path / "interrupted.sqlite3"))
    task = interrupted.create_task(project_root=root, prompt="task")
    run = interrupted.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    interrupted.record(run_id=run.run_id, type=RunEventType.RUN_INTERRUPTED, payload={})
    assert retry_source(interrupted, root, run.run_id) is None

    for name, attempts in (("gap", (1, 3)), ("duplicate", (1, 1)), ("branch", (1, 2, 2))):
        runtime = RunRuntime(RunStore(tmp_path / f"{name}.sqlite3"))
        task = runtime.create_task(project_root=root, prompt="task")
        previous = None
        created = []
        for index, attempt in enumerate(attempts):
            item = runtime.create_run(task_id=task.task_id, attempt=attempt,
                retry_of_run_id=previous.run_id if attempt > 1 and index else None,
                routing={"agent_provider": "p"})
            runtime.record(run_id=item.run_id, type=RunEventType.RUN_STARTED, payload={})
            runtime.record(run_id=item.run_id, type=RunEventType.EXECUTION_STARTED,
                           payload={"role": "worker"}, execution_id=f"exec-{index}")
            runtime.record(run_id=item.run_id, type=RunEventType.EXECUTION_FAILED,
                           payload={}, execution_id=f"exec-{index}")
            runtime.record(run_id=item.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
            created.append(item)
            previous = item
        assert retry_source(runtime, root, created[-1].run_id) is None

    runtime = RunRuntime(RunStore(tmp_path / "bounded.sqlite3"))
    task = runtime.create_task(project_root=root, prompt="task")
    first = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    runtime.record(run_id=first.run_id, type=RunEventType.RUN_FAILED, payload={"error_code": "failed"})
    for attempt in range(2, 131):
        runtime.create_run(task_id=task.task_id, attempt=attempt, retry_of_run_id=first.run_id,
                           routing={"agent_provider": "p"})
    assert retry_source(runtime, root, first.run_id) is None


def test_history_suppresses_receipt_closed_by_rejection_or_resume(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "history.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record(run_id=run.run_id, type=RunEventType.PROPOSAL_READY,
                   payload={"execution_id": "old", "agent_message": "old receipt"})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_WAITING_USER, payload={})
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_RESUMED, payload={})
    detail = get_history(runtime, str(tmp_path), run.run_id)
    assert detail["evidence"] is None
    assert detail["proposals"] == []

    rejected = RunRuntime(RunStore(tmp_path / "rejected.sqlite3"))
    rejected_task = rejected.create_task(project_root=str(tmp_path), prompt="reject task")
    rejected_run = rejected.create_run(task_id=rejected_task.task_id, routing={"agent_provider": "p"})
    rejected.record(run_id=rejected_run.run_id, type=RunEventType.RUN_STARTED, payload={})
    rejected.record(run_id=rejected_run.run_id, type=RunEventType.PROPOSAL_READY,
                    payload={"execution_id": "rejected-proof", "agent_message": "not current"})
    rejected.record(run_id=rejected_run.run_id, type=RunEventType.RUN_WAITING_USER, payload={})
    rejected.record(run_id=rejected_run.run_id, type=RunEventType.PROPOSAL_REJECTED, payload={})
    assert get_history(rejected, str(tmp_path), rejected_run.run_id)["evidence"] is None


def test_history_reads_past_event_pages_and_fails_closed_after_scan_limit(tmp_path):
    runtime = RunRuntime(RunStore(tmp_path / "history.sqlite3"))
    task = runtime.create_task(project_root=str(tmp_path), prompt="task")
    run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
    specs = [RunEventSpec(RunEventType.RUN_PHASE_CHANGED, {"phase": "planning"}) for _ in range(205)]
    specs.append(RunEventSpec(RunEventType.PROPOSAL_READY, {"execution_id": "late", "agent_message": "latest"}))
    runtime.record_many(run_id=run.run_id, specs=specs)
    detail = get_history(runtime, str(tmp_path), run.run_id)
    assert detail["evidence"]["execution_id"] == "late"
    assert detail["historyTruncated"] is False

    over_limit = RunRuntime(RunStore(tmp_path / "over.sqlite3"))
    large_task = over_limit.create_task(project_root=str(tmp_path), prompt="large")
    large_run = over_limit.create_run(task_id=large_task.task_id, routing={"agent_provider": "p"})
    long_specs = [RunEventSpec(RunEventType.RUN_PHASE_CHANGED, {"phase": "planning"}) for _ in range(2_001)]
    long_specs.append(RunEventSpec(RunEventType.PROPOSAL_READY, {"execution_id": "must-not-be-stale", "agent_message": "latest"}))
    over_limit.record_many(run_id=large_run.run_id, specs=long_specs)
    truncated = get_history(over_limit, str(tmp_path), large_run.run_id)
    assert truncated["evidence"] is None
    assert truncated["historyTruncated"] is True


def test_history_suppresses_receipt_after_terminal_boundaries_and_fails_closed_on_corrupt_runtime(tmp_path):
    for terminal in (RunEventType.RUN_CANCELLED, RunEventType.RUN_FAILED,
                     RunEventType.RUN_INTERRUPTED, RunEventType.CHECKPOINT_RESTORED,
                     RunEventType.PROPOSAL_APPLIED):
        runtime = RunRuntime(RunStore(tmp_path / f"{terminal.value}.sqlite3"))
        task = runtime.create_task(project_root=str(tmp_path), prompt="task")
        run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
        if terminal is RunEventType.PROPOSAL_APPLIED:
            runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
        runtime.record(run_id=run.run_id, type=RunEventType.PROPOSAL_READY,
                       payload={"execution_id": "old", "agent_message": "receipt"})
        if terminal is RunEventType.PROPOSAL_APPLIED:
            runtime.record(run_id=run.run_id, type=RunEventType.RUN_WAITING_USER, payload={})
        if terminal is RunEventType.CHECKPOINT_RESTORED:
            runtime.record(run_id=run.run_id, type=terminal, payload={"checkpoint_id": "cp"})
        else:
            runtime.record(run_id=run.run_id, type=terminal, payload={"error_code": "failed"} if terminal is RunEventType.RUN_FAILED else {})
        assert get_history(runtime, str(tmp_path), run.run_id)["evidence"] is None

    class CorruptRuntime:
        def get_run(self, run_id):
            raise ValueError("corrupt")
    assert get_history(CorruptRuntime(), str(tmp_path), "run_corrupt") is None

    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_runtime = RunRuntime(RunStore(corrupt_db))
    corrupt_task = corrupt_runtime.create_task(project_root=str(tmp_path), prompt="corrupt routing")
    corrupt_run = corrupt_runtime.create_run(task_id=corrupt_task.task_id, run_id="run_corrupt_row",
                                              routing={"agent_provider": "p"})
    import sqlite3
    with sqlite3.connect(corrupt_db) as connection:
        connection.execute("UPDATE runs SET routing_json = ? WHERE run_id = ?", ("{bad-json", corrupt_run.run_id))
    assert get_history(RunRuntime(RunStore(corrupt_db)), str(tmp_path), corrupt_run.run_id) is None

    malformed = RunRuntime(RunStore(tmp_path / "malformed.sqlite3"))
    malformed_task = malformed.create_task(project_root=str(tmp_path), prompt="invalid provider")
    malformed.create_run(task_id=malformed_task.task_id, routing={"agent_provider": ""})
    assert list_history(malformed, str(tmp_path)) == []


def test_run_bridge_reopens_same_database_after_registry_restart_and_live_wins_same_id(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from run_runtime.models import RunStatus
    from webhost import state
    from webhost.api import run as run_api
    from webhost.bridge import BridgeError
    from webhost.run_registry import RunRegistry, RunSlot

    root = str(tmp_path.resolve())
    db = tmp_path / "bridge.sqlite3"
    original_runtime = RunRuntime(RunStore(db))
    task = original_runtime.create_task(project_root=root, prompt="restart me")
    historical_run = original_runtime.create_run(task_id=task.task_id, run_id="run_reopened",
                                                  routing={"agent_provider": "agent-x"})
    other_task = original_runtime.create_task(project_root=str(tmp_path / "private"), prompt="do not leak")
    original_runtime.create_run(task_id=other_task.task_id, run_id="run_private",
                                routing={"agent_provider": "agent-x"})
    runtime_after_restart = RunRuntime(RunStore(db))
    monkeypatch.setattr(state, "_active", None)
    state.set_project(root)
    monkeypatch.setattr(state, "_run_runtime", runtime_after_restart)
    monkeypatch.setattr(run_api, "_run_registry", RunRegistry())

    listed = run_api._list_runs({}, None)
    assert listed["historyUnavailable"] is False
    assert len(listed["runs"]) == 1 and listed["runs"][0]["runId"] == historical_run.run_id
    detail = run_api._get_run({"runId": historical_run.run_id}, None)
    assert detail["readOnly"] is True and detail["task"] == "restart me"
    with pytest.raises(BridgeError):
        run_api._get_run({"runId": "run_private"}, None)

    # Reading an old record does not attach a worker/lease or grant mutation
    # authority. Exercise the actual bridge handlers, not only DTO flags.
    monkeypatch.setattr(run_api, "_active", {"run_id": None, "worker": None,
        "coordinator": None, "engine": "legacy", "proposals": []})
    source = tmp_path / "source.txt"
    source.write_text("unchanged", encoding="utf-8")
    mutations = []
    monkeypatch.setattr(runtime_after_restart, "record", lambda **kwargs: mutations.append(kwargs))
    for handler, params in (
        (run_api._cancel, {"runId": historical_run.run_id}),
        (run_api._follow_up, {"runId": historical_run.run_id, "feedback": "do not resume"}),
        (run_api._apply, {"runId": historical_run.run_id, "paths": ["source.txt"]}),
        (run_api._reject, {"runId": historical_run.run_id}),
    ):
        with pytest.raises(BridgeError):
            handler(params, None)
    assert mutations == []
    assert source.read_text(encoding="utf-8") == "unchanged"
    assert run_api._run_registry.slots() == ()
    assert run_api._run_registry.reserve(root) is True
    run_api._run_registry.release(root)

    class Coordinator:
        def get_run(self):
            return SimpleNamespace(status=RunStatus.RUNNING)

    # A current process-owned DTO remains authoritative when IDs collide.
    run_api._run_registry.add(RunSlot(run_id=historical_run.run_id, task_id=task.task_id,
        project_root=root, provider_id="live-provider", coordinator=Coordinator(), task="live task"))
    live = run_api._get_run({"runId": historical_run.run_id}, None)
    assert live["task"] == "live task" and live["providerId"] == "live-provider"
    assert not live.get("readOnly", False)
    monkeypatch.setattr(state, "get_run_runtime", lambda: (_ for _ in ()).throw(ValueError("unavailable")))
    degraded = run_api._list_runs({}, None)
    assert degraded["historyUnavailable"] is True
    assert any(item["runId"] == historical_run.run_id and item["task"] == "live task" for item in degraded["runs"])
