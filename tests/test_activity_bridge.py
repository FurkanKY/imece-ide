"""webhost/api/run.py + webhost/api/activity.py -- F1 (live agent activity)
end-to-end bridge test.

Same harness pattern as tests/test_run_pipeline_bridge.py (HostBridge.call()
driven headless under QT_QPA_PLATFORM=offscreen, a REAL PipelineRunner with
ScriptedBackend/FakeProcessRunner transports, a real Git worktree) --
duplicated here (not imported) since tests/ has no package __init__.py and
each bridge test file is self-contained by convention in this repo.

Verifies that `run.activity` bridge events are actually emitted during a
real pipeline run (not just the legacy stage/metric vocabulary), and that
throttling/coalescing never drops an item's FINAL status.
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
    # A test_*.py file at the root makes verification_detect.detect_verification_plan
    # match the pytest heuristic (see docs/ARCHITECTURE.md "Verification
    # detection"), so this run actually reaches the verification stage
    # instead of the advisory-review-only branch -- needed so the
    # verification role shows up in the live-activity feed.
    (repo / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch, git_repo):
    import webhost.api.run as run_api

    reset = {
        "worker": None, "coordinator": None, "run_id": None, "proposals": [],
        "engine": "legacy", "workspace": None, "cancel_event": None, "activity_streamer": None,
    }
    run_api._active.update(reset)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    state.set_project(str(git_repo))
    state.set_run_runtime(RunRuntime(RunStore(tmp_path / "runs.sqlite3")))
    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto"})
    yield
    run_api._active.update(reset)
    state._active = None
    state.set_run_runtime(None)


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


def _fix_worker_turns():
    return [
        ModelTurn(
            "", (ModelToolCall("c1", "write_file", {"path": "a.txt", "content": "fixed\n"}),),
            ModelStopReason.TOOL_USE, ModelUsage(),
        ),
        _completed_turn("a.txt düzeltildi."),
    ]


def _make_ports_factory(*, worker_turns, review_text):
    def factory(runtime, run_id, routing, **kwargs):
        planner = NativePlanAttemptRunner(runtime, run_id, ScriptedBackend([_completed_turn(_plan_json())]))
        worker = NativeWorkerAttemptAdapter(runtime, run_id, ScriptedBackend(worker_turns))
        reviewer = NativeReviewAttemptAdapter(
            runtime, run_id, ReviewerRunner(ScriptedBackend([_completed_turn(review_text)])),
        )
        results = [
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
    # Streamer'ın son drain'i (kapalı olsa bile) ana thread'e kuyruklu
    # bağlantıyla ulaşır -- birkaç ek processEvents() turu bunu akıtır.
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.01)
    return run_id, events


def _activity_payloads(events, run_id):
    return [e["payload"] for e in events if e["channel"] == "run.activity" and e["payload"].get("runId") == run_id]


def test_run_activity_emitted_during_pipeline_run(bridge, qapp, monkeypatch, git_repo):
    ports_factory = _make_ports_factory(
        worker_turns=_fix_worker_turns(), review_text='{"verdict":"APPROVED","summary":"iyi","findings":[]}',
    )
    run_id, events = _drive_run(bridge, qapp, monkeypatch, ports_factory)

    activity = _activity_payloads(events, run_id)
    assert activity, "run.activity kanalından hiçbir olay gelmedi"

    # Her öğe F1'in bounded şeklini taşımalı.
    for item in activity:
        assert set(item) >= {"id", "runId", "seq", "ts", "role", "kind", "status", "title"}
        assert item["role"] in {"planner", "worker", "verification", "reviewer", "fix", "system"}
        assert item["kind"] in {"tool", "model", "check", "stage", "note", "usage"}
        assert item["status"] in {"running", "ok", "error", "info"}

    # Planner/Worker/Reviewer/Verification hepsi en az bir aşama öğesi üretir.
    roles_seen = {item["role"] for item in activity}
    assert {"planner", "worker", "reviewer", "verification"} <= roles_seen

    # write_file aracı için id bazlı yerinde-güncelleme: son durum "ok" olmalı,
    # kaybolmamalı (throttling/coalescing final durumu asla düşürmez).
    write_items = [item for item in activity if "Düzenlendi: a.txt" in item["title"]]
    assert write_items, f"write_file etkinlik öğesi bulunamadı: {activity}"
    ids = {item["id"] for item in write_items}
    assert len(ids) == 1  # aynı çağrı boyunca tek, sabit id
    assert write_items[-1]["status"] == "ok"

    # Doğrulama kontrolü de görünür olmalı (verification.check_* -> check kind).
    check_items = [item for item in activity if item["kind"] == "check"]
    assert check_items and check_items[-1]["status"] in {"ok", "error"}
