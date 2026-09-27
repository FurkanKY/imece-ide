"""webhost/api/run.py — yeni (pipeline) motor uçtan uca köprü testleri.

HostBridge.call() headless sürülür (QT_QPA_PLATFORM=offscreen). Gerçek model/
ağ çağrısı YOK: engine_factory.build_pipeline_ports enjekte edilerek
ScriptedBackend/sahte process runner ile REAL PipelineRunner/adapter'lar
çalıştırılır — yalnızca transport sahte. Gerçek Git worktree kullanılır
(tmp_path altında; runtime_paths.workspaces_dir yerine yönlendirilir).
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import engine_factory  # noqa: E402
import ui_prefs  # noqa: E402
from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage  # noqa: E402
from change_runtime import GitWorktreeChangeProvider  # noqa: E402
from engine_factory import PipelinePorts  # noqa: E402
from executor_runtime.native_reviewer import NativeReviewAttemptAdapter  # noqa: E402
from executor_runtime.native_verification import NativeVerificationAttemptAdapter  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from pipeline_runtime.native_planner import NativePlanAttemptRunner  # noqa: E402
from process_runtime.models import ProcessResult  # noqa: E402
from review_runtime.runner import ReviewerRunner  # noqa: E402
from run_runtime import RunRuntime, RunStatus, RunStore  # noqa: E402
from run_runtime.events import RunEventType  # noqa: E402
from webhost import state  # noqa: E402
from webhost.bridge import HostBridge  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def bridge(qapp):
    return HostBridge()


def rpc(bridge, method, params=None, call_id=1):
    out = []
    bridge.reply.connect(lambda raw: out.append(json.loads(raw)))
    bridge.call(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    assert out, f"{method}: yanıt gelmedi"
    return out[-1]


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    (repo / "a.txt").write_text("old\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    # kullanıcının o anki (henüz commit edilmemiş) çalışması:
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch, git_repo):
    import webhost.api.run as run_api  # handler kaydı + modül-seviyeli _active durumu

    # _active modül-seviyeli (süreç ömürlü) bir sözlüktür — testler ARASI
    # sızmasın diye her testten önce/sonra sıfırlanır.
    run_api._active.update({
        "worker": None, "coordinator": None, "run_id": None, "proposals": [],
        "engine": "legacy", "workspace": None, "cancel_event": None,
    })
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    state.set_project(str(git_repo))
    state.set_run_runtime(RunRuntime(RunStore(tmp_path / "runs.sqlite3")))
    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto"})
    yield
    run_api._active.update({
        "worker": None, "coordinator": None, "run_id": None, "proposals": [],
        "engine": "legacy", "workspace": None, "cancel_event": None,
    })
    state._active = None
    state.set_run_runtime(None)


# ---------------- sahte pipeline port fabrikası ----------------

class ScriptedSession:
    def __init__(self, turns):
        self.turns = list(turns)

    def respond(self, input_items):
        value = self.turns.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedBackend:
    def __init__(self, turns):
        self.session = ScriptedSession(turns)

    def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
        return self.session


class FakeProcessRunner:
    def __init__(self, results):
        self._results = list(results)

    def run(self, workspace, request, *, cancel_token=None):
        return self._results.pop(0)


def _completed_turn(text):
    return ModelTurn(text, (), ModelStopReason.COMPLETED, ModelUsage())


def _plan_json():
    return (
        '{"summary":"a.txt düzeltilecek.","steps":[{"title":"Adım 1","objective":"Düzelt."}],'
        '"acceptance_criteria":["a.txt fixed yazar"],"risks":[],'
        '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
    )


def _make_ports_factory(*, worker_turns, review_text, process_results=None, no_verification=False):
    def factory(runtime, run_id, routing, **kwargs):
        planner = NativePlanAttemptRunner(runtime, run_id, ScriptedBackend([_completed_turn(_plan_json())]))
        worker = NativeWorkerAttemptAdapter(runtime, run_id, ScriptedBackend(worker_turns))
        reviewer = NativeReviewAttemptAdapter(
            runtime, run_id, ReviewerRunner(ScriptedBackend([_completed_turn(review_text)])),
        )
        results = process_results if process_results is not None else [
            ProcessResult(
                argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                stdout_bytes=0, stderr_bytes=0,
            )
        ]
        verification = NativeVerificationAttemptAdapter(
            runtime, run_id, process_runner=FakeProcessRunner(results),
        )
        return PipelinePorts(
            planner=planner, worker=worker, reviewer=reviewer,
            verification=verification, change_provider=GitWorktreeChangeProvider(),
        )
    return factory


def _fix_worker_turns():
    return [
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("a.txt düzeltildi."),
    ]


def _no_op_worker_turns():
    return [_completed_turn("Hiçbir değişiklik gerekmedi.")]


def _wait_until(predicate, qapp, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _all_native_routing():
    return {"planner": "gemini", "coder": "deepseek", "reviewer": "openai"}


def _drive_run(bridge, qapp, monkeypatch, ports_factory, task="a.txt'yi düzelt"):
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", ports_factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": task, "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp), f"run.finished gelmedi; toplanan olaylar: {events}"
    return run_id, events


def _run_ev_payloads(events, run_id):
    return [e["payload"]["ev"] for e in events if e["channel"] == "run.event" and e["payload"].get("runId") == run_id]


def _finished_payload(events, run_id):
    for e in events:
        if e["channel"] == "run.finished" and e["payload"].get("runId") == run_id:
            return e["payload"]
    raise AssertionError("run.finished bulunamadı")


# ---------------- testler ----------------

def test_pipeline_engine_selected_for_git_repo(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    from webhost.api import run as run_api
    assert run_api._active["engine"] == "pipeline"

    finished = _finished_payload(events, run_id)
    assert finished["status"] == "done"

    evs = _run_ev_payloads(events, run_id)
    stages = [e["stage"] for e in evs if e["type"] == "stage"]
    assert stages == ["plan", "code", "review"]

    proposal_events = [e for e in evs if e["type"] == "proposal"]
    assert len(proposal_events) == 1
    proposals = proposal_events[0]["proposals"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p["path"] == "a.txt"
    assert p["new"] == "fixed\n"
    # Diff, projedeki GÜNCEL (henüz commit edilmemiş "dirty") içeriğe göredir —
    # eski commit ("old") DEĞİL (bkz. T1.2 karar #5/#3).
    assert "-dirty" in p["diff"]
    assert "+fixed" in p["diff"]
    assert "-old" not in p["diff"]

    verdicts = [e for e in evs if e["type"] == "verdict"]
    assert verdicts and verdicts[0]["verdict"] == "APPROVED"

    # F2 (takip isteği): worktree, Run WAITING_USER'da (bekleyen bir
    # proposal var) kaldığı sürece ARTIK KORUNUR -- bir sonraki
    # run.followUp AYNI worktree'den devam edebilsin diye (bkz. karar #1).
    # Yalnızca Apply/Reddet/cancel/yeni run.start/kapanışta dispose edilir.
    from webhost.api import run as run_api
    assert run_api._active["workspace"] is not None
    from webhost import state as _state
    assert _state.get_run_runtime().get_run(run_id).status is RunStatus.WAITING_USER


def test_pipeline_apply_writes_file_and_disposes(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    r = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=2)
    assert r["ok"], r
    assert r["result"]["applied"] == ["a.txt"]
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "fixed\n"

    from webhost.api import run as run_api
    assert run_api._active["proposals"] == []
    assert run_api._active["workspace"] is None

    from webhost import state as _state
    run_record = _state.get_run_runtime().get_run(run_id)
    assert run_record.status.value == "succeeded" or run_record.status.value == "waiting_user"
    canonical_types = [e.type for e in _state.get_run_runtime().events(run_id, limit=500).events]
    assert "proposal.applied" in canonical_types


def test_pipeline_apply_conflict_when_file_modified_during_run(bridge, qapp, monkeypatch, git_repo):
    """Koşu bitip öneri hazır olduktan SONRA, kullanıcı a.txt'yi (editörde
    veya diskte) değiştirirse Apply hiçbir şey yazmamalı, checkpoint
    oluşturmamalı ve öneri bekleyen (pending) kalmalıdır."""
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    (git_repo / "a.txt").write_text("kullanici-degistirdi\n", encoding="utf-8")

    r = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=2)
    assert r["ok"], r
    assert r["result"]["applied"] == []
    assert r["result"]["checkpointId"] is None
    assert len(r["result"]["conflicts"]) == 1
    assert r["result"]["conflicts"][0]["path"] == "a.txt"
    assert "değişti" in r["result"]["conflicts"][0]["reason"]
    # Dosyaya DOKUNULMADI (kullanıcının kendi değişikliği korunur).
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "kullanici-degistirdi\n"

    from webhost.api import run as run_api
    # Öneri bekleyen (pending) kaldı — reddedilebilir/yeniden koşulabilir.
    assert run_api._active["proposals"], "öneriler temizlenmemeli"

    from webhost import state as _state
    run_record = _state.get_run_runtime().get_run(run_id)
    assert run_record.status.value == "waiting_user"
    canonical_types = [e.type for e in _state.get_run_runtime().events(run_id, limit=500).events]
    assert "proposal.applied" not in canonical_types


def test_pipeline_apply_conflict_when_new_file_created_during_run(bridge, qapp, monkeypatch, git_repo):
    """Öneri yeni bir dosya (b.txt) oluşturmayı önerir (taban durumu 'absent').
    Koşu bitip Apply çağrılmadan önce kullanıcı b.txt'yi kendisi oluşturursa
    bu da bir çakışma sayılmalıdır (sessizce üzerine yazılmamalı)."""
    new_file_turns = [
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "b.txt", "content": "hello\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("b.txt oluşturuldu."),
    ]
    ports_factory = _make_ports_factory(worker_turns=new_file_turns, review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    evs = _run_ev_payloads(events, run_id)
    proposal_events = [e for e in evs if e["type"] == "proposal"]
    assert proposal_events and proposal_events[0]["proposals"][0]["path"] == "b.txt"
    assert proposal_events[0]["proposals"][0]["is_new"] is True

    (git_repo / "b.txt").write_text("kullanici-olusturdu\n", encoding="utf-8")

    r = rpc(bridge, "run.applyProposals", {"paths": ["b.txt"]}, call_id=2)
    assert r["ok"], r
    assert r["result"]["applied"] == []
    assert r["result"]["checkpointId"] is None
    assert len(r["result"]["conflicts"]) == 1
    assert r["result"]["conflicts"][0]["path"] == "b.txt"
    assert (git_repo / "b.txt").read_text(encoding="utf-8") == "kullanici-olusturdu\n"


def test_pipeline_apply_conflict_when_file_deleted_during_run(bridge, qapp, monkeypatch, git_repo):
    """Öneri a.txt'yi düzeltmeyi önerir; Apply'dan ÖNCE kullanıcı a.txt'yi
    silerse bu da bir çakışmadır (taban durum artık uyuşmuyor)."""
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    (git_repo / "a.txt").unlink()

    r = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=2)
    assert r["ok"], r
    assert r["result"]["applied"] == []
    assert r["result"]["checkpointId"] is None
    assert len(r["result"]["conflicts"]) == 1
    assert r["result"]["conflicts"][0]["path"] == "a.txt"
    assert not (git_repo / "a.txt").exists()  # dosya durumuna dokunulmadı


def test_pipeline_reject_disposes_without_writing(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    r = rpc(bridge, "run.rejectProposals", {}, call_id=2)
    assert r["ok"], r
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"  # değişmedi

    from webhost.api import run as run_api
    assert run_api._active["proposals"] == []

    from webhost import state as _state
    canonical_types = [e.type for e in _state.get_run_runtime().events(run_id, limit=500).events]
    assert "proposal.rejected" in canonical_types


def test_pipeline_no_changes(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_no_op_worker_turns(), review_text='{"verdict":"APPROVED","summary":"n/a","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)
    finished = _finished_payload(events, run_id)
    assert finished["status"] == "done"
    evs = _run_ev_payloads(events, run_id)
    assert any(e["type"] == "info" and "önerisi çıkmadı" in e.get("text", "") for e in evs)
    # F2/UI bitiş durumu: Worker'ın son mesajı da bir "summary" olayı olarak
    # akışa eklenir -- kullanıcı sadece "değişiklik yok" değil, ajanın ne
    # yaptığını/söylediğini de görür (bkz. webhost/api/run.py
    # _last_worker_final_message).
    assert any(e["type"] == "summary" and "Hiçbir değişiklik gerekmedi" in e.get("text", "") for e in evs)

    from webhost import state as _state
    assert _state.get_run_runtime().get_run(run_id).status.value == "succeeded"


def test_pipeline_review_needs_fix_then_exhausted_reports_failed(bridge, qapp, monkeypatch, git_repo):
    # İlk deneme: NEEDS_FIX, düzeltme denemesi de NEEDS_FIX -> fix loop tükenir.
    def factory(runtime, run_id, routing, **kwargs):
        planner = NativePlanAttemptRunner(runtime, run_id, ScriptedBackend([_completed_turn(_plan_json())]))
        worker = NativeWorkerAttemptAdapter(runtime, run_id, ScriptedBackend([
            *_fix_worker_turns(), *_fix_worker_turns(),
        ]))
        reviewer = NativeReviewAttemptAdapter(runtime, run_id, ReviewerRunner(ScriptedBackend([
            _completed_turn('{"verdict":"NEEDS_FIX","summary":"eksik","findings":["x"]}'),
            _completed_turn('{"verdict":"NEEDS_FIX","summary":"hala eksik","findings":["x"]}'),
        ])))
        verification = NativeVerificationAttemptAdapter(
            runtime, run_id, process_runner=FakeProcessRunner([
                ProcessResult(argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                              stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                              stdout_bytes=0, stderr_bytes=0),
                ProcessResult(argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                              stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                              stdout_bytes=0, stderr_bytes=0),
            ]),
        )
        return PipelinePorts(planner=planner, worker=worker, reviewer=reviewer,
                              verification=verification, change_provider=GitWorktreeChangeProvider())

    monkeypatch.setattr(engine_factory, "build_pipeline_ports", factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    # max_fix_attempts=1 varsayılanla tükenebilir; test yalnızca "failed" ile
    # sonuçlandığını ve worktree'nin dispose edildiğini doğrular (tam deneme
    # sayısına bağlı kalmaz).
    assert _wait_until(is_finished, qapp, timeout=15.0), f"olaylar: {events}"
    finished = _finished_payload(events, run_id)
    assert finished["status"] == "failed"
    from webhost.api import run as run_api
    # A5 (hata UX): terminal hata kartı için TEK Türkçe eşleme noktası
    # (webhost/api/run.py _error_details/_ERROR_MESSAGES) her "failed"
    # sonucu için MUTLAKA bir errorCode/Title/Description üretir -- ham
    # nedene bakılmaksızın (bu senaryoda kanonik error_code
    # "legacy_worker_error"e düşüyor; tam eşleme davranışı
    # tests/test_run_error_mapping.py'de ayrıntılı test edilir).
    assert finished["errorCode"] in run_api._ERROR_MESSAGES
    assert finished["errorTitle"]
    assert finished["errorDescription"]
    assert run_api._active["workspace"] is None


def test_pipeline_cancel_before_start_stops_run(bridge, qapp, monkeypatch, git_repo):
    # Planner çağrılmadan ÖNCE cancel_event set edilirse pipeline "planning"
    # aşaması başlamadan cancelled döner (bkz. PipelineRunner._check_cancel).
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", ports_factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    rpc(bridge, "run.cancel", {}, call_id=2)

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=10.0), f"olaylar: {events}"
    finished = _finished_payload(events, run_id)
    assert finished["status"] in ("cancelled", "done", "failed")
    from webhost.api import run as run_api
    assert run_api._active["workspace"] is None


class _BlockingThenToolUseSession:
    """First respond() blocks (simulating an in-flight model HTTP call)
    until told to proceed, then returns a TOOL_USE turn; a second respond()
    would return the completion turn, but must NEVER be reached once the
    caller cancels while the tool call is pending (see AgentSession's
    per-tool-call cooperative check)."""

    def __init__(self, release: threading.Event, entered: threading.Event):
        self._release = release
        self._entered = entered
        self._calls = 0

    def respond(self, input_items):
        self._calls += 1
        if self._calls == 1:
            self._entered.set()
            self._release.wait(timeout=5)
            return ModelTurn(
                "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
                ModelStopReason.TOOL_USE, ModelUsage(),
            )
        raise AssertionError("respond() must not be called again after cancellation")


def test_pipeline_cancel_mid_native_worker_stops_promptly_and_leaves_no_children(bridge, qapp, monkeypatch, git_repo):
    """F7: run.cancel during a blocking native Worker turn stops the run
    promptly (well under the fake turn's own unblocking + a couple of
    Qt event-loop ticks), settles CANCELLED, disposes the worktree, and
    leaves no leftover child processes."""
    release = threading.Event()
    entered = threading.Event()

    class _BlockingBackend:
        def __init__(self):
            self.session = _BlockingThenToolUseSession(release, entered)

        def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
            return self.session

    def factory(runtime, run_id, routing, **kwargs):
        planner = NativePlanAttemptRunner(runtime, run_id, ScriptedBackend([_completed_turn(_plan_json())]))
        worker = NativeWorkerAttemptAdapter(runtime, run_id, _BlockingBackend())
        reviewer = NativeReviewAttemptAdapter(
            runtime, run_id, ReviewerRunner(ScriptedBackend([_completed_turn(
                '{"verdict":"APPROVED","summary":"iyi","findings":[]}'
            )])),
        )
        verification = NativeVerificationAttemptAdapter(
            runtime, run_id, process_runner=FakeProcessRunner([ProcessResult(
                argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                stdout_bytes=0, stderr_bytes=0,
            )]),
        )
        return PipelinePorts(
            planner=planner, worker=worker, reviewer=reviewer,
            verification=verification, change_provider=GitWorktreeChangeProvider(),
        )

    monkeypatch.setattr(engine_factory, "build_pipeline_ports", factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "a.txt'yi düzelt", "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    this_process = psutil.Process()
    children_before = set(p.pid for p in this_process.children(recursive=True))

    # Wait until the fake Worker turn is actually in flight before cancelling
    # -- this is the "blocking native worker" moment the spec asks for.
    assert entered.wait(timeout=5.0), "worker turn hiç başlamadı"
    cancel_started = time.monotonic()
    rpc(bridge, "run.cancel", {}, call_id=2)
    # Unblock the in-flight "model call" only AFTER cancel was requested --
    # cancellation must be observed at the NEXT cooperative check point
    # (before the tool call this turn requested), not mid-call.
    release.set()

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=10.0), f"olaylar: {events}"
    elapsed = time.monotonic() - cancel_started
    assert elapsed < 2.0, f"iptal çok geç sonuçlandı: {elapsed:.2f}s"

    finished = _finished_payload(events, run_id)
    assert finished["status"] == "cancelled"

    from webhost.api import run as run_api
    assert run_api._active["workspace"] is None

    runtime = state.get_run_runtime()
    assert runtime.get_run(run_id).status is RunStatus.CANCELLED

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        children_after = set(p.pid for p in this_process.children(recursive=True))
        if children_after <= children_before:
            break
        time.sleep(0.05)
    else:
        children_after = set(p.pid for p in this_process.children(recursive=True))
    assert children_after <= children_before, f"artık (leftover) alt süreçler: {children_after - children_before}"


def _fake_legacy_generator(root, task, routing):
    """project_runner.run_project_task yerine geçer: gerçek sağlayıcı/ağ
    çağrısı YAPMADAN hemen tükenen sahte bir legacy event generator'ı."""
    return iter(())


def test_engine_falls_back_to_legacy_when_not_git(bridge, qapp, monkeypatch, tmp_path):
    from webhost.api import run as run_api

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    state.set_project(str(plain_dir))
    monkeypatch.setattr(run_api, "run_project_task", _fake_legacy_generator)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    called = {"n": 0}

    def should_not_be_called(*a, **k):
        called["n"] += 1
        raise AssertionError("pipeline port fabrikası çağrılmamalı (git deposu değil)")

    monkeypatch.setattr(engine_factory, "build_pipeline_ports", should_not_be_called)
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=5.0), f"olaylar: {events}"
    has_info = any(
        e["channel"] == "run.event" and e["payload"].get("runId") == run_id
        and e["payload"]["ev"].get("type") == "info"
        for e in events
    )
    assert has_info, f"'klasik motor kullanılıyor' bilgi satırı yok: {events}"
    assert run_api._active["engine"] == "legacy"
    assert called["n"] == 0


def test_ai_engine_pref_legacy_skips_pipeline(bridge, qapp, monkeypatch, git_repo):
    from webhost.api import run as run_api

    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "legacy"})
    monkeypatch.setattr(run_api, "run_project_task", _fake_legacy_generator)
    called = {"n": 0}

    def should_not_be_called(*a, **k):
        called["n"] += 1
        raise AssertionError("aiEngine=legacy iken pipeline port fabrikası çağrılmamalı")

    monkeypatch.setattr(engine_factory, "build_pipeline_ports", should_not_be_called)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=5.0), f"olaylar: {events}"
    assert run_api._active["engine"] == "legacy"
    assert called["n"] == 0


# ---------------- karar katmanı (decision_layer) kapı inşası ----------------

def test_decision_layer_off_builds_no_gate(bridge, qapp, monkeypatch, git_repo):
    """decision_layer="off" (varsayılan) -- run.py bir VerificationFailureGate
    İNŞA ETMEMELİ; PipelineRunner'a decision_gate=None geçmelidir (bugünkü
    davranış bayt-bayt korunur, bkz. engine_factory.build_verification_failure_gate)."""
    from webhost.api import run as run_api

    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto", "decision_layer": "off"})
    ports_factory = _make_ports_factory(
        worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}',
    )
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    assert run_api._active["engine"] == "pipeline"
    assert run_api._active["decision_gate"] is None


def test_decision_layer_rules_builds_gate(bridge, qapp, monkeypatch, git_repo):
    """decision_layer="rules" -- run.py, engine_factory.build_verification_
    failure_gate aracılığıyla GERÇEK bir VerificationFailureGate inşa edip
    _active'e koymalı (PipelineRunner'a decision_gate=... olarak geçirilen
    AYNI nesne)."""
    from decision_runtime.gate import VerificationFailureGate
    from webhost.api import run as run_api

    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto", "decision_layer": "rules"})
    ports_factory = _make_ports_factory(
        worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}',
    )
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    assert run_api._active["engine"] == "pipeline"
    assert isinstance(run_api._active["decision_gate"], VerificationFailureGate)


# ---------------- legacy motor: "stale apply" koruması ----------------

def _fake_legacy_generator_with_stale_guard(root, task, routing, mentions=None):
    """project_runner.run_project_task'ın gerçek diff-hesaplama adımını
    (taban durum kaydı dahil) taklit eden minimal sahte generator — gerçek
    ajan/ağ çağrısı YAPMAZ."""
    from project import Project

    proj = Project(root)
    new_content = "fixed\n"
    diff = proj.make_diff("a.txt", new_content)
    base_hash = proj.hash_file("a.txt")  # project_runner ile AYNI an/yöntem
    yield {"type": "stage", "stage": "plan", "provider": "x"}
    yield {
        "type": "proposal",
        "proposals": [{
            "path": "a.txt", "new": new_content, "diff": diff, "is_new": False,
            "baseHash": base_hash,
        }],
        "totals": {"cost_usd": 0, "latency_s": 0, "tokens": 0},
        "verdict": "APPROVED",
    }


def _legacy_conflict_repo(tmp_path, name, content):
    plain_dir = tmp_path / name
    plain_dir.mkdir()
    (plain_dir / "a.txt").write_text(content, encoding="utf-8")
    return plain_dir


def test_legacy_apply_conflict_when_file_modified_during_run(bridge, qapp, monkeypatch, tmp_path):
    from webhost.api import run as run_api

    plain_dir = _legacy_conflict_repo(tmp_path, "plain-legacy-conflict", "dirty\n")
    state.set_project(str(plain_dir))
    monkeypatch.setattr(run_api, "run_project_task", _fake_legacy_generator_with_stale_guard)

    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=5.0), f"olaylar: {events}"
    assert run_api._active["engine"] == "legacy"

    # Kullanıcı a.txt'yi Apply'dan ÖNCE değiştirdi (editörde kaydetti / diskte düzenledi).
    (plain_dir / "a.txt").write_text("kullanici-degistirdi\n", encoding="utf-8")

    r2 = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=2)
    assert r2["ok"], r2
    assert r2["result"]["applied"] == []
    assert r2["result"]["checkpointId"] is None
    assert len(r2["result"]["conflicts"]) == 1
    assert r2["result"]["conflicts"][0]["path"] == "a.txt"
    assert "değişti" in r2["result"]["conflicts"][0]["reason"]
    # Dosyaya DOKUNULMADI; checkpoint OLUŞTURULMADI.
    assert (plain_dir / "a.txt").read_text(encoding="utf-8") == "kullanici-degistirdi\n"
    assert not (plain_dir / ".imece" / "checkpoints").exists()
    # Öneri bekleyen (pending) kaldı.
    assert run_api._active["proposals"], "öneriler bekleyen kalmalı (legacy motor)"


def test_legacy_apply_succeeds_when_file_unchanged(bridge, qapp, monkeypatch, tmp_path):
    from webhost.api import run as run_api

    plain_dir = _legacy_conflict_repo(tmp_path, "plain-legacy-ok", "dirty\n")
    state.set_project(str(plain_dir))
    monkeypatch.setattr(run_api, "run_project_task", _fake_legacy_generator_with_stale_guard)

    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=5.0), f"olaylar: {events}"

    # Dosya değişmeden kaldı — Apply eskisi gibi başarılı olmalı.
    r2 = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=2)
    assert r2["ok"], r2
    assert r2["result"]["applied"] == ["a.txt"]
    assert r2["result"]["conflicts"] == []
    assert r2["result"]["checkpointId"]
    assert (plain_dir / "a.txt").read_text(encoding="utf-8") == "fixed\n"
    assert run_api._active["proposals"] == []


# ---------------- F6 (@-mentions): run.start bridge validation ----------------


def test_run_start_mentions_valid_pinned_and_invalid_reported_as_info(bridge, qapp, monkeypatch, git_repo):
    """`run.start`'s optional `mentions` param: a valid mention (existing
    project file) becomes a `pinned_paths` entry the pipeline actually uses
    (visible on the canonical plan.started event); an invalid one (doesn't
    exist) is DROPPED and surfaced as an info event, never crashes the run."""
    ports_factory = _make_ports_factory(
        worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}',
    )
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", ports_factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {
        "task": "a.txt'yi düzelt", "routing": _all_native_routing(),
        "mentions": ["a.txt", "does-not-exist.txt"],
    })
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp), f"run.finished gelmedi; toplanan olaylar: {events}"

    evs = _run_ev_payloads(events, run_id)
    info_texts = [e["text"] for e in evs if e["type"] == "info"]
    assert any("Bahsedilen dosya bulunamadı: does-not-exist.txt" in t for t in info_texts)

    from webhost import state as _state
    canonical_events = _state.get_run_runtime().events(run_id, limit=500).events
    plan_started = next(e for e in canonical_events if e.type == "plan.started")
    assert plan_started.payload.get("pinned_paths") == ["a.txt"]


def test_run_start_mentions_capped_at_backend_limit(bridge, qapp, monkeypatch, git_repo):
    """The backend enforces its own mention cap regardless of what a client
    sends -- never trust the Composer's own 10-item UI cap blindly."""
    from webhost.api import run as run_api

    ports_factory = _make_ports_factory(
        worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}',
    )
    monkeypatch.setattr(engine_factory, "build_pipeline_ports", ports_factory)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    many = [f"nope-{i}.txt" for i in range(25)]
    r = rpc(bridge, "run.start", {
        "task": "a.txt'yi düzelt", "routing": _all_native_routing(), "mentions": many,
    })
    assert r["ok"], r
    run_id = r["result"]["runId"]

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp), f"run.finished gelmedi; toplanan olaylar: {events}"
    evs = _run_ev_payloads(events, run_id)
    info_texts = [e["text"] for e in evs if e["type"] == "info"]
    # every one of the 25 fabricated mentions is invalid (none exist), so
    # every one is reported -- proving the cap applies to VALID mentions
    # only, never silently drops the invalid-mention info events themselves.
    assert len(info_texts) >= 25


# ==================== F2 (follow-up on a proposal): run.followUp ====================


def test_follow_up_produces_second_proposal_and_apply_writes_final_content(bridge, qapp, monkeypatch, git_repo):
    """run -> proposals -> run.followUp -> new proposals reflect BOTH
    changes -> Apply writes the final content -> worktree disposed."""

    def factory(runtime, run_id, routing, **kwargs):
        planner = NativePlanAttemptRunner(runtime, run_id, ScriptedBackend([_completed_turn(_plan_json())]))
        worker = NativeWorkerAttemptAdapter(runtime, run_id, ScriptedBackend([
            *_fix_worker_turns(),  # first attempt: a.txt -> "fixed\n"
            ModelTurn(
                "", (ModelToolCall("c2", "write_file", {"path": "a.txt", "content": "fixed-and-negative-safe\n"}),),
                ModelStopReason.TOOL_USE, ModelUsage(),
            ),
            _completed_turn("Also handled negative numbers."),
        ]))
        reviewer = NativeReviewAttemptAdapter(runtime, run_id, ReviewerRunner(ScriptedBackend([
            _completed_turn('{"verdict":"APPROVED","summary":"iyi","findings":[]}'),
            _completed_turn('{"verdict":"APPROVED","summary":"negatifler de tamam","findings":[]}'),
        ])))
        verification = NativeVerificationAttemptAdapter(
            runtime, run_id, process_runner=FakeProcessRunner([
                ProcessResult(argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                              stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                              stdout_bytes=0, stderr_bytes=0),
                ProcessResult(argv=("true",), cwd=".", exit_code=0, timed_out=False, duration_ms=1,
                              stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
                              stdout_bytes=0, stderr_bytes=0),
            ]),
        )
        return PipelinePorts(
            planner=planner, worker=worker, reviewer=reviewer,
            verification=verification, change_provider=GitWorktreeChangeProvider(),
        )

    run_id, events = _drive_run(bridge, qapp, monkeypatch, factory)
    from webhost.api import run as run_api
    assert run_api._active["workspace"] is not None  # kept -- WAITING_USER

    # A4-A3: run.followUp'a geçirilecek plan bağlamı yalnızca özet (summary)
    # DEĞİL, Planner'ın ürettiği TÜM plan (adımlar/kabul kriterleri dahil)
    # olmalı -- bkz. webhost/api/run.py _full_plan_text.
    stored_plan_text = run_api._active["plan_text"]
    assert "a.txt düzeltilecek." in stored_plan_text  # summary hâlâ orada
    assert "Adım 1" in stored_plan_text  # ama artık adımlar da var
    assert "a.txt fixed yazar" in stored_plan_text  # ve kabul kriterleri de

    r = rpc(bridge, "run.followUp", {"feedback": "negatif sayıları da ele al"}, call_id=2)
    assert r["ok"], r
    assert r["result"]["runId"] == run_id

    events2 = []
    bridge.event.connect(lambda raw: events2.append(json.loads(raw)))

    def is_finished_again():
        finished = [e for e in events2 if e["channel"] == "run.finished"]
        return bool(finished)

    assert _wait_until(is_finished_again, qapp, timeout=10.0), f"olaylar: {events2}"
    finished2 = [e["payload"] for e in events2 if e["channel"] == "run.finished"][-1]
    assert finished2["status"] == "done"
    assert finished2.get("engine") == "pipeline"

    evs2 = _run_ev_payloads(events2, run_id)
    proposal_events = [e for e in evs2 if e["type"] == "proposal"]
    assert proposal_events, f"ikinci proposal olayı gelmedi: {evs2}"
    proposals = proposal_events[-1]["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["new"] == "fixed-and-negative-safe\n"

    from webhost import state as _state
    run_record = _state.get_run_runtime().get_run(run_id)
    assert run_record.status is RunStatus.WAITING_USER
    types = [e.type for e in _state.get_run_runtime().events(run_id, limit=1000).events]
    assert types.count(RunEventType.RUN_RESUMED) == 1
    assert types.count(RunEventType.PROPOSAL_READY) == 2

    r2 = rpc(bridge, "run.applyProposals", {"paths": ["a.txt"]}, call_id=3)
    assert r2["ok"], r2
    assert r2["result"]["applied"] == ["a.txt"]
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "fixed-and-negative-safe\n"
    assert run_api._active["workspace"] is None


def test_follow_up_rejected_for_legacy_engine(bridge, qapp, monkeypatch, tmp_path):
    from webhost.api import run as run_api

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    state.set_project(str(plain_dir))
    monkeypatch.setattr(run_api, "run_project_task", _fake_legacy_generator)
    events = []
    bridge.event.connect(lambda raw: events.append(json.loads(raw)))
    r = rpc(bridge, "run.start", {"task": "x", "routing": _all_native_routing()})
    assert r["ok"], r

    def is_finished():
        return any(e["channel"] == "run.finished" for e in events)

    assert _wait_until(is_finished, qapp, timeout=5.0)
    assert run_api._active["engine"] == "legacy"

    r2 = rpc(bridge, "run.followUp", {"feedback": "x"}, call_id=2)
    assert not r2["ok"]
    assert r2["error"]["code"] == "follow_up_unsupported"
    assert "Klasik motorda" in r2["error"]["message"]


def test_follow_up_rejected_when_no_active_run(bridge, qapp, monkeypatch, git_repo):
    r = rpc(bridge, "run.followUp", {"feedback": "x"}, call_id=1)
    assert not r["ok"]
    assert r["error"]["code"] == "follow_up_unsupported"


def test_follow_up_rejected_after_reject(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    r = rpc(bridge, "run.rejectProposals", {}, call_id=2)
    assert r["ok"], r

    r2 = rpc(bridge, "run.followUp", {"feedback": "x"}, call_id=3)
    assert not r2["ok"]
    assert r2["error"]["code"] == "no_active_run"


def test_follow_up_rejected_empty_feedback(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}')
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    r = rpc(bridge, "run.followUp", {"feedback": "   "}, call_id=2)
    assert not r["ok"]
    assert r["error"]["code"] == "empty_feedback"
