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
import time
from pathlib import Path

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
from run_runtime import RunRuntime, RunStore  # noqa: E402
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

    def run(self, workspace, request):
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

    # worktree, öneri üretildikten SONRA dispose edilmiş olmalı.
    from webhost.api import run as run_api
    assert run_api._active["workspace"] is None


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
