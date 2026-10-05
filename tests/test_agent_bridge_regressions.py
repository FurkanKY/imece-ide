"""Regressions for the explicit single-provider agent bridge (backend only).

Scope (production code under test, read-only here):
  * webhost/api/run.py — `run.start` `providerId` branch, `_AgentWorker`,
    `_wire_agent_worker`, `run.followUp`, `run.cancel`, `run.applyProposals`,
    `run.rejectProposals`, `shutdown`
  * agent_execution_runtime/execution.py — `execute_task` (fingerprint /
    receipt / final capture) and its canonical event lookups

Every case asserts a CANONICAL outcome (run status + run_events) TOGETHER with
the host-side resource invariant (worktree lifetime, worker reachability); a UI
event alone is never treated as proof. Transport is faked (ports are injected,
no model/network), Git worktrees are real.

Fixture hygiene: `_active` / project / RunRuntime are snapshotted and restored,
and every patch goes through monkeypatch, so the legacy pipeline E2E module
keeps its own assumptions.
"""

import json
import shutil
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

import engine_factory
import ui_prefs
from agent_execution_runtime import (
    AgentExecutionPorts,
    AgentExecutionRequest,
    AgentExecutionStatus,
    execute_task,
)
from change_runtime import GitWorktreeChangeProvider
from executor_runtime.native_verification import NativeVerificationAttemptAdapter
from fix_runtime.ports import WorkerAttemptResult
from process_runtime.models import ProcessResult
from run_runtime.events import RunEventType
from run_runtime.readmodels import load_full_event_history
from run_runtime.service import RunRuntime, RunStore
from webhost import state
from webhost.bridge import HostBridge
import webhost.api.run as run_api

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git yok")


# --------------------------------------------------------------------------
# transport / repository fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
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


def _write(path, text):
    """LF bytes only: change capture compares raw working-tree bytes with
    immutable blobs, so a CRLF smudge would invent a phantom ``a.txt``."""
    return path.write_text(text, encoding="utf-8", newline="\n")


def _init_repo(repo):
    """A TEMPORARY repo without newline translation (never a user/global config)."""
    repo.mkdir(exist_ok=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "core.autocrlf", "false"], repo)
    return repo


@pytest.fixture
def git_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "a.txt", "old\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    # the user's uncommitted work must survive every agent run/apply/reject:
    _write(repo / "a.txt", "dirty\n")
    return repo


def _commit_verification(repo, checks):
    """Commit `.imece/verify.json` so the worktree gets a detected plan."""
    (repo / ".imece").mkdir(exist_ok=True)
    _write(repo / ".imece" / "verify.json", json.dumps(checks))
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "verification fixture"], repo)


# --------------------------------------------------------------------------
# deterministic ports (no model, no network)
# --------------------------------------------------------------------------

class ScriptedWorker:
    """One deterministic worker attempt: optional gate, writes, filler events."""

    def __init__(self, runtime, run_id, *, writes=(), gate=None, filler_before=0,
                 filler_after=0, error=None, final_text="Implemented agent fixture."):
        self.runtime, self.run_id = runtime, run_id
        self.writes = tuple(writes)
        self.gate = gate
        self.filler_before = filler_before
        self.filler_after = filler_after
        self.error = error
        self.final_text = final_text
        self.requests: list[str] = []
        self.settled = False

    def release(self):
        self.settled = True
        if self.gate is not None:
            self.gate.set()

    def _filler(self, count, execution_id):
        for index in range(count):
            self.runtime.record(
                run_id=self.run_id, type=RunEventType.EXECUTION_OUTPUT, execution_id=execution_id,
                payload={"chunk": f"filler {index}", "index": index},
            )

    def run(self, workspace, request, *, execution_id, cancel_token=None):
        self.requests.append(request.task)
        if self.gate is not None:
            # blocks the REAL QThread until the test releases it
            self.gate.wait(30)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        for rel, text in self.writes:
            workspace.write_text(rel, text)
        self._filler(self.filler_before, execution_id)
        if self.error is not None:
            raise self.error
        self.runtime.record(
            run_id=self.run_id, type=RunEventType.EXECUTION_COMPLETED, execution_id=execution_id,
            payload={"final_text": self.final_text, "model_turns": 2, "tool_calls": 1},
        )
        self._filler(self.filler_after, execution_id)
        self.settled = True
        return WorkerAttemptResult(execution_id)


class ScriptedProcessRunner:
    """ProcessRunner double; may mutate the workspace like a real check does."""

    def __init__(self, *, exit_code=0, write=None):
        self.exit_code = exit_code
        self.write = write
        self.calls: list[tuple] = []

    def run(self, workspace, request, *, cancel_token=None):
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        self.calls.append(tuple(request.argv))
        if self.write is not None:
            workspace.write_text(*self.write)
        return ProcessResult(
            argv=tuple(request.argv), cwd=".", exit_code=self.exit_code, timed_out=False,
            duration_ms=1, stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
            stdout_bytes=0, stderr_bytes=0,
        )


class PortsRegistry:
    """Injected `build_agent_ports` factory that records every worker it built."""

    def __init__(self):
        self.recipes: list = []
        self.workers: list[ScriptedWorker] = []
        self.verification_factory = None
        self.construction_error: Exception | None = None
        self.provider_ids: list[str] = []
        self.threads: list = []          # every _AgentWorker ever constructed
        self.real_build = run_api.build_agent_ports

    def add(self, **kwargs):
        self.recipes.append(lambda runtime, run_id: ScriptedWorker(runtime, run_id, **kwargs))

    def build(self, runtime, run_id, provider_id):
        self.provider_ids.append(provider_id)
        if self.construction_error is not None:
            raise self.construction_error
        recipe = self.recipes[min(len(self.workers), len(self.recipes) - 1)]
        worker = recipe(runtime, run_id)
        self.workers.append(worker)
        verification = (self.verification_factory(runtime, run_id)
                        if self.verification_factory is not None
                        else NativeVerificationAttemptAdapter(runtime, run_id))
        return AgentExecutionPorts(worker, verification, GitWorktreeChangeProvider())


def _recording_agent_worker(registry):
    """Same real QThread; also remembered so teardown never leaks a live thread."""
    class RecordingAgentWorker(run_api._AgentWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            registry.threads.append(self)

    return RecordingAgentWorker


class BrokenActivityStreamer:
    """Fails on `start()` — i.e. AFTER the agent worker thread already runs."""

    def __init__(self, *args, **kwargs):
        self.activity = _NullSignal()
        self.started = False

    def start(self):
        self.started = True
        raise RuntimeError("activity streamer unavailable")

    def request_stop(self):
        return None

    def wait(self, timeout=None):
        return True


class _NullSignal:
    def connect(self, *_args, **_kwargs):
        return None


@pytest.fixture
def ports(monkeypatch):
    registry = PortsRegistry()
    monkeypatch.setattr(run_api, "build_agent_ports", registry.build)
    monkeypatch.setattr(run_api, "_AgentWorker", _recording_agent_worker(registry))
    return registry


@pytest.fixture(autouse=True)
def agent_host(ports, monkeypatch, git_repo, tmp_path):
    """Own state fixture: restores the legacy E2E module's assumptions."""
    saved_active = dict(run_api._active)
    saved_project = state._active
    saved_runtime = state._run_runtime
    # An earlier module may have left a live streamer/thread referenced by the
    # module-level `_active`; stop it before this fixture drops the handle.
    run_api._stop_activity_streamer()
    inherited = saved_active.get("worker")
    if inherited is not None and inherited.isRunning():
        if saved_active.get("cancel_event") is not None:
            saved_active["cancel_event"].set()
        inherited.wait(5000)
    run_api._active.update({
        "worker": None, "coordinator": None, "run_id": None, "proposals": [],
        "engine": "legacy", "workspace": None, "cancel_event": None,
        "activity_streamer": None, "pipeline_ports": None, "task": None,
        "plan_text": None, "pinned_paths": [], "decision_gate": None,
        "collab_session": None,
    })
    run_api._active.pop("agent_provider_id", None)
    monkeypatch.setattr(engine_factory, "workspaces_dir", lambda: tmp_path / "workspaces")
    monkeypatch.setattr(ui_prefs, "load", lambda: {**ui_prefs.DEFAULTS, "ai_engine": "auto"})
    state.set_project(str(git_repo))
    state.set_run_runtime(RunRuntime(RunStore(tmp_path / "runs.sqlite3")))
    yield ports
    for worker in ports.workers:
        worker.release()
    for thread in ports.threads:
        if thread.isRunning():
            if thread.cancel_event is not None:
                thread.cancel_event.set()
            thread.wait(5000)
    # a live ActivityStreamer must be stopped before _active drops its handle
    run_api._stop_activity_streamer()
    run_api._dispose_workspace()
    root = str(git_repo.resolve())
    for slot in run_api._run_registry.slots():
        if slot.project_root != root:
            continue
        if slot.worker is not None and slot.worker.isRunning():
            if slot.cancel_event is not None:
                slot.cancel_event.set()
            slot.worker.wait(5000)
        if slot.activity_streamer is not None:
            slot.activity_streamer.request_stop()
            slot.activity_streamer.wait(2000)
            assert slot.activity_streamer.isFinished()
            slot.activity_streamer = None
        if slot.workspace is not None:
            slot.workspace.dispose()
            slot.workspace = None
        if slot.worker is not None and slot.worker.isFinished():
            slot.worker = None
        assert run_api._run_registry.forget_cleaned(slot.run_id)
    run_api._active.clear()
    run_api._active.update(saved_active)
    state._active = saved_project
    state.set_run_runtime(saved_runtime)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _pump_until(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def _settle(app, seconds=.3):
    """Let queued signals arrive without waiting for a specific condition."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)


def _canonical_events(runtime, run_id):
    """Read the WHOLE durable history (the lookups under test must page too)."""
    return load_full_event_history(runtime.store, run_id)


def _types(runtime, run_id):
    return [event.type for event in _canonical_events(runtime, run_id)]


def _ui_events(bridge):
    seen: list[dict] = []
    bridge.event.connect(lambda raw: seen.append(json.loads(raw)))
    return seen


def _ui_payloads(seen):
    return [item.get("payload", {}).get("ev", {}) for item in seen
            if item.get("channel") == "run.event"]


def _of_type(payloads, event_type):
    return next((p for p in payloads if p.get("type") == event_type), None)


def _finished(seen):
    return next((item["payload"] for item in seen if item.get("channel") == "run.finished"), None)


def _worktree(run_id, tmp_path):
    return tmp_path / "workspaces" / run_id


def _start_agent_run(bridge, task="write agent.txt", **extra):
    reply = rpc(bridge, "run.start", {"task": task, "providerId": "openai", **extra})
    assert reply["ok"], reply
    return reply["result"]["runId"]


def _await_proposal(qapp, run_id, timeout=20):
    """Wait until the canonical run waits AND the host published its proposals.

    The WAITING_USER status is written by the worker thread before the bridge
    hands the result to the main thread, so the status alone is a race.
    """
    runtime = state.get_run_runtime()
    assert _pump_until(
        qapp,
        lambda: (runtime.get_run(run_id).status.value == "waiting_user"
                 and bool(run_api._active.get("proposals"))),
        timeout=timeout,
    ), "the agent bridge never published a pending proposal"
    return runtime


# --------------------------------------------------------------------------
# 1. worker construction failure settles the canonical run and frees the start
# --------------------------------------------------------------------------

def test_start_worker_construction_failure_settles_failed_and_frees_start(
        ports, bridge, qapp, git_repo, tmp_path):
    ports.construction_error = RuntimeError("backend factory exploded")
    ports.add(writes=(("agent.txt", "never written\n"),))

    reply = rpc(bridge, "run.start", {"task": "write agent.txt", "providerId": "openai"})

    assert reply["ok"] is False
    assert reply["error"]["code"] == "worker_start_failed"
    runtime = state.get_run_runtime()
    runs = runtime.store.list_runs()
    assert len(runs) == 1, "the canonical run must exist and be settled, not silently skipped"
    assert runs[0].status.value == "failed"
    assert runs[0].error_message == "agent_worker_unavailable"
    assert RunEventType.RUN_FAILED in _types(runtime, runs[0].run_id)
    # the isolated worktree must not survive a start that never launched a worker
    assert not _worktree(runs[0].run_id, tmp_path).exists()
    assert run_api._active["coordinator"] is None
    assert run_api._active["engine"] == "legacy"
    assert run_api._active_canonical_run_blocks_start() is False


def test_agent_worker_constructor_failure_settles_slot_and_cleans_workspace(
        ports, bridge, qapp, git_repo, tmp_path, monkeypatch):
    ports.add(writes=(("agent.txt", "never written\n"),))

    def fail_constructor(*_args, **_kwargs):
        raise RuntimeError("worker constructor exploded")

    monkeypatch.setattr(run_api, "_AgentWorker", fail_constructor)
    reply = rpc(bridge, "run.start", {"task": "write agent.txt", "providerId": "openai"})

    assert reply["ok"] is False
    assert reply["error"]["code"] == "worker_start_failed"
    runtime = state.get_run_runtime()
    record = runtime.store.list_runs()[0]
    assert record.status.value == "failed"
    assert record.error_message == "agent_worker_unavailable"
    assert not _worktree(record.run_id, tmp_path).exists()
    slot = run_api._run_registry.get(record.run_id)
    assert slot is not None and slot.worker is None and slot.workspace is None
    assert run_api._active["run_id"] is None


def test_agent_start_failure_releases_never_started_worker_ownership(
        ports, bridge, qapp, git_repo, monkeypatch):
    ports.add(writes=(("agent.txt", "never written\n"),))

    class FailedStartWorker(run_api._AgentWorker):
        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(run_api, "_AgentWorker", FailedStartWorker)
    reply = rpc(bridge, "run.start", {"task": "write agent.txt", "providerId": "openai"})
    assert reply["error"]["code"] == "worker_start_failed"
    record = state.get_run_runtime().store.list_runs()[0]
    slot = run_api._run_registry.get(record.run_id)
    assert record.status.value == "failed"
    assert slot.worker is None and slot.workspace is None
    assert slot not in run_api._run_registry.open_slots(str(git_repo.resolve()))


def test_failed_second_start_does_not_hide_first_run_from_targeted_cancel(
        ports, bridge, qapp, git_repo, monkeypatch):
    gate = threading.Event()
    ports.add(writes=(("agent.txt", "first\n"),), gate=gate)
    first = _start_agent_run(bridge)

    def fail_constructor(*_args, **_kwargs):
        raise RuntimeError("second constructor exploded")

    monkeypatch.setattr(run_api, "_AgentWorker", fail_constructor)
    failed = rpc(bridge, "run.start", {"task": "second", "providerId": "openai"})
    assert failed["error"]["code"] == "worker_start_failed"
    assert run_api._active["engine"] == "legacy"
    cancelled = rpc(bridge, "run.cancel", {"runId": first})
    assert cancelled["ok"], cancelled
    assert run_api._run_registry.get(first).cancel_event.is_set()
    gate.set()
    assert _pump_until(qapp, lambda: state.get_run_runtime().get_run(first).status.value == "cancelled")


def test_followup_started_precedes_optional_streamer_setup(
        ports, bridge, qapp, git_repo, monkeypatch):
    ports.add(writes=(("agent.txt", "first\n"),))
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    seen = _ui_events(bridge)
    observed = []

    class ProbeStreamer(BrokenActivityStreamer):
        def __init__(self, *args, **kwargs):
            observed.append(any(e.get("type") == "followUpStarted" for e in _ui_payloads(seen)))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(run_api, "ActivityStreamer", ProbeStreamer)
    reply = rpc(bridge, "run.followUp", {"runId": run_id, "feedback": "continue"})
    assert reply["ok"], reply
    assert observed == [True], "continuation reset must precede optional stream setup and waits"


def test_fast_finished_agent_retains_stalled_streamer_until_confirmed_finished(
        ports, bridge, qapp, git_repo, monkeypatch):
    from types import SimpleNamespace
    from webhost.run_registry import RunRegistry, RunSlot

    class Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self):
            for callback in self.callbacks:
                callback()

    class StalledStreamer:
        def __init__(self, *_args, **_kwargs):
            self.activity, self.finished = Signal(), Signal()
            self.done = False

        def start(self):
            pass

        def request_stop(self):
            pass

        def wait(self, _timeout):
            return self.done

        def isFinished(self):
            return self.done

    monkeypatch.setattr(run_api, "ActivityStreamer", StalledStreamer)
    worker = SimpleNamespace(stage=Signal(), failed=Signal(), finished_ok=Signal(), finished=Signal(),
                             start=lambda: None, isFinished=lambda: True, isRunning=lambda: False)
    runtime = state.get_run_runtime()
    coordinator = run_api.AgentRunCoordinator.start(
        runtime, project_root=str(git_repo), task="already finished", provider_id="openai")
    coordinator.finish_failed("fixture_finished")
    slot = RunSlot(coordinator.run_id, coordinator.get_run().task_id, str(git_repo.resolve()),
                   "openai", coordinator)
    run_api._wire_agent_worker(worker, runtime=runtime, run_id=slot.run_id, coordinator=coordinator,
                               workspace=None, proj=None, emit_ui=lambda _ev: None, bridge=bridge,
                               ended={"flag": False}, slot=slot)
    worker.finished.emit()
    streamer = slot.activity_streamer
    assert streamer is not None and not streamer.isFinished()
    assert slot.phase == "cleanup_failed" and slot.error_code == "activity_streamer_cleanup_pending"
    registry = RunRegistry()
    registry.add(slot)
    assert registry.open_slots(slot.project_root) == (slot,)
    assert registry.reserve(slot.project_root) and not registry.reserve(slot.project_root)
    streamer.done = True
    streamer.finished.emit()
    assert slot.activity_streamer is None and slot.error_code is None
    assert not registry.open_slots(slot.project_root)


def test_agent_followup_constructor_failure_settles_and_emits_no_started_event(
        ports, bridge, qapp, git_repo, monkeypatch):
    ports.add(writes=(("agent.txt", "first\n"),))
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    seen = _ui_events(bridge)

    def fail_constructor(*_args, **_kwargs):
        raise RuntimeError("follow-up constructor exploded")

    monkeypatch.setattr(run_api, "_AgentWorker", fail_constructor)
    reply = rpc(bridge, "run.followUp", {"runId": run_id, "feedback": "continue"})

    assert reply["ok"] is False
    assert reply["error"]["code"] == "worker_start_failed"
    assert state.get_run_runtime().get_run(run_id).status.value == "failed"
    slot = run_api._run_registry.get(run_id)
    assert slot.phase == "failed" and slot.worker is None and slot.workspace is None
    assert slot.proposals == [] and slot.evidence is None
    assert not any(event.get("type") == "followUpStarted" for event in _ui_payloads(seen))


def test_agent_followup_start_failure_settles_and_emits_no_started_event(
        ports, bridge, qapp, git_repo, monkeypatch):
    ports.add(writes=(("agent.txt", "first\n"),))
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    seen = _ui_events(bridge)

    class FailedStartWorker(run_api._AgentWorker):
        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(run_api, "_AgentWorker", FailedStartWorker)
    reply = rpc(bridge, "run.followUp", {"runId": run_id, "feedback": "continue"})

    assert reply["ok"] is False
    assert reply["error"]["code"] == "worker_start_failed"
    assert state.get_run_runtime().get_run(run_id).status.value == "failed"
    slot = run_api._run_registry.get(run_id)
    assert slot.phase == "failed" and slot.worker is None and slot.workspace is None
    assert slot.proposals == [] and slot.evidence is None
    assert not any(event.get("type") == "followUpStarted" for event in _ui_payloads(seen))


# --------------------------------------------------------------------------
# 2. streamer failure AFTER worker.start(): no stranded job, no busy dispose
# --------------------------------------------------------------------------

def test_start_streamer_failure_keeps_started_worker_reachable_and_workspace(
        ports, bridge, qapp, git_repo, tmp_path, monkeypatch):
    gate = threading.Event()
    ports.add(writes=(("agent.txt", "written\n"),), gate=gate)
    monkeypatch.setattr(run_api, "ActivityStreamer", BrokenActivityStreamer)

    reply = rpc(bridge, "run.start", {"task": "write agent.txt", "providerId": "openai"})

    assert reply["ok"] is True
    coordinator = run_api._active.get("coordinator")
    assert coordinator is not None
    assert reply["result"]["runId"] == coordinator.run_id
    worker = run_api._active.get("worker")
    assert worker is not None and worker.isRunning(), (
        "the already-started worker must stay reachable so run.cancel/shutdown can stop it"
    )
    assert _worktree(coordinator.run_id, tmp_path).is_dir(), (
        "a busy workspace must not be disposed while its worker still runs"
    )
    gate.set()  # release the gated attempt so teardown stays quiescent


# --------------------------------------------------------------------------
# 3. follow-up streamer failure must not dispose the busy workspace
# --------------------------------------------------------------------------

def test_follow_up_streamer_failure_does_not_dispose_busy_workspace(
        ports, bridge, qapp, git_repo, tmp_path, monkeypatch):
    ports.add(writes=(("agent.txt", "first\n"),))
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    assert _worktree(run_id, tmp_path).is_dir()

    gate = threading.Event()
    ports.recipes.append(lambda runtime, worker_run_id: ScriptedWorker(
        runtime, worker_run_id, writes=(("second.txt", "second\n"),), gate=gate))
    monkeypatch.setattr(run_api, "ActivityStreamer", BrokenActivityStreamer)

    reply = rpc(bridge, "run.followUp", {"feedback": "also add second.txt"})

    assert reply["ok"] is True
    assert reply["result"]["runId"] == run_id
    assert _worktree(run_id, tmp_path).is_dir(), (
        "the follow-up worker already runs in this worktree: it must not be disposed"
    )
    running = [w for w in ports.workers if not w.settled]
    assert len(running) <= 1, "only one attempt may be in flight"
    gate.set()  # release the gated follow-up attempt so teardown stays quiescent


# --------------------------------------------------------------------------
# 4. cancel while a real gated worker runs -> quiescent settlement
# --------------------------------------------------------------------------

def test_cancel_of_gated_worker_settles_cancelled_then_disposes_worktree(
        ports, bridge, qapp, git_repo, tmp_path):
    gate = threading.Event()
    ports.add(writes=(("agent.txt", "written\n"),), gate=gate)
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge)
    assert _pump_until(qapp, lambda: run_api._active["worker"] is not None
                       and run_api._active["worker"].isRunning())
    worktree = _worktree(run_id, tmp_path)
    assert worktree.is_dir()

    cancelled_reply = rpc(bridge, "run.cancel")
    assert cancelled_reply["ok"]

    assert run_api._active["cancel_event"].is_set(), "cancel must reach the running attempt's token"
    assert worktree.is_dir(), "cancel must not dispose a workspace that is still in use"
    # The canonical run may already have observed the cancellation and settled:
    # an early CANCELLED is the same honest outcome as a still-running one. What
    # this gate really protects is that the worktree survives the gate, i.e. it is
    # NOT disposed while the gated attempt is still holding it.
    assert state.get_run_runtime().get_run(run_id).status.value in {"running", "cancelled"}

    gate.set()
    assert _pump_until(qapp, lambda: _finished(seen) is not None)
    finished = _finished(seen)
    assert finished["status"] == "cancelled"
    assert finished["engine"] == "agent"
    assert state.get_run_runtime().get_run(run_id).status.value == "cancelled"
    assert RunEventType.RUN_CANCELLED in _types(state.get_run_runtime(), run_id)
    assert _pump_until(qapp, lambda: not worktree.exists()), (
        "the worktree is only removed once the worker thread is quiescent"
    )
    assert run_api._active["workspace"] is None
    assert not (git_repo / "agent.txt").exists(), "a cancelled attempt applies nothing"


# --------------------------------------------------------------------------
# 5. follow-up must not operate the original worktree after a project switch
# --------------------------------------------------------------------------

def test_follow_up_refused_after_project_switch_never_touches_original_worktree(
        ports, bridge, qapp, git_repo, tmp_path):
    ports.add(writes=(("agent.txt", "first\n"),))
    runtime = state.get_run_runtime()
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    original_worktree = _worktree(run_id, tmp_path)
    assert original_worktree.is_dir()

    other = _init_repo(tmp_path / "other-repo")
    _write(other / "b.txt", "other\n")
    _git(["add", "-A"], other)
    _git(["commit", "-q", "-m", "init"], other)
    state.set_project(str(other))

    resumed_before = _types(runtime, run_id).count(RunEventType.RUN_RESUMED)
    reply = rpc(bridge, "run.followUp", {"feedback": "continue in the original worktree"})

    assert reply["ok"] is False, (
        "a follow-up must not operate the original worktree while another project is selected"
    )
    assert reply["error"]["code"] != "internal"
    _settle(qapp)
    assert _types(runtime, run_id).count(RunEventType.RUN_RESUMED) == resumed_before
    assert sorted(p.name for p in other.iterdir() if p.name != ".git") == ["b.txt"]


# --------------------------------------------------------------------------
# 6. provider / routing validation happens before ANY allocation
# --------------------------------------------------------------------------

def test_provider_validation_rejects_before_any_run_or_worktree_allocation(
        ports, bridge, qapp, git_repo, tmp_path):
    cases = [
        ({"task": "t", "providerId": 7}, "invalid_provider"),
        ({"task": "t", "providerId": ""}, "invalid_provider"),
        ({"task": "t", "providerId": "not-a-registered-provider"}, "invalid_provider"),
        ({"task": "t", "providerId": "qwen-code"}, "invalid_provider"),
        ({"task": "t", "providerId": "openai", "routing": {"coder": "openai"}}, "conflicting_routing"),
        ({"task": "t", "providerId": "openai", "reviewer": "openai"}, "conflicting_routing"),
        ({"task": "t", "providerId": "openai", "collabApprovalHandle": "handle-1"}, "collab_unsupported"),
        ({"task": "x" * 20_001, "providerId": "openai"}, "task_too_long"),
    ]
    runtime = state.get_run_runtime()
    for index, (params, expected) in enumerate(cases, start=1):
        reply = rpc(bridge, "run.start", params, call_id=index)
        assert reply["ok"] is False, params
        assert reply["error"]["code"] == expected, (params, reply["error"])
        assert runtime.store.list_runs() == [], f"a rejected start must not allocate a run: {params}"
        assert not (tmp_path / "workspaces").exists(), f"a rejected start must not create a worktree: {params}"
        assert run_api._active["coordinator"] is None
        assert run_api._active["engine"] == "legacy"
    assert ports.workers == []


# --------------------------------------------------------------------------
# 7. stale apply (user edited a proposal file during the run) is refused
# --------------------------------------------------------------------------

def test_stale_apply_after_new_source_wip_is_refused_without_checkpoint(
        ports, bridge, qapp, git_repo, tmp_path):
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)

    # the user creates the same path in the source project while the run waits:
    _write(git_repo / "agent.txt", "user wip\n")
    result = rpc(bridge, "run.applyProposals", {"paths": ["agent.txt"]})["result"]

    assert result["applied"] == []
    assert result["errors"] == []
    assert [c["path"] for c in result["conflicts"]] == ["agent.txt"]
    assert result["checkpointId"] is None
    assert (git_repo / "agent.txt").read_text(encoding="utf-8") == "user wip\n"
    assert runtime.get_run(run_id).status.value == "waiting_user", "proposals stay pending"
    assert RunEventType.PROPOSAL_APPLIED not in _types(runtime, run_id)
    assert _worktree(run_id, tmp_path).is_dir(), "a refused apply keeps the pending worktree"


# --------------------------------------------------------------------------
# 8. explicit apply -> checkpoint + canonical accept
# --------------------------------------------------------------------------

def test_explicit_apply_creates_checkpoint_and_records_canonical_accept(
        ports, bridge, qapp, git_repo, tmp_path):
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"

    result = rpc(bridge, "run.applyProposals", {"paths": ["agent.txt"]})["result"]

    assert result["applied"] == ["agent.txt"]
    assert result["errors"] == [] and result["conflicts"] == []
    assert result["checkpointId"]
    assert (git_repo / "agent.txt").read_text(encoding="utf-8") == "agent content\n"
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "dirty\n", "source WIP untouched"
    applied = next(e for e in _canonical_events(runtime, run_id)
                   if e.type == RunEventType.PROPOSAL_APPLIED)
    assert applied.payload["applied"] == ["agent.txt"]
    assert applied.payload["checkpoint_id"] == result["checkpointId"]
    assert run_api._active["proposals"] == []
    assert _pump_until(qapp, lambda: not _worktree(run_id, tmp_path).exists()), (
        "the settled worktree is released after a committed apply"
    )


# --------------------------------------------------------------------------
# 9. reject -> canonical reject, no checkpoint, source WIP untouched
# --------------------------------------------------------------------------

def test_reject_records_canonical_reject_and_never_writes_the_source(
        ports, bridge, qapp, git_repo, tmp_path):
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    run_id = _start_agent_run(bridge)
    _await_proposal(qapp, run_id)

    assert rpc(bridge, "run.rejectProposals", {})["ok"]

    rejected = next(e for e in _canonical_events(runtime, run_id)
                    if e.type == RunEventType.PROPOSAL_REJECTED)
    assert rejected.payload["rejected"] == ["agent.txt"]
    assert RunEventType.PROPOSAL_APPLIED not in _types(runtime, run_id)
    assert not (git_repo / "agent.txt").exists()
    assert (git_repo / "a.txt").read_text(encoding="utf-8") == "dirty\n"
    assert run_api._active["proposals"] == []
    assert _pump_until(qapp, lambda: not _worktree(run_id, tmp_path).exists())


# --------------------------------------------------------------------------
# 10./11. honest verification evidence
# --------------------------------------------------------------------------

def test_failed_check_is_not_reported_as_pass(ports, bridge, qapp, git_repo):
    _commit_verification(git_repo, [{
        "id": "always-fails", "title": "Always fails",
        "argv": [sys.executable, "-c", "raise SystemExit(1)"], "timeout_ms": 5000,
    }])
    runner = ScriptedProcessRunner(exit_code=1)
    ports.verification_factory = lambda runtime, run_id: NativeVerificationAttemptAdapter(
        runtime, run_id, process_runner=runner)
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge)
    assert _pump_until(qapp, lambda: _finished(seen) is not None)

    evidence = _of_type(_ui_payloads(seen), "evidence")
    assert evidence is not None and evidence["verification"]["outcome"] == "fail"
    assert evidence["verification"]["checks"] == [{"check_id": "always-fails", "status": "fail"}]
    assert _of_type(_ui_payloads(seen), "proposal") is not None, "a failed check is still reviewable"
    assert runner.calls, "the deterministic plan must actually run"
    assert runtime.get_run(run_id).status.value == "waiting_user"
    assert not (git_repo / "agent.txt").exists()


def test_workspace_without_verification_plan_reports_not_run(ports, bridge, qapp, git_repo):
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge)
    assert _pump_until(qapp, lambda: _finished(seen) is not None)

    evidence = _of_type(_ui_payloads(seen), "evidence")
    assert evidence["verification"]["outcome"] == "not_run"
    assert evidence["verification"]["checks"] == []
    assert evidence["verification"]["verification_id"] is None
    proposal = _of_type(_ui_payloads(seen), "proposal")
    assert proposal["proposals"], "an unverified run still publishes a reviewable proposal"
    assert runtime.get_run(run_id).status.value == "waiting_user"


def test_check_mutating_unchanged_file_invalidates_pass_and_publication_is_final_capture(
        ports, bridge, qapp, git_repo):
    """Verification side effects must downgrade the verdict AND be published.

    The published diff_sha256/changed_paths must describe the FINAL capture
    (after the checks ran), otherwise the UI would offer a stale proposal.
    """
    _commit_verification(git_repo, [{
        "id": "mutating", "title": "Touches an unchanged file",
        "argv": [sys.executable, "-c", "open('side.txt', 'w').write('side\\n')"],
        "timeout_ms": 5000,
    }])
    runner = ScriptedProcessRunner(exit_code=0, write=("side.txt", "side\n"))
    ports.verification_factory = lambda runtime, run_id: NativeVerificationAttemptAdapter(
        runtime, run_id, process_runner=runner)
    ports.add(writes=(("agent.txt", "agent content\n"),))
    runtime = state.get_run_runtime()
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge)
    assert _pump_until(qapp, lambda: _finished(seen) is not None)

    evidence = _of_type(_ui_payloads(seen), "evidence")
    assert evidence["verification"]["outcome"] == "invalidated"
    assert evidence["verification"]["changed_content"] is True
    assert evidence["verification"]["checks"] == [{"check_id": "mutating", "status": "pass"}]
    final = GitWorktreeChangeProvider().capture(run_api._active["workspace"])
    assert sorted(evidence["changed_paths"]) == sorted(final.changed_paths)
    assert "side.txt" in evidence["changed_paths"], "the mutated file belongs to the proposal"
    assert evidence["diff_sha256"] == final.diff_sha256, (
        "the published diff must be the post-verification capture"
    )
    proposal = _of_type(_ui_payloads(seen), "proposal")
    assert sorted(p["path"] for p in proposal["proposals"]) == sorted(final.changed_paths)
    assert runtime.get_run(run_id).status.value == "waiting_user"


# --------------------------------------------------------------------------
# 12. one backend, no planner / reviewer
# --------------------------------------------------------------------------

def test_single_provider_run_uses_one_backend_and_no_planner_or_reviewer(
        ports, bridge, qapp, git_repo, monkeypatch):
    from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage

    turns = [
        ModelTurn("", (ModelToolCall("write-1", "write_file",
                                    {"path": "native.txt", "content": "native result\n"}),),
                  ModelStopReason.TOOL_USE, ModelUsage()),
        ModelTurn("Implemented native.txt.", (), ModelStopReason.COMPLETED, ModelUsage()),
    ]

    class OneShotBackend:
        def __init__(self):
            self.sessions = 0

        def open_session(self, **_kwargs):
            self.sessions += 1
            remaining = list(turns)

            class Session:
                def respond(self, _input_items):
                    return remaining.pop(0)

            return Session()

    backend = OneShotBackend()
    monkeypatch.setattr(engine_factory, "_default_backend_factory", lambda _provider: backend)
    monkeypatch.setattr(run_api, "build_agent_ports", ports.real_build)
    monkeypatch.setattr(engine_factory, "_build_planner",
                        lambda *a, **k: pytest.fail("a planner must never be built"))
    monkeypatch.setattr(engine_factory, "_build_reviewer",
                        lambda *a, **k: pytest.fail("a reviewer must never be built"))
    runtime = state.get_run_runtime()
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge, task="create native.txt")
    assert _pump_until(qapp, lambda: _finished(seen) is not None)

    assert backend.sessions == 1
    types = _types(runtime, run_id)
    assert not [t for t in types if str(t).startswith("plan.")]
    assert not [t for t in types if str(t).startswith("review.")]
    assert types.count(RunEventType.EXECUTION_COMPLETED) == 1
    assert _of_type(_ui_payloads(seen), "proposal") is not None
    assert runtime.get_run(run_id).status.value == "waiting_user"
    assert not (git_repo / "native.txt").exists()


# --------------------------------------------------------------------------
# 13./14. canonical event pagination (first page is not the whole history)
# --------------------------------------------------------------------------

def test_execute_task_finds_completion_past_the_first_event_page(tmp_path, git_repo):
    runtime = RunRuntime(RunStore(tmp_path / "core.sqlite3"))
    task = runtime.create_task(project_root=str(git_repo), prompt="write agent.txt")
    run = runtime.create_run(task_id=task.task_id)
    runtime.record(run_id=run.run_id, type=RunEventType.RUN_STARTED, payload={})
    workspace = engine_factory.create_pipeline_workspace(git_repo, run.run_id)
    ports = AgentExecutionPorts(
        ScriptedWorker(runtime, run.run_id, writes=(("agent.txt", "content\n"),), filler_before=120),
        NativeVerificationAttemptAdapter(runtime, run.run_id),
        GitWorktreeChangeProvider(),
    )
    try:
        result = execute_task(runtime, run.run_id,
                              AgentExecutionRequest("write agent.txt", "openai", workspace), ports=ports)
        assert result.status is AgentExecutionStatus.NEEDS_USER, (
            "execution.completed beyond the first event page must still be found"
        )
        assert result.changed_paths == ("agent.txt",)
        assert runtime.get_run(run.run_id).status.value == "waiting_user"
    finally:
        workspace.dispose()


def test_bridge_publishes_proposal_when_it_falls_past_the_first_event_page(
        ports, bridge, qapp, git_repo):
    ports.add(writes=(("agent.txt", "agent content\n"),), filler_after=120)
    runtime = state.get_run_runtime()
    seen = _ui_events(bridge)

    run_id = _start_agent_run(bridge)
    assert _pump_until(qapp, lambda: _finished(seen) is not None)

    finished = _finished(seen)
    assert finished["status"] == "done", (
        f"a proposal beyond the first event page must still be published (got {finished})"
    )
    payloads = _ui_payloads(seen)
    assert _of_type(payloads, "proposal") is not None
    evidence = _of_type(payloads, "evidence")
    assert evidence and evidence["reason"] == "single_agent_proposal"
    assert RunEventType.PROPOSAL_READY in _types(runtime, run_id)
    assert runtime.get_run(run_id).status.value == "waiting_user"
