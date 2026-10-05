"""Factory-to-PipelineRunner integration for host-owned worker safe points.

Two independent acceptance proofs for ``engine_factory.build_pipeline_ports``:

1. ``worker_safe_point`` reaches ONLY the native worker adapter, binds the
   host-owned canonical snapshot exactly at the NEXT worker attempt boundary,
   and the run reaches its normal terminal shape with the consumer still open
   (the host -- never the factory -- closes it).
2. A safe point + a non-native (ACP) worker provider is rejected BEFORE any
   port/backend/ACP-client is constructed, and the rejection never starts,
   closes or even reads the host-owned consumer or the snapshot service.

Everything runs offline: a real loopback HTTP server + a real Git-backed
Collaboration store on temporary paths, but scripted (fake) model providers
and a fake process runner.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine_factory  # noqa: E402
from agent_runtime import ModelStopReason, ModelToolCall, ModelTurn, ModelUsage, UserInput  # noqa: E402
from collab_runtime.context import SNAPSHOT_SECTION_HEADER  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.consumer import RevisionConsumer  # noqa: E402
from collab_runtime.client import LoopbackSnapshotClient  # noqa: E402
from collab_runtime.models import build_context, build_initial_state, build_task  # noqa: E402
from collab_runtime.safe_point import NativeWorkerSafePoint  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from executor_runtime import NativeVerificationAttemptAdapter  # noqa: E402
from pipeline_runtime.models import PipelineStatus  # noqa: E402
from pipeline_runtime.runner import PipelineRunner  # noqa: E402
from run_runtime import RunEventType  # noqa: E402
from test_collab_safe_point import _wait_for  # noqa: E402
from test_collab_transport import ALICE, SESSION_ID, servers  # noqa: E402
from test_pipeline_integration import (  # noqa: E402
    FakeProcessRunner, ScriptedBackend, _completed_turn, _process_result,
    repo_workspace, setup_runtime,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")

TASK_TEXT = "Fix the bug in a.txt"
PLAN_SUMMARY = "Step one: correct the body of a.txt."
PLAN_JSON = (
    '{"summary":"' + PLAN_SUMMARY + '","steps":[{"title":"Step 1","objective":"Fix it."}],'
    '"acceptance_criteria":["a.txt says fixed"],"risks":[],'
    '"task_profile":{"complexity":"LOW","scope":"LOCAL"}}'
)
PINNED_PATHS = ("a.txt", "tests/test_a.py")
# Non-terminal consumer states the host may legitimately observe between
# polls; "closed" is the only state that would mean the factory took over.
_RUNNABLE_CONSUMER_STATES = {"connecting", "streaming", "retrying", "inbox_full"}


def _pinned_paths(text: str) -> list[str]:
    """The ordered "- " entries of the USER-REFERENCED FILES section only."""
    marker = "USER-REFERENCED FILES"
    if marker not in text:
        return []
    section = text[text.index(marker):]
    for stop in ("SHARED COLLABORATION SNAPSHOT", "FOLLOW-UP INSTRUCTION"):
        index = section.find(stop)
        if index != -1:
            section = section[:index]
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]


@pytest.fixture
def collab_session(tmp_path, repo_workspace, servers):
    """A live loopback server + host-started consumer over a temporary Git
    store, with all temporary artifacts confined to tmp_path."""
    runtime, run = setup_runtime(tmp_path)
    hub = GitStore.create_bare(tmp_path / "factory-hub.git", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / "factory-store.git"), remote=str(hub))
    revision = store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version="factory integration",
        base_commit=repo_workspace.snapshot.source_head,
    ))
    store.upsert_task(build_task(
        task_id="factory-task", owner="alice", goal=TASK_TEXT,
        scopes=["a.txt"], status="running", context_revision=revision,
    ), expected_revision=revision)
    coordinator = Coordinator(store, session_id=SESSION_ID, owner_id="alice", member_credentials={"alice": ALICE})
    listener = servers(coordinator).start()
    snapshot_client = LoopbackSnapshotClient(listener.base_url, credential=ALICE)
    initial = snapshot_client.snapshot()
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir(mode=0o700)
    consumer = RevisionConsumer(
        listener.base_url, credential=ALICE, member_id="alice",
        checkpoint_path=checkpoint_dir / "cursor.json", initial_snapshot=initial,
    )
    try:
        consumer.start()
        assert _wait_for(lambda: consumer.status()["state"] == "streaming")
        yield SimpleNamespace(
            runtime=runtime, run=run, coordinator=coordinator, initial=initial,
            snapshot_client=snapshot_client,
            task=initial.state.tasks["factory-task"], consumer=consumer,
            baseline=consumer.status()["consumed_revision"],
        )
    finally:
        consumer.close()


def test_factory_built_pipeline_binds_context_only_at_next_worker_attempt(collab_session, repo_workspace, tmp_path):
    session = collab_session
    runtime, run = session.runtime, session.run
    coordinator, consumer = session.coordinator, session.consumer
    initial, task, baseline = session.initial, session.task, session.baseline

    class PublishingBackend:
        """Counts every ACTUAL respond() call per worker attempt, so the
        'one model input per attempt, never per stream event' claim is
        proven by call counts and not inferred from recorded UserInputs."""

        def __init__(self):
            self.calls = []          # (attempt, UserInput text) -- recorded inputs
            self.sessions = 0        # open_session() calls
            self.responds = []       # (attempt, turn_index) -- real model round trips
            self.session_objects = []
            self.initial_worker_input = None
            self.latest_revision = None

        def open_session(self, *, instructions, tools, allow_parallel_tool_calls):
            self.sessions += 1
            attempt = self.sessions
            owner = self

            class Session:
                def __init__(self):
                    self.turn = 0

                def respond(self, input_items):
                    owner.responds.append((attempt, self.turn))
                    user = next((item for item in input_items if isinstance(item, UserInput)), None)
                    if user is not None:
                        owner.calls.append((attempt, user.text))
                    text = user.text if user is not None else next(
                        value for session_attempt, value in reversed(owner.calls)
                        if session_attempt == attempt
                    )
                    state = consumer.status()
                    if attempt == 1 and self.turn == 0:
                        owner.initial_worker_input = text
                        revision_local = coordinator.update_context(
                            ALICE, build_context(goal="middle context", decisions=[], interfaces={}),
                            expected_revision=initial.revision,
                        )
                        owner.latest_revision = coordinator.update_context(
                            ALICE, build_context(goal="latest context", decisions=["current"], interfaces={}),
                            expected_revision=revision_local,
                        )
                        # Two new revisions are DELIVERED but not yet accepted.
                        assert _wait_for(lambda: len(consumer.peek()) == 2)
                        assert consumer.status()["consumed_revision"] == baseline
                        assert consumer.status()["pending_count"] == 2
                        self.turn += 1
                        return ModelTurn("", (
                            ModelToolCall("bad", "write_file", {"path": "a.txt", "content": "still buggy\n"}),
                        ), ModelStopReason.TOOL_USE, ModelUsage())
                    if attempt == 1:
                        # Second model turn of the SAME attempt: nothing was
                        # accepted mid-attempt, and the next turn reuses the
                        # already-bound text (no new snapshot is bound).
                        self.turn += 1
                        assert "latest context" not in text
                        assert state["consumed_revision"] == baseline
                        assert state["pending_count"] == 2
                        assert len(consumer.peek()) == 2
                        return _completed_turn("Initial attempt completed.")
                    if self.turn == 0:
                        # FIRST model turn of the NEXT attempt: the host's
                        # canonical snapshot was bound AND acknowledged before
                        # any model work happened for this attempt.
                        assert "latest context" in text
                        assert "middle context" not in text
                        assert state["consumed_revision"] == owner.latest_revision
                        assert state["pending_count"] == 0
                        assert consumer.peek() == ()
                        self.turn += 1
                        return ModelTurn("", (
                            ModelToolCall("fix", "write_file", {"path": "a.txt", "content": "fixed\n"}),
                        ), ModelStopReason.TOOL_USE, ModelUsage())
                    self.turn += 1
                    return _completed_turn("Fixed it.")

            session_object = Session()
            self.session_objects.append(session_object)
            return session_object

    worker_backend = PublishingBackend()
    planner_backend = ScriptedBackend([_completed_turn(PLAN_JSON)])
    reviewer_backend = ScriptedBackend([_completed_turn(
        '{"verdict":"APPROVED","summary":"Good fix.","findings":[]}'
    )])
    backends = {"openai": iter((planner_backend, worker_backend, reviewer_backend))}
    ports = engine_factory.build_pipeline_ports(
        runtime, run.run_id, {"planner": "openai", "coder": "openai", "reviewer": "openai"},
        backend_factory=lambda provider: next(backends[provider]), worker_safe_point=NativeWorkerSafePoint(
            consumer, session.snapshot_client.snapshot, initial_snapshot=initial,
            member_id="alice", approved_task=task,
        ),
    )
    # The safe point belongs to the worker port only.
    assert isinstance(ports.worker._safe_point, NativeWorkerSafePoint)
    assert not hasattr(ports.planner, "_safe_point")
    assert not hasattr(ports.reviewer, "_safe_point")

    ports = type(ports)(
        ports.planner, ports.worker,
        ports.reviewer,
        NativeVerificationAttemptAdapter(
            runtime, run.run_id, process_runner=FakeProcessRunner([_process_result(1), _process_result(0)]),
        ),
        ports.change_provider,
    )
    report = PipelineRunner(
        runtime, planner=ports.planner, worker=ports.worker, verification=ports.verification,
        reviewer=ports.reviewer, change_provider=ports.change_provider, max_fix_attempts=1,
    ).run(run.run_id, repo_workspace, TASK_TEXT, pinned_paths=PINNED_PATHS)

    assert report.status is PipelineStatus.NEEDS_USER
    assert report.reason == "reviewed"
    assert report.fix_loop_report is not None
    assert (repo_workspace.root / "a.txt").read_text(encoding="utf-8") == "fixed\n"
    assert (tmp_path / "repo" / "a.txt").read_text(encoding="utf-8") == "buggy\n"

    # Exactly two FRESH worker sessions, exactly two real model round trips
    # per attempt, four in total -- so no stream event ever caused an extra
    # model call.
    assert worker_backend.sessions == 2
    assert len({id(s) for s in worker_backend.session_objects}) == 2
    assert worker_backend.responds == [(1, 0), (1, 1), (2, 0), (2, 1)]
    assert len(worker_backend.calls) == 2
    assert [attempt for attempt, _ in worker_backend.calls] == [1, 2]

    first, second = worker_backend.calls[0][1], worker_backend.calls[1][1]
    assert first == worker_backend.initial_worker_input
    assert "latest context" not in first
    assert "latest context" in second
    assert "middle context" not in second

    # The canonical shared snapshot is bound EXACTLY ONCE per model input, and
    # the bound metadata revision flips precisely at the attempt boundary.
    for text in (first, second):
        assert text.count(SNAPSHOT_SECTION_HEADER) == 1
        assert f"base_commit={initial.state.base_commit}" in text
    assert f"revision={initial.revision}" in first
    assert f"revision={worker_backend.latest_revision}" in second
    assert initial.revision != worker_backend.latest_revision

    # Initial attempt: the deterministic verification preview and the
    # ordered, paths-only pinned list survive the collab re-render, as do the
    # trusted ORIGINAL USER TASK and the advisory GENERATED PLAN.
    assert "HOW THIS WILL BE JUDGED" in first
    assert "pytest" in first
    assert TASK_TEXT in first
    assert PLAN_SUMMARY in first
    assert _pinned_paths(first) == list(PINNED_PATHS)

    # Fix attempt: the original task, the plan and the pinned list survive,
    # and the fix framing reports the configured budget (attempt 1 of 1).
    assert "FIX FEEDBACK" in second
    assert "attempt_index: 1" in second
    assert "max_fix_attempts: 1" in second
    assert TASK_TEXT in second
    assert PLAN_SUMMARY in second
    assert _pinned_paths(second) == list(PINNED_PATHS)
    assert second.index(TASK_TEXT) < second.index(PLAN_SUMMARY)

    # Host-owned inbox: fully accepted, nothing left pending.
    assert consumer.status()["consumed_revision"] == worker_backend.latest_revision
    assert consumer.status()["pending_count"] == 0
    assert consumer.peek() == ()

    # The factory never owns the consumer lifecycle: it is still live here and
    # only the host's close() in the fixture teardown stops it.
    assert consumer.status()["state"] in _RUNNABLE_CONSUMER_STATES

    events = runtime.events(run.run_id, limit=500).events
    types = [e.type for e in events]
    # Coarse canonical shape: the initial attempt and its FAILED verification
    # run first, then the fix loop (one attempt -> verification -> review) and
    # finally the proposal hand-off. A collab-bound worker attempt is still an
    # ordinary canonical attempt: two executions, one failed check then a pass.
    coarse = [t for t in types if t in {
        RunEventType.RUN_STARTED, RunEventType.PLAN_STARTED, RunEventType.PLAN_COMPLETED,
        RunEventType.EXECUTION_STARTED, RunEventType.EXECUTION_COMPLETED,
        RunEventType.VERIFICATION_STARTED, RunEventType.VERIFICATION_COMPLETED,
        RunEventType.FIX_LOOP_STARTED, RunEventType.FIX_ATTEMPT_STARTED,
        RunEventType.FIX_ATTEMPT_COMPLETED,
        RunEventType.REVIEW_STARTED, RunEventType.REVIEW_COMPLETED,
        RunEventType.FIX_LOOP_COMPLETED,
        RunEventType.PROPOSAL_READY, RunEventType.RUN_WAITING_USER,
    }]
    assert coarse == [
        RunEventType.RUN_STARTED,
        RunEventType.PLAN_STARTED,
        RunEventType.PLAN_COMPLETED,
        RunEventType.EXECUTION_STARTED,
        RunEventType.EXECUTION_COMPLETED,
        RunEventType.VERIFICATION_STARTED,
        RunEventType.VERIFICATION_COMPLETED,
        RunEventType.FIX_LOOP_STARTED,
        RunEventType.FIX_ATTEMPT_STARTED,
        RunEventType.EXECUTION_STARTED,
        RunEventType.EXECUTION_COMPLETED,
        RunEventType.FIX_ATTEMPT_COMPLETED,
        RunEventType.VERIFICATION_STARTED,
        RunEventType.VERIFICATION_COMPLETED,
        RunEventType.REVIEW_STARTED,
        RunEventType.REVIEW_COMPLETED,
        RunEventType.FIX_LOOP_COMPLETED,
        RunEventType.PROPOSAL_READY,
        RunEventType.RUN_WAITING_USER,
    ], f"full event sequence was: {types}"
    assert RunEventType.RUN_COMPLETED not in types
    assert runtime.get_run(run.run_id).status.value == "waiting_user"

    proposal = next(e for e in events if e.type == RunEventType.PROPOSAL_READY)
    assert proposal.source == "run_gate"
    assert proposal.payload["reason"] == "reviewed"

    # The collaboration base commit is untouched by the isolated run.
    assert repo_workspace.snapshot.source_head == initial.state.base_commit


def test_factory_rejects_acp_worker_before_touching_host_owned_consumer(collab_session):
    """A safe point + ACP (non-native) worker provider is refused during the
    factory's preflight: no port construction, no backend/ACP client build,
    and the host's already-running consumer and snapshot service are never
    consulted -- only the host closes it."""
    session = collab_session
    runtime, run = session.runtime, session.run
    coordinator, consumer = session.coordinator, session.consumer
    initial, task = session.initial, session.task
    built = []
    snapshots = []

    def forbidden(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("no backend/acp client may be constructed")

    class RecordingSafePoint(NativeWorkerSafePoint):
        """A real safe point that additionally proves the factory never
        drives it (no prepare/acknowledge/snapshot-service call)."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.driven = []

        def prepare(self, request, workspace):
            self.driven.append("prepare")
            return super().prepare(request, workspace)

        def accept_reset(self, snapshot, request, workspace):
            self.driven.append("accept_reset")
            return super().accept_reset(snapshot, request, workspace)

    def snapshot_provider():
        snapshots.append(True)
        return collab_session.snapshot_client.snapshot()

    safe_point = RecordingSafePoint(
        consumer, snapshot_provider, initial_snapshot=initial,
        member_id="alice", approved_task=task,
    )

    try:
        with pytest.raises(engine_factory.EngineUnsupportedError,
                           match="Collaboration safe points require a native worker provider"):
            engine_factory.build_pipeline_ports(
                runtime, run.run_id, {"planner": "openai", "coder": "claude", "reviewer": "openai"},
                backend_factory=forbidden, acp_client_factory=forbidden,
                worker_safe_point=safe_point,
            )
        assert built == []
        assert safe_point.driven == []
        assert snapshots == []
        assert consumer.peek() == ()
        assert consumer.status()["consumed_revision"] == session.baseline
        assert consumer.status()["state"] in _RUNNABLE_CONSUMER_STATES
        assert consumer.status()["state"] != "closed"
    finally:
        consumer.close()
    assert consumer.status()["state"] == "closed"
