"""run_runtime.recovery.recover_running_runs — MUHAFAZAKÂR kurtarma testleri.

recover_running_runs artık RunStore'u DEĞİL, RunRuntime'ı kabul eder: tüm
kanonik yazımlar runtime.record() (tek kanonik yazma noktası) üzerinden
gider, böylece kurtarma event'i de canlı DurableEventTail abonelerine
COMMIT'ten SONRA bildirilir (bkz. test_recovery_emits_live_notification_...).
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.errors import RunStoreError  # noqa: E402
from run_runtime.events import RunEventSpec, RunEventType  # noqa: E402
from run_runtime.models import RunPhase, RunStatus  # noqa: E402
from run_runtime.recovery import recover_running_runs  # noqa: E402
from run_runtime.service import RunRuntime  # noqa: E402
from run_runtime.store import RunStore  # noqa: E402


@pytest.fixture
def runtime(tmp_path) -> RunRuntime:
    return RunRuntime(RunStore(tmp_path / "runtime.sqlite3"))


def _seed_run(runtime, *, task_id, run_id):
    task = runtime.store.create_task(project_root="/tmp", prompt="p", task_id=task_id)
    return runtime.store.create_run(task_id=task.task_id, run_id=run_id)


def test_running_run_is_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ("run_a",)
    assert report.skipped_changed_run_ids == ()
    updated = runtime.get_run("run_a")
    assert updated.status == RunStatus.INTERRUPTED
    assert updated.phase == RunPhase.ERROR


def test_recovery_event_source_and_payload(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    recover_running_runs(runtime)

    page = runtime.store.events("run_a", limit=200)
    last = page.events[-1]
    assert last.type == "run.interrupted"
    assert last.source == "recovery"
    assert last.payload == {"reason": "process_restart"}


def test_second_recovery_scan_is_idempotent(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    first = recover_running_runs(runtime)
    assert first.interrupted_run_ids == ("run_a",)
    before = runtime.get_run("run_a")
    before_event_count = len(runtime.store.events("run_a", limit=200).events)

    second = recover_running_runs(runtime)

    assert second.interrupted_run_ids == ()
    assert second.skipped_changed_run_ids == ()
    after = runtime.get_run("run_a")
    assert after == before  # hiçbir alan değişmedi
    after_event_count = len(runtime.store.events("run_a", limit=200).events)
    assert after_event_count == before_event_count  # yinelenen run.interrupted YOK


def test_waiting_user_run_is_not_touched(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_WAITING_USER, payload={})
    before = runtime.get_run("run_a")

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ()
    assert report.skipped_changed_run_ids == ()
    after = runtime.get_run("run_a")
    assert after == before


def test_created_run_is_not_touched(runtime):
    _seed_run(runtime, task_id="task_a", run_id="run_a")
    before = runtime.get_run("run_a")

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ()
    after = runtime.get_run("run_a")
    assert after == before


@pytest.mark.parametrize(
    "terminal_event",
    [
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
        RunEventType.RUN_INTERRUPTED,
    ],
    ids=["succeeded", "failed", "cancelled", "interrupted"],
)
def test_terminal_runs_are_not_touched(runtime, terminal_event):
    task_id = f"task_{terminal_event.name.lower()}"
    run_id = f"run_{terminal_event.name.lower()}"
    run = _seed_run(runtime, task_id=task_id, run_id=run_id)
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(run_id=run.run_id, type=terminal_event, payload={})
    before = runtime.get_run(run.run_id)

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ()
    assert report.skipped_changed_run_ids == ()
    after = runtime.get_run(run.run_id)
    assert after == before


def test_stale_sequence_candidate_is_conservatively_skipped(runtime, monkeypatch):
    """Tarama sonrası (candidate.last_event_seq okunduktan sonra) ama gerçek
    kurtarma yazımından ÖNCE Run başka bir üretici tarafından ilerletilirse,
    kör bir yeniden deneme YAPILMAZ — yalnızca muhafazakâr biçimde atlanır."""
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    original_append = runtime.store.append_event

    def racing_append_event(*args, **kwargs):
        # recover_running_runs, candidate.last_event_seq'i (tarama anında)
        # zaten okudu. Gerçek kurtarma çağrısından (expected_last_event_seq
        # ile) HEMEN önce, "hâlâ aktif bir üretici" tarafından ilerletilmiş
        # gibi davranmak için burada GERÇEK bir yarış (event ekleme) simüle
        # edilir.
        original_append(
            run_id=run.run_id, type=RunEventType.RUN_PHASE_CHANGED, payload={"phase": "planning"},
        )
        return original_append(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "append_event", racing_append_event)

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ()
    assert report.skipped_changed_run_ids == ("run_a",)
    after = runtime.get_run("run_a")
    assert after.status == RunStatus.RUNNING  # hâlâ RUNNING, INTERRUPTED DEĞİL
    assert after.phase == RunPhase.PLANNING  # yarışan üreticinin değişikliği korunuyor
    assert after.last_event_seq == 2


def test_unexpected_store_failure_is_not_swallowed(runtime, monkeypatch):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    def boom(*args, **kwargs):
        raise RunStoreError("simulated failure")

    monkeypatch.setattr(runtime.store, "append_event", boom)

    with pytest.raises(RunStoreError):
        recover_running_runs(runtime)


def test_multiple_running_runs_handled_independently(runtime):
    run_a = _seed_run(runtime, task_id="task_a", run_id="run_a")
    run_b = _seed_run(runtime, task_id="task_b", run_id="run_b")
    runtime.store.append_event(run_id=run_a.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(run_id=run_b.run_id, type=RunEventType.RUN_STARTED, payload={})

    report = recover_running_runs(runtime)

    assert set(report.interrupted_run_ids) == {"run_a", "run_b"}
    assert runtime.get_run("run_a").status == RunStatus.INTERRUPTED
    assert runtime.get_run("run_b").status == RunStatus.INTERRUPTED


def test_unfinished_review_is_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(
        run_id=run.run_id, type=RunEventType.REVIEW_STARTED,
        payload={"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer",
    )

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ("run_a",)
    events = runtime.events("run_a", limit=200).events
    review_interrupted = [event for event in events if event.type == RunEventType.REVIEW_INTERRUPTED]
    assert len(review_interrupted) == 1
    assert review_interrupted[0].payload == {"review_id": "rev-1", "reason": "process_restart"}
    types = [event.type for event in events]
    assert types.index(RunEventType.REVIEW_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)
    assert runtime.get_run("run_a").status == RunStatus.INTERRUPTED


def test_unfinished_reviewer_tool_and_review_are_both_interrupted_in_order(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.REVIEW_STARTED, {"review_id": "rev-1"},
            correlation_id="rev-1", source="reviewer",
        ),
        RunEventSpec(
            RunEventType.TOOL_REQUESTED,
            {"call_id": "c1", "tool_name": "search_text", "arguments": {}},
            turn_id="t1", item_id="i1", correlation_id="rev-1", source="reviewer",
        ),
        RunEventSpec(
            RunEventType.TOOL_STARTED,
            {"call_id": "c1", "tool_name": "search_text"},
            turn_id="t1", item_id="i1", correlation_id="rev-1", source="reviewer",
        ),
    ))

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ("run_a",)
    events = runtime.events("run_a", limit=200).events
    types = [event.type for event in events]
    assert types.index(RunEventType.TOOL_INTERRUPTED) < types.index(RunEventType.REVIEW_INTERRUPTED)
    assert types.index(RunEventType.REVIEW_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)


def test_completed_review_is_not_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(RunEventType.REVIEW_STARTED, {"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer"),
        RunEventSpec(RunEventType.REVIEW_COMPLETED, {
            "review_id": "rev-1", "verdict": "APPROVED", "note": "ok", "summary": "ok",
            "findings": [], "repository_fingerprint": "a" * 64, "diff_sha256": "b" * 64,
            "verification_id": None, "verification_status": None,
        }, correlation_id="rev-1", source="reviewer"),
    ))

    recover_running_runs(runtime)

    events = runtime.events("run_a", limit=200).events
    assert RunEventType.REVIEW_INTERRUPTED not in [event.type for event in events]
    assert runtime.get_run("run_a").status == RunStatus.INTERRUPTED  # run itself still settles


def test_review_recovery_is_idempotent_on_repeated_scan(runtime):
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(
        run_id=run.run_id, type=RunEventType.REVIEW_STARTED,
        payload={"review_id": "rev-1"}, correlation_id="rev-1", source="reviewer",
    )

    first = recover_running_runs(runtime)
    assert first.interrupted_run_ids == ("run_a",)
    before = len(runtime.events("run_a", limit=200).events)

    second = recover_running_runs(runtime)
    assert second.interrupted_run_ids == ()
    after = len(runtime.events("run_a", limit=200).events)
    assert after == before


def test_recovery_emits_live_notification_through_event_bus(runtime):
    """recover_running_runs, RunRuntime.record() (ve dolayısıyla EventBus.publish())
    üzerinden yazmalıdır. Bunu kanıtlamak için next_page() ZATEN bloklu
    beklerken kurtarmayı tetikleriz: tüketici yalnızca CANLI bir bildirim
    varsa HIZLI biçimde uyanabilir. recover_running_runs store.append_event'i
    DOĞRUDAN çağırsaydı (publish YOK), tüketici 10 saniyelik zaman aşımının
    TAMAMINI beklerdi ve bu test BAŞARISIZ olurdu."""
    run = _seed_run(runtime, task_id="task_a", run_id="run_a")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})

    with runtime.open_event_tail(run.run_id) as tail:
        # Var olan (RUN_STARTED) geçmişi tüket ki yalnızca kurtarma
        # event'inin CANLI bildirimini izole biçimde görelim.
        first_page = tail.next_page(timeout=1)
        assert first_page is not None
        assert tail.cursor == 1

        entered_wait = threading.Event()
        original_wait = tail._subscription.wait

        def patched_wait(timeout=None):
            entered_wait.set()
            return original_wait(timeout=timeout)

        tail._subscription.wait = patched_wait

        result = {}

        def consumer():
            result["page"] = tail.next_page(timeout=10)

        t = threading.Thread(target=consumer)
        start = time.monotonic()
        t.start()
        assert entered_wait.wait(timeout=5), "tüketici zamanında beklemeye girmedi"
        time.sleep(0.05)  # gerçekten Condition.wait() içinde olduğundan emin ol

        report = recover_running_runs(runtime)
        assert report.interrupted_run_ids == ("run_a",)

        t.join(timeout=5)
        elapsed = time.monotonic() - start
        assert not t.is_alive()
        assert elapsed < 5.0, "CANLI bildirim gelmedi; 10sn zaman aşımının tamamı beklendi"

        page = result["page"]
        assert page is not None
        assert [e.type for e in page.events] == ["run.interrupted"]

    final = runtime.get_run("run_a")
    assert final.status == RunStatus.INTERRUPTED


# ==================== 3H: bounded Fix Loop recovery ====================


def _seed_running_fix_loop_run(runtime, *, task_id="task_fix", run_id="run_fix"):
    run = _seed_run(runtime, task_id=task_id, run_id=run_id)
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    return run


def _append_fix_loop_started(runtime, run_id, fix_loop_id):
    runtime.record(
        run_id=run_id, type=RunEventType.FIX_LOOP_STARTED, payload={"fix_loop_id": fix_loop_id},
        correlation_id=fix_loop_id, source="fix_loop",
    )


def _append_fix_loop_terminal(runtime, run_id, fix_loop_id, event_type, payload=None):
    runtime.record(
        run_id=run_id, type=event_type, payload=payload or {"fix_loop_id": fix_loop_id, "reason": "x"},
        correlation_id=fix_loop_id, source="fix_loop",
    )


def _append_fix_attempt_started(runtime, run_id, fix_loop_id, fix_attempt_id, attempt_index, worker_execution_id):
    runtime.record(
        run_id=run_id, type=RunEventType.FIX_ATTEMPT_STARTED,
        payload={
            "fix_loop_id": fix_loop_id, "fix_attempt_id": fix_attempt_id, "attempt_index": attempt_index,
            "trigger_kind": "verification_fail", "worker_execution_id": worker_execution_id,
            "before_diff_sha256": "a" * 64,
        },
        correlation_id=fix_loop_id, source="fix_loop",
    )


def _append_fix_attempt_completed(runtime, run_id, fix_loop_id, fix_attempt_id, attempt_index, worker_execution_id):
    runtime.record(
        run_id=run_id, type=RunEventType.FIX_ATTEMPT_COMPLETED,
        payload={
            "fix_loop_id": fix_loop_id, "fix_attempt_id": fix_attempt_id, "attempt_index": attempt_index,
            "worker_execution_id": worker_execution_id, "before_diff_sha256": "a" * 64,
            "after_diff_sha256": "b" * 64, "changed": True,
        },
        correlation_id=fix_loop_id, source="fix_loop",
    )


def test_unfinished_fix_attempt_is_interrupted(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == (run.run_id,)
    events = runtime.events(run.run_id, limit=200).events
    attempt_interrupted = [e for e in events if e.type == RunEventType.FIX_ATTEMPT_INTERRUPTED]
    assert len(attempt_interrupted) == 1
    assert attempt_interrupted[0].payload == {
        "fix_loop_id": "fix-1", "fix_attempt_id": "att-1", "attempt_index": 1,
        "worker_execution_id": "exec-fix-1", "reason": "process_restart", "outcome_unknown": True,
    }


def test_unfinished_fix_loop_is_interrupted(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    _append_fix_attempt_completed(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    loop_interrupted = [e for e in events if e.type == RunEventType.FIX_LOOP_INTERRUPTED]
    assert len(loop_interrupted) == 1
    assert loop_interrupted[0].payload == {"fix_loop_id": "fix-1", "reason": "process_restart"}
    assert RunEventType.FIX_ATTEMPT_INTERRUPTED not in [e.type for e in events]


def test_ordering_tool_then_fix_attempt_then_fix_loop_then_run(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    runtime.record(
        run_id=run.run_id, type=RunEventType.TOOL_REQUESTED,
        payload={"call_id": "c", "tool_name": "search_text", "arguments": {}},
        execution_id="exec-fix-1", turn_id="t", item_id="i", correlation_id="exec-fix-1", source="native_agent",
    )
    runtime.record(
        run_id=run.run_id, type=RunEventType.TOOL_STARTED,
        payload={"call_id": "c", "tool_name": "search_text"},
        execution_id="exec-fix-1", turn_id="t", item_id="i", correlation_id="exec-fix-1", source="native_agent",
    )

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    types = [e.type for e in events]
    assert types.index(RunEventType.TOOL_INTERRUPTED) < types.index(RunEventType.FIX_ATTEMPT_INTERRUPTED)
    assert types.index(RunEventType.FIX_ATTEMPT_INTERRUPTED) < types.index(RunEventType.FIX_LOOP_INTERRUPTED)
    assert types.index(RunEventType.FIX_LOOP_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)


def test_ordering_verification_then_fix_loop(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    _append_fix_attempt_completed(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    runtime.record(
        run_id=run.run_id, type=RunEventType.VERIFICATION_STARTED,
        payload={"verification_id": "ver-1", "plan_id": "plan", "check_count": 1},
        correlation_id="ver-1", source="verification",
    )

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    types = [e.type for e in events]
    assert types.index(RunEventType.VERIFICATION_INTERRUPTED) < types.index(RunEventType.FIX_LOOP_INTERRUPTED)
    assert types.index(RunEventType.FIX_LOOP_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)


def test_completed_fix_attempt_is_not_interrupted(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    _append_fix_attempt_completed(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")
    _append_fix_loop_terminal(runtime, run.run_id, "fix-1", RunEventType.FIX_LOOP_EXHAUSTED, {
        "fix_loop_id": "fix-1", "reason": "stalled", "attempts_used": 1, "max_fix_attempts": 2,
    })

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.FIX_ATTEMPT_INTERRUPTED not in [e.type for e in events]
    assert RunEventType.FIX_LOOP_INTERRUPTED not in [e.type for e in events]


@pytest.mark.parametrize("terminal_type", [
    RunEventType.FIX_LOOP_COMPLETED, RunEventType.FIX_LOOP_EXHAUSTED, RunEventType.FIX_LOOP_FAILED,
])
def test_loop_with_terminal_is_not_interrupted(runtime, terminal_type):
    run = _seed_running_fix_loop_run(runtime, task_id=f"task_{terminal_type.name}", run_id=f"run_{terminal_type.name}")
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    payload = {"fix_loop_id": "fix-1", "reason": "x"}
    if terminal_type == RunEventType.FIX_LOOP_COMPLETED:
        payload = {
            "fix_loop_id": "fix-1", "attempts_used": 1, "final_execution_id": "e",
            "verification_id": "v", "review_id": "r", "diff_sha256": "c" * 64,
        }
    elif terminal_type == RunEventType.FIX_LOOP_EXHAUSTED:
        payload = {"fix_loop_id": "fix-1", "reason": "stalled", "attempts_used": 1, "max_fix_attempts": 2}
    _append_fix_loop_terminal(runtime, run.run_id, "fix-1", terminal_type, payload)

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    assert RunEventType.FIX_LOOP_INTERRUPTED not in [e.type for e in events]


def test_fix_loop_recovery_repeated_scan_is_idempotent(runtime):
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")

    first = recover_running_runs(runtime)
    assert first.interrupted_run_ids == (run.run_id,)
    before_count = len(runtime.events(run.run_id, limit=200).events)

    second = recover_running_runs(runtime)
    assert second.interrupted_run_ids == ()
    after_count = len(runtime.events(run.run_id, limit=200).events)
    assert after_count == before_count
    assert runtime.get_run(run.run_id).status == RunStatus.INTERRUPTED


def test_fix_loop_recovery_never_reruns_worker(runtime):
    """Recovery only settles canonical state; it never invokes a Worker port,
    so there is nothing to assert beyond: no new fix_attempt.started/completed
    is added beyond the interrupted marker, and the Run ends INTERRUPTED."""
    run = _seed_running_fix_loop_run(runtime)
    _append_fix_loop_started(runtime, run.run_id, "fix-1")
    _append_fix_attempt_started(runtime, run.run_id, "fix-1", "att-1", 1, "exec-fix-1")

    recover_running_runs(runtime)

    events = runtime.events(run.run_id, limit=200).events
    starts = [e for e in events if e.type == RunEventType.FIX_ATTEMPT_STARTED]
    assert len(starts) == 1
    assert runtime.get_run(run.run_id).status == RunStatus.INTERRUPTED


# ==================== Planner recovery ====================


def test_unfinished_plan_is_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(
        run_id=run.run_id, type=RunEventType.PLAN_STARTED,
        payload={"plan_id": "plan-1"}, correlation_id="plan-1", source="planner",
    )

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ("run_plan",)
    events = runtime.events("run_plan", limit=200).events
    plan_interrupted = [event for event in events if event.type == RunEventType.PLAN_INTERRUPTED]
    assert len(plan_interrupted) == 1
    assert plan_interrupted[0].payload == {"plan_id": "plan-1", "reason": "process_restart"}
    assert plan_interrupted[0].execution_id is None
    assert plan_interrupted[0].correlation_id == "plan-1"
    types = [event.type for event in events]
    assert types.index(RunEventType.PLAN_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)
    assert runtime.get_run("run_plan").status == RunStatus.INTERRUPTED


def test_completed_plan_is_not_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-1"},
            correlation_id="plan-1", source="planner",
        ),
        RunEventSpec(RunEventType.PLAN_COMPLETED, {
            "plan_id": "plan-1", "summary": "ok",
            "steps": [{"title": "t", "objective": "o"}],
            "acceptance_criteria": [], "risks": [],
            "task_profile": {"complexity": "LOW", "scope": "LOCAL"},
            "repository_fingerprint": "a" * 64, "task_sha256": "b" * 64,
        }, correlation_id="plan-1", source="planner"),
    ))

    recover_running_runs(runtime)

    events = runtime.events("run_plan", limit=200).events
    assert RunEventType.PLAN_INTERRUPTED not in [event.type for event in events]
    assert runtime.get_run("run_plan").status == RunStatus.INTERRUPTED  # run itself still settles


def test_failed_plan_is_not_interrupted(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-1"},
            correlation_id="plan-1", source="planner",
        ),
        RunEventSpec(
            RunEventType.PLAN_FAILED,
            {"plan_id": "plan-1", "error_type": "PlannerProtocolError", "error_message": "bad json"},
            correlation_id="plan-1", source="planner",
        ),
    ))

    recover_running_runs(runtime)

    events = runtime.events("run_plan", limit=200).events
    assert RunEventType.PLAN_INTERRUPTED not in [event.type for event in events]


def test_plan_interrupted_is_not_interrupted_again(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-1"},
            correlation_id="plan-1", source="planner",
        ),
        RunEventSpec(
            RunEventType.PLAN_INTERRUPTED, {"plan_id": "plan-1", "reason": "process_restart"},
            correlation_id="plan-1", source="planner",
        ),
    ))

    recover_running_runs(runtime)

    events = runtime.events("run_plan", limit=200).events
    plan_interrupted = [event for event in events if event.type == RunEventType.PLAN_INTERRUPTED]
    assert len(plan_interrupted) == 1  # not doubled


def test_two_plan_ids_only_unfinished_latest_attempt_settled(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-A"},
            correlation_id="plan-A", source="planner",
        ),
        RunEventSpec(RunEventType.PLAN_COMPLETED, {
            "plan_id": "plan-A", "summary": "ok",
            "steps": [{"title": "t", "objective": "o"}],
            "acceptance_criteria": [], "risks": [],
            "task_profile": {"complexity": "LOW", "scope": "LOCAL"},
            "repository_fingerprint": "a" * 64, "task_sha256": "b" * 64,
        }, correlation_id="plan-A", source="planner"),
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-B"},
            correlation_id="plan-B", source="planner",
        ),
    ))

    recover_running_runs(runtime)

    events = runtime.events("run_plan", limit=200).events
    plan_interrupted = [event for event in events if event.type == RunEventType.PLAN_INTERRUPTED]
    assert len(plan_interrupted) == 1
    assert plan_interrupted[0].payload["plan_id"] == "plan-B"


def test_unfinished_planner_tool_and_plan_are_both_interrupted_in_order(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.record_many(run_id=run.run_id, specs=(
        RunEventSpec(
            RunEventType.PLAN_STARTED, {"plan_id": "plan-1"},
            correlation_id="plan-1", source="planner",
        ),
        RunEventSpec(
            RunEventType.TOOL_REQUESTED,
            {"call_id": "c1", "tool_name": "search_text", "arguments": {}},
            turn_id="t1", item_id="i1", correlation_id="plan-1", source="planner",
        ),
        RunEventSpec(
            RunEventType.TOOL_STARTED,
            {"call_id": "c1", "tool_name": "search_text"},
            turn_id="t1", item_id="i1", correlation_id="plan-1", source="planner",
        ),
    ))

    report = recover_running_runs(runtime)

    assert report.interrupted_run_ids == ("run_plan",)
    events = runtime.events("run_plan", limit=200).events
    types = [event.type for event in events]
    assert types.index(RunEventType.TOOL_INTERRUPTED) < types.index(RunEventType.PLAN_INTERRUPTED)
    assert types.index(RunEventType.PLAN_INTERRUPTED) < types.index(RunEventType.RUN_INTERRUPTED)


def test_plan_recovery_is_idempotent_on_repeated_scan(runtime):
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(
        run_id=run.run_id, type=RunEventType.PLAN_STARTED,
        payload={"plan_id": "plan-1"}, correlation_id="plan-1", source="planner",
    )

    first = recover_running_runs(runtime)
    assert first.interrupted_run_ids == ("run_plan",)
    before = len(runtime.events("run_plan", limit=200).events)

    second = recover_running_runs(runtime)
    assert second.interrupted_run_ids == ()
    after = len(runtime.events("run_plan", limit=200).events)
    assert after == before
    assert runtime.get_run("run_plan").status == RunStatus.INTERRUPTED


def test_plan_recovery_never_reruns_planner(runtime):
    """Recovery only settles canonical state; it never invokes a Planner
    AgentSession, so there is nothing to assert beyond: no new plan.started
    is added beyond the interrupted marker, and the Run ends INTERRUPTED."""
    run = _seed_run(runtime, task_id="task_plan", run_id="run_plan")
    runtime.store.append_event(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    runtime.store.append_event(
        run_id=run.run_id, type=RunEventType.PLAN_STARTED,
        payload={"plan_id": "plan-1"}, correlation_id="plan-1", source="planner",
    )

    recover_running_runs(runtime)

    events = runtime.events("run_plan", limit=200).events
    starts = [e for e in events if e.type == RunEventType.PLAN_STARTED]
    assert len(starts) == 1
    assert runtime.get_run("run_plan").status == RunStatus.INTERRUPTED
