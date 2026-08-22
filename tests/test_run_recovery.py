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
from run_runtime.events import RunEventType  # noqa: E402
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
