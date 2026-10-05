"""Bounded lifecycle acceptance for the host-owned collaboration boundary.

Everything on the collaboration side is REAL: a temporary Git source checkout
(``repo_workspace``), a real loopback HTTP server over a real Git-backed store
(``servers``), the real ``LoopbackSnapshotClient``, the real
``RevisionConsumer`` streaming ``text/event-stream`` revisions, the real POSIX
checkpoint lease and the real private cursor file. Only the model provider is
scripted (``ScriptedBackend``), and only two per-test seams inject failures
that a live server cannot produce deterministically (a terminal consumer state
and a failing ``close``); both are confined to the test that needs them.

Proven here:

* ``preview`` / ``approve`` / ``bind_run`` create no server subscriber, no
  consumer, no cursor namespace and no lock file;
* ``activate`` runs the real HTTP preflight and starts the real consumer;
  ``deactivate``/``close`` stop it, release the lease and never advance the
  durable cursor;
* a restart replays every delta newer than the PERSISTED cursor, in order,
  before the next bind, and never silently rebases the cursor on a fresh head;
* an explicit ``reset_cursor`` consent is armed at activation, spent exactly
  once at the FIRST ``prepare``, and resets to the EXACT approved revision;
* root / base / head, task and session-identity drift, and cancellation are
  refused before any checkpoint write, any consumer and any model call;
* one writer per (root, session, task, member) checkpoint namespace: a second
  owner is refused while the first is active, across host instances AND across
  processes, and a failed close keeps the lease held until the retry succeeds.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import fcntl
except ImportError:  # Windows uses msvcrt for the production checkpoint lease.
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime.cancellation import CancellationToken, OperationCancelledError  # noqa: E402
from collab_runtime.client import LoopbackSnapshotClient  # noqa: E402
from collab_runtime.context import SNAPSHOT_SECTION_HEADER  # noqa: E402
from collab_runtime.coordinator import Coordinator  # noqa: E402
from collab_runtime.host import (  # noqa: E402
    CollaborationHost, HostCollaborationError, NativeRunCollaboration, _CheckpointLease, _private_cursor_dir,
)
from collab_runtime.models import (  # noqa: E402
    SessionState, build_context, build_initial_state, build_task,
)
from collab_runtime.safe_point import SafePointError  # noqa: E402
from collab_runtime.store import GitStore  # noqa: E402
from executor_runtime.native_worker import NativeWorkerAttemptAdapter  # noqa: E402
from fix_runtime.models import InitialWorkerRenderContext, InitialWorkerRequest  # noqa: E402
from test_collab_safe_point import _wait_for  # noqa: E402
from test_collab_transport import ALICE, SESSION_ID, servers  # noqa: E402
from test_pipeline_integration import (  # noqa: E402
    ScriptedBackend, _completed_turn, repo_workspace, setup_runtime,
)

TASK_TEXT = "Fix the bug in a.txt"
TARGET_VERSION = "host lifecycle"
TASK_ID = "factory-task"
MEMBER = "alice"
PREVIEW_KEYS = {
    "previewId", "projectRoot", "endpoint", "memberId", "taskId", "sessionId",
    "targetVersion", "baseCommit", "revision", "task", "context",
}
STATUS_KEYS = {
    "state", "code", "consumedRevision", "receivedRevision", "pendingCount",
    "sessionId", "taskId", "memberId", "active",
}
CURSOR_KEYS = {
    "schema", "session_id", "base_commit", "target_version", "member_id", "consumed_revision",
}


# --------------------------------------------------------------------- helpers


def _initial_request() -> InitialWorkerRequest:
    """A canonical first-attempt worker input with rebinding metadata."""
    return InitialWorkerRequest(
        task=TASK_TEXT, rendered_input="stale bounded input", plan="advisory plan",
        render_context=InitialWorkerRenderContext(verification_preview="pytest -q", pinned_paths=("a.txt",)),
    )


def _legacy_request() -> InitialWorkerRequest:
    """A pre-safe-point input with no render metadata: it must fail closed."""
    return InitialWorkerRequest(task=TASK_TEXT, rendered_input="legacy bounded input")


def _stand_in(env, *, source_root=None, source_head=None) -> SimpleNamespace:
    """A minimal trusted stand-in for the fields activation reads."""
    return SimpleNamespace(
        snapshot=SimpleNamespace(
            source_root=env.source if source_root is None else source_root,
            source_head=env.head if source_head is None else source_head,
        ),
        root=env.workspace.root,
    )


def _git_clean(source: Path) -> bool:
    status = subprocess.run(["git", "status", "--porcelain"], cwd=source, check=True, capture_output=True)
    return status.stdout == b""


def _commit_in_source(env, name: str) -> str:
    """Move the TEMPORARY checkout's HEAD forward (never the user's project)."""
    (env.source / name).write_text("moved\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=env.source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=env.source, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.source, check=True,
                          capture_output=True, text=True).stdout.strip()


def _cursor_file(env) -> Path:
    files = sorted(env.cursor_root.glob("*.json"))
    assert len(files) == 1, f"expected exactly one private cursor file, found {files}"
    return files[0]


def _lock_file(env) -> Path:
    files = sorted(env.cursor_root.glob("*.lock"))
    assert len(files) == 1, f"expected exactly one checkpoint lease file, found {files}"
    return files[0]


def _consumed(env) -> str:
    return json.loads(_cursor_file(env).read_text(encoding="utf-8"))["consumed_revision"]


def _publish_context(env, goal: str) -> str:
    """Owner-side context publication; the consumer is never touched."""
    env.revision = env.coordinator.update_context(
        ALICE, build_context(goal=goal, decisions=[], interfaces={}),
        expected_revision=env.revision,
    )
    return env.revision


def _publish_task(env, task) -> str:
    revision, state = env.store.fetch_state()
    env.revision = env.store.publish(state.with_task(task), expected_revision=revision)
    env.task_revision = task.context_revision
    return env.revision


def _publish_without_task(env) -> str:
    revision, state = env.store.fetch_state()
    stripped = SessionState(state.session_id, state.target_version, state.base_commit,
                            state.context, {})
    env.revision = env.store.publish(stripped, expected_revision=revision)
    return env.revision


def _descriptors_under(directory: Path) -> int:
    proc_fds = Path("/proc/self/fd")
    if not proc_fds.is_dir():
        pytest.skip("descriptor accounting requires /proc/self/fd")
    count = 0
    for name in os.listdir(proc_fds):
        try:
            target = os.readlink(proc_fds / name)
        except OSError:
            continue
        if target.startswith(str(directory)):
            count += 1
    return count


@pytest.fixture
def env(tmp_path, repo_workspace, servers):
    """A live loopback session over a real store, pinned at the real worktree head."""
    source = Path(repo_workspace.snapshot.source_root)
    head = repo_workspace.snapshot.source_head
    hub = GitStore.create_bare(tmp_path / "lifecycle-hub.git", what="hub")
    store = GitStore(store=GitStore.create_bare(tmp_path / "lifecycle-store.git"), remote=str(hub))
    revision = store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version=TARGET_VERSION, base_commit=head,
    ))
    context_revision = revision
    revision = store.upsert_task(build_task(
        task_id=TASK_ID, owner=MEMBER, goal=TASK_TEXT, scopes=["a.txt"], status="running",
        context_revision=context_revision,
    ), expected_revision=revision)
    coordinator = Coordinator(store, session_id=SESSION_ID, owner_id=MEMBER,
                              member_credentials={MEMBER: ALICE})
    listener = servers(coordinator).start()
    trust = tmp_path / "private"
    trust.mkdir(mode=0o700)
    state = SimpleNamespace(
        tmp=tmp_path, servers=servers, store=store, coordinator=coordinator, listener=listener,
        base_url=listener.base_url, credential=ALICE, workspace=repo_workspace, source=source,
        head=head, trust=trust, cursor_root=trust / "collab-cursors", revision=revision,
        task_revision=context_revision, sessions=[],
    )
    try:
        yield state
    finally:
        for session in reversed(state.sessions):
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


def _approved(env, host=None, *, reset_cursor=False, run_id="run-1", cursor_root=None):
    """preview -> approve -> bind_run, with no activation of any kind."""
    host = host or CollaborationHost(cursor_root or env.cursor_root)
    preview = host.preview(env.source, env.base_url, env.credential, MEMBER, TASK_ID)
    approval = host.approve(preview["previewId"], env.source, reset_cursor=reset_cursor)
    session = host.bind_run(approval["approvalHandle"], env.source, run_id)
    env.sessions.append(session)
    return host, preview, session


def _activate_and_stream(env, session, workspace=None):
    session.activate(workspace or env.workspace)
    assert _wait_for(lambda: session.status()["state"] == "streaming"), session.status()
    return session.status()


def _refused(callable_, *args, **kwargs) -> HostCollaborationError:
    with pytest.raises(HostCollaborationError) as caught:
        callable_(*args, **kwargs)
    assert isinstance(caught.value, HostCollaborationError)
    return caught.value


# ------------------------------------------------------- 1. inert + no secrets


def test_preview_approve_and_bind_are_inert_and_never_leak_the_credential(env):
    host = CollaborationHost(env.cursor_root)
    assert not env.cursor_root.exists()

    preview = host.preview(env.source, env.base_url, env.credential, MEMBER, TASK_ID)
    assert set(preview) == PREVIEW_KEYS
    assert preview["task"] == {"owner": MEMBER, "goal": TASK_TEXT, "scopes": ["a.txt"],
                               "status": "running", "contextRevision": env.task_revision}

    approval = host.approve(preview["previewId"], env.source)
    assert set(approval) == {"approvalHandle", "preview", "resetCursor"}
    assert approval["resetCursor"] is False
    assert approval["preview"]["revision"] == preview["revision"]

    session = host.bind_run(approval["approvalHandle"], env.source, "run-1")
    env.sessions.append(session)
    # No consumer, no subscriber, no lease and no cursor file exist yet: nothing
    # was activated by previewing, approving or binding.
    assert not env.cursor_root.exists()
    assert session.active is False
    assert session.safe_point is session

    status = session.status()
    assert set(status) == STATUS_KEYS
    assert status["state"] == "inactive" and status["active"] is False
    # The baseline is the APPROVED revision, never a silently fetched head.
    assert status["consumedRevision"] == preview["revision"]
    assert status["receivedRevision"] == preview["revision"]
    assert status["pendingCount"] == 0
    assert (status["sessionId"], status["taskId"], status["memberId"]) == (SESSION_ID, TASK_ID, MEMBER)

    for surface in (preview, approval, status, session.snapshot(), host, session):
        assert env.credential not in repr(surface), surface

    # drop_preview/clear forget the credential record and nothing else.
    spare = host.preview(env.source, env.base_url, env.credential, MEMBER, TASK_ID)
    host.drop_preview(spare["previewId"])
    host.drop_preview(spare["previewId"])  # idempotent
    _refused(host.approve, spare["previewId"], env.source)
    host.clear(env.source)
    _refused(host.bind_run, approval["approvalHandle"], env.source, "run-2")
    assert session.status()["taskId"] == TASK_ID
    assert session.safe_point is session


# ------------------------------------------------------ 2. real activation


def test_activate_starts_the_real_consumer_over_the_approved_baseline(env):
    _, preview, session = _approved(env)
    status = _activate_and_stream(env, session)

    assert status["active"] is True
    assert status["consumedRevision"] == preview["revision"] == status["receivedRevision"]
    assert status["pendingCount"] == 0
    assert status["code"] is None

    # Exactly one private namespace, one 0600 cursor file (schema 1) and one
    # lease file; the credential never reaches the private namespace.
    assert env.cursor_root.stat().st_mode & 0o777 == 0o700
    cursor = _cursor_file(env)
    _lock_file(env)
    payload = json.loads(cursor.read_text(encoding="utf-8"))
    assert set(payload) == CURSOR_KEYS
    assert payload["schema"] == 1
    assert payload["consumed_revision"] == preview["revision"]
    assert (payload["session_id"], payload["member_id"]) == (SESSION_ID, MEMBER)
    assert (payload["base_commit"], payload["target_version"]) == (env.head, TARGET_VERSION)
    assert cursor.stat().st_mode & 0o777 == 0o600
    assert env.credential.encode("ascii") not in cursor.read_bytes()
    for surface in (session.status(), session.snapshot(), session, repr(env.cursor_root)):
        assert env.credential not in repr(surface)

    # Activation touched neither the user's checkout nor its index/refs/config.
    assert _git_clean(env.source)
    assert env.workspace.snapshot.source_head == env.head
    assert (env.source / "a.txt").read_text(encoding="utf-8") == "buggy\n"


# ------------------------------------------------------ 3. prepare/ack cursor


def test_prepare_binds_the_delivered_delta_and_only_the_ack_advances_the_cursor(env):
    _, preview, session = _approved(env)
    assert session.accepted_binding is None
    _activate_and_stream(env, session)

    first = _publish_context(env, "shared goal one")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()
    assert _consumed(env) == preview["revision"]

    prepared = session.prepare(_initial_request(), env.workspace)
    assert prepared.binding.revision == first
    assert session.accepted_binding is None
    assert isinstance(prepared.request, InitialWorkerRequest)
    assert prepared.request is not None and callable(prepared.acknowledge)
    text = prepared.request.rendered_input
    assert text.count(SNAPSHOT_SECTION_HEADER) == 1
    assert "shared goal one" in text and TASK_TEXT in text
    # Binding is not acknowledgement: the durable cursor has not moved yet.
    assert _consumed(env) == preview["revision"]

    prepared.acknowledge()
    accepted = session.accepted_binding
    assert accepted is not None and accepted.revision == first
    with pytest.raises((AttributeError, TypeError)):
        accepted.state.tasks[TASK_ID] = accepted.state.tasks[TASK_ID]
    assert _consumed(env) == first
    status = session.status()
    assert status["consumedRevision"] == first == status["receivedRevision"]
    assert status["pendingCount"] == 0


# ------------------------------------------------- 4. the model turn boundary


def test_the_native_worker_adapter_binds_and_acknowledges_before_the_model_turn(env, tmp_path):
    _, preview, session = _approved(env)
    _activate_and_stream(env, session)
    published = _publish_context(env, "shared goal for the model")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()

    backend = ScriptedBackend([_completed_turn("Done.")])
    runtime, run = setup_runtime(tmp_path)
    adapter = NativeWorkerAttemptAdapter(runtime, run.run_id, backend, safe_point=session.safe_point)

    result = adapter.run(env.workspace, _initial_request(), execution_id="exec-host-1")

    assert result.execution_id == "exec-host-1"
    # Exactly one real model round trip, fed the bound canonical snapshot.
    assert len(backend.session.received_inputs) == 1
    text = backend.first_user_input_text
    assert text.count(SNAPSHOT_SECTION_HEADER) == 1
    assert "shared goal for the model" in text
    # The acknowledgement completed BEFORE the model was called.
    assert _consumed(env) == published
    assert session.status()["pendingCount"] == 0
    assert session.status()["consumedRevision"] == published
    # The run happened in the isolated workspace; the source checkout is untouched.
    assert (env.source / "a.txt").read_text(encoding="utf-8") == "buggy\n"
    assert _git_clean(env.source)


# ------------------------------------------------- 5. teardown + lease handoff


def test_deactivate_stops_the_consumer_releases_the_lease_and_never_advances(env):
    _, preview, session = _approved(env)
    _activate_and_stream(env, session)
    _publish_context(env, "delivered but never acknowledged")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()

    session.deactivate()
    status = session.status()
    assert status["active"] is False
    assert status["state"] == "closed"
    assert status["consumedRevision"] == preview["revision"]
    assert _consumed(env) == preview["revision"]

    # The lease is released: a second owner takes the very same checkpoint.
    _, _, second = _approved(env, host=CollaborationHost(env.cursor_root), run_id="run-2")
    _activate_and_stream(env, second)
    assert len(sorted(env.cursor_root.glob("*.json"))) == 1

    # close() is permanent and idempotent, and never revives the run.
    session.close()
    session.close()
    closed = session.status()
    assert closed["active"] is False
    assert set(closed) == STATUS_KEYS
    assert closed["sessionId"] == SESSION_ID and closed["taskId"] == TASK_ID
    assert _refused(session.activate, env.workspace).code == "invalid"
    assert _refused(session.prepare, _initial_request(), env.workspace).code == "invalid"
    assert _consumed(env) == preview["revision"]


# ------------------------------------------------- 6. restart / replay deltas


def test_restart_replays_every_delta_newer_than_the_persisted_cursor_in_order(env):
    _, preview, session = _approved(env)
    _activate_and_stream(env, session)

    first = _publish_context(env, "shared goal one")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()
    prepared = session.prepare(_initial_request(), env.workspace)
    prepared.acknowledge()
    assert _consumed(env) == first

    session.deactivate()
    second = _publish_context(env, "shared goal two")
    third = _publish_context(env, "shared goal three")

    session.activate(env.workspace)
    assert _wait_for(lambda: session.status()["state"] == "streaming"), session.status()
    # Resumed from the PERSISTED cursor, not from a fresh head baseline.
    assert session.status()["consumedRevision"] == first
    assert _wait_for(lambda: session.status()["pendingCount"] == 2), session.status()
    status = session.status()
    assert status["receivedRevision"] == third
    assert status["consumedRevision"] == first
    assert status["pendingCount"] == 2

    prepared = session.prepare(_initial_request(), env.workspace)
    text = prepared.request.rendered_input
    assert text.count(SNAPSHOT_SECTION_HEADER) == 1
    assert "shared goal three" in text
    assert "shared goal one" not in text  # the acknowledged prefix is not rebound
    prepared.acknowledge()
    assert _consumed(env) == third
    assert second != third


# ---------------------------------------------- 7. root / base / head refusal


def test_root_base_and_head_drift_and_an_in_checkout_cursor_root_are_refused_before_storage(env):
    for label, stand_in in (
        ("root", _stand_in(env, source_root=env.source / "tests")),
        ("base", _stand_in(env, source_head="0" * 40)),
    ):
        _, _, session = _approved(env)
        assert _refused(session.activate, stand_in).code == "stale", label
        assert session.active is False, label
        assert not env.cursor_root.exists(), f"{label} drift created a private namespace"

    # A cursor namespace inside the checkout is refused and never created.
    inside = env.source / ".imece-cursors"
    _, _, refused = _approved(env, host=CollaborationHost(inside), run_id="run-inside")
    assert _refused(refused.activate, env.workspace).code == "storage"
    assert not inside.exists()
    assert not env.cursor_root.exists()
    assert _git_clean(env.source)

    _, _, moved = _approved(env)
    _commit_in_source(env, "moved-head.txt")
    assert _refused(moved.activate, env.workspace).code == "stale"
    assert moved.active is False
    assert not env.cursor_root.exists()
    assert _git_clean(env.source)


# ------------------------------------------- 8. server-side task/session drift


def test_task_owner_scope_status_removal_and_session_identity_drift_are_refused_before_storage(env):
    def absolute_task(**overrides) -> object:
        fields = {"task_id": TASK_ID, "owner": MEMBER, "goal": TASK_TEXT, "scopes": ["a.txt"],
                  "status": "running", "context_revision": env.task_revision}
        fields.update(overrides)
        return build_task(**fields)

    mutations = (
        ("reassigned", lambda: _publish_task(env, absolute_task(owner="bob"))),
        ("scope_widened", lambda: _publish_task(env, absolute_task(scopes=["a.txt", "tests"]))),
        ("status_done", lambda: _publish_task(env, absolute_task(status="done"))),
        ("removed", lambda: _publish_without_task(env)),
    )
    for label, mutate in mutations:
        # A private namespace of its own, so "nothing was written" is exact.
        refused_root = env.trust / f"refused-{label}"
        _, _, session = _approved(env, host=CollaborationHost(refused_root), run_id=f"run-{label}")
        mutate()
        error = _refused(session.activate, env.workspace)
        assert error.code in {"stale", "invalid"}, f"{label}: {error.code}"
        assert session.active is False, label
        # A refused activation leaves nothing to bind through either.
        assert _refused(session.prepare, _initial_request(), env.workspace).code == "invalid", label
        assert session.status()["pendingCount"] == 0, label
        assert not refused_root.exists(), f"{label} created a private namespace"
        session.close()

        # Restoring the exact approved task makes the next run activatable
        # again, so the refusals above were caused by the mutation alone.
        _publish_task(env, absolute_task())
        _, _, restored = _approved(env, host=CollaborationHost(env.cursor_root), run_id=f"run-after-{label}")
        _activate_and_stream(env, restored)
        restored.close()
        assert _git_clean(env.source)

    # A whole different session identity (same ids, new target version) fails
    # the pinned-identity preflight before any checkpoint is written.
    other_hub = GitStore.create_bare(env.tmp / "other-hub.git", what="hub")
    other_store = GitStore(store=GitStore.create_bare(env.tmp / "other-store.git"), remote=str(other_hub))
    other_store.init_session(build_initial_state(
        session_id=SESSION_ID, target_version="host lifecycle v2", base_commit=env.head,
    ))
    other = Coordinator(other_store, session_id=SESSION_ID, owner_id=MEMBER,
                        member_credentials={MEMBER: ALICE})
    other_listener = env.servers(other).start()

    real = LoopbackSnapshotClient(env.base_url, credential=env.credential)
    calls = {"count": 0}

    def switching_client(endpoint, *, credential):
        class _Switching:
            def __init__(self) -> None:
                self._credential = credential

            def snapshot(self):
                calls["count"] += 1
                url = env.base_url if calls["count"] == 1 else other_listener.base_url
                return LoopbackSnapshotClient(url, credential=self._credential).snapshot()

        return _Switching()

    identity_root = env.trust / "refused-identity"
    swapped = CollaborationHost(identity_root, client_factory=switching_client)
    _, _, switched = _approved(env, host=swapped, run_id="run-identity")
    assert _refused(switched.activate, env.workspace).code in {"stale", "invalid"}
    assert switched.active is False
    assert not identity_root.exists()
    # One real HTTP call per endpoint: the approved one, then the drifted one.
    assert calls["count"] == 2, calls


# ------------------------------------------------- 9. goal change / re-approval


def test_a_changed_task_goal_requires_a_fresh_approval_and_is_never_retargeted(env):
    _, preview, session = _approved(env)
    _publish_context(env, "shared goal one")
    _publish_task(env, build_task(
        task_id=TASK_ID, owner=MEMBER, goal="Rewrite the goal entirely",
        scopes=["a.txt"], status="running", context_revision=env.task_revision,
    ))

    assert _refused(session.activate, env.workspace).code == "stale"
    assert session.active is False
    assert not env.cursor_root.exists()
    assert "Rewrite the goal entirely" not in preview["task"]["goal"]

    # A NEW explicit approval (a new run) is the only way in, and it binds the
    # newly approved goal -- it never retargets the refused approval.
    _, fresh_preview, fresh = _approved(env, host=CollaborationHost(env.cursor_root), run_id="run-2")
    assert fresh_preview["task"]["goal"] == "Rewrite the goal entirely"
    _activate_and_stream(env, fresh)
    prepared = fresh.prepare(_initial_request(), env.workspace)
    assert "Rewrite the goal entirely" in prepared.request.rendered_input
    assert "shared goal one" in prepared.request.rendered_input


# ------------------------------------------------------------ 10. cancellation


def test_cancellation_before_activation_and_during_the_http_preflight_is_preserved(env):
    _, _, session = _approved(env)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError):
        session.activate(env.workspace, cancel_token=token)
    assert session.active is False
    assert not env.cursor_root.exists()

    entered = threading.Event()
    release = threading.Event()
    real = LoopbackSnapshotClient(env.base_url, credential=env.credential)

    class GatedClient:
        """A real loopback client whose activation preflight blocks on demand."""

        def __init__(self) -> None:
            self._calls = 0

        def snapshot(self):
            self._calls += 1
            if self._calls == 2:
                entered.set()
                assert release.wait(10), "test gate was never released"
            return real.snapshot()

    host = CollaborationHost(env.cursor_root, client_factory=lambda endpoint, *, credential: GatedClient())
    _, _, gated = _approved(env, host=host, run_id="run-cancel")
    token = CancellationToken()
    failures: list[BaseException] = []

    def activate() -> None:
        try:
            gated.activate(env.workspace, cancel_token=token)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
            failures.append(exc)

    worker = threading.Thread(target=activate, daemon=True)
    worker.start()
    assert entered.wait(10), "the HTTP preflight never started"
    token.cancel()
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], OperationCancelledError), failures
    assert gated.active is False
    assert not env.cursor_root.exists()


# ------------------------------------------------- 11. failed prepares are inert


def test_cancelled_and_failed_prepares_leave_the_cursor_inbox_and_consent_unchanged(env):
    _, preview, session = _approved(env, reset_cursor=True)
    session.activate(env.workspace)
    # An armed reset consent does not start the consumer: it waits for the
    # first input boundary.
    armed = session.status()
    assert armed["active"] is True and armed["state"] == "stopped"
    assert _consumed(env) == preview["revision"]

    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError):
        session.prepare(_initial_request(), env.workspace, cancel_token=token)
    assert _consumed(env) == preview["revision"]
    assert session.status()["state"] == "stopped"
    # The consent was not burned by the cancellation.
    assert _wait_for(lambda: session.prepare(_initial_request(), env.workspace) is not None)

    session.close()
    _, preview, session = _approved(env, host=CollaborationHost(env.cursor_root), run_id="run-legacy")
    _activate_and_stream(env, session)
    delivered = _publish_context(env, "shared goal one")
    assert _wait_for(lambda: session.status()["pendingCount"] == 1), session.status()

    with pytest.raises(SafePointError):
        session.prepare(_legacy_request(), env.workspace)
    assert _consumed(env) == preview["revision"]
    assert session.status()["pendingCount"] == 1
    assert session.status()["consumedRevision"] == preview["revision"]

    prepared = session.prepare(_initial_request(), env.workspace)
    prepared.acknowledge()
    assert _consumed(env) == delivered


# ------------------------------------------------------------- 12. reset consent


def test_reset_consent_is_armed_at_activation_and_spends_the_approved_revision_once(env):
    _, preview, session = _approved(env, reset_cursor=True)
    session.activate(env.workspace)
    assert session.status()["active"] is True and session.status()["state"] == "stopped"
    assert _consumed(env) == preview["revision"]

    published = _publish_context(env, "context published after approval")
    prepared = session.prepare(_initial_request(), env.workspace)
    text = prepared.request.rendered_input
    assert text.count(SNAPSHOT_SECTION_HEADER) == 1
    # The reset rebased the cursor on the EXACT approved revision; it never
    # adopted the fresh head that was published after the approval.
    assert _consumed(env) == preview["revision"]
    prepared.acknowledge()
    assert _consumed(env) == published
    assert session.status()["pendingCount"] == 0

    # The consent is one-shot: a restart starts the consumer eagerly instead of
    # resetting the cursor back to the approved revision.
    session.deactivate()
    session.activate(env.workspace)
    assert _wait_for(lambda: session.status()["state"] == "streaming"), session.status()
    assert _consumed(env) == published
    assert session.status()["consumedRevision"] == published

    # A task change between activation and the first (reset) prepare refuses the
    # reset before anything is written.
    session.close()
    _, preview, blocked = _approved(env, host=CollaborationHost(env.cursor_root),
                                    reset_cursor=True, run_id="run-blocked")
    blocked.activate(env.workspace)
    assert blocked.status()["state"] == "stopped"
    _publish_task(env, build_task(
        task_id=TASK_ID, owner=MEMBER, goal="goal moved after activation",
        scopes=["a.txt"], status="running", context_revision=env.task_revision,
    ))
    assert _refused(blocked.prepare, _initial_request(), env.workspace).code == "stale"
    assert _consumed(env) == published
    assert blocked.status()["state"] == "stopped"


# ----------------------------------------------------------- 13. terminal gate


@pytest.mark.parametrize("terminal", ["resnapshot_required", "access_denied"])
def test_a_terminal_consumer_gate_is_preserved_after_deactivate_and_close(env, monkeypatch, terminal):
    started = []

    class ScriptedConsumer:
        """Per-test double for states a live server cannot produce on demand.

        ``close`` normalizes the state exactly like the real consumer does, so
        the host must latch the terminal gate itself.
        """

        def __init__(self, base_url, *, credential, member_id, checkpoint_path, initial_snapshot):
            self.member_id = member_id
            self.session_identity = (initial_snapshot.state.session_id,
                                     initial_snapshot.state.base_commit,
                                     initial_snapshot.state.target_version)
            self._revision = initial_snapshot.revision
            self._state = "connecting"

        def start(self):
            self._state = terminal
            started.append(True)
            return self

        def status(self):
            return {"state": self._state, "code": self._state if self._state == terminal else None,
                    "consumed_revision": self._revision, "received_revision": self._revision,
                    "pending_count": 0}

        def peek(self):
            return ()

        def close(self):
            self._state = "closed"

    monkeypatch.setattr("collab_runtime.host.RevisionConsumer", ScriptedConsumer)
    _, _, session = _approved(env)
    session.activate(env.workspace)
    assert started == [True]
    gated = session.status()
    assert gated["active"] is True and gated["state"] == terminal and gated["code"] == terminal

    # The gate blocks the next bind instead of silently binding stale context.
    with pytest.raises(SafePointError):
        session.prepare(_initial_request(), env.workspace)

    session.deactivate()
    after = session.status()
    assert after["active"] is False
    assert after["state"] == terminal and after["code"] == terminal
    session.close()
    final = session.status()
    assert final["active"] is False
    assert final["state"] == terminal and final["code"] == terminal
    assert set(final) == STATUS_KEYS


# --------------------------------------------------------- 14. prompt status()


def test_status_answers_while_an_activation_is_in_flight(env):
    entered = threading.Event()
    release = threading.Event()
    real = LoopbackSnapshotClient(env.base_url, credential=env.credential)

    class GatedClient:
        def __init__(self) -> None:
            self._calls = 0

        def snapshot(self):
            self._calls += 1
            if self._calls == 2:
                entered.set()
                assert release.wait(10), "test gate was never released"
            return real.snapshot()

    host = CollaborationHost(env.cursor_root, client_factory=lambda endpoint, *, credential: GatedClient())
    _, _, session = _approved(env, host=host)
    failures: list[BaseException] = []

    def activate() -> None:
        try:
            session.activate(env.workspace)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
            failures.append(exc)

    worker = threading.Thread(target=activate, daemon=True)
    worker.start()
    assert entered.wait(10), "the HTTP preflight never started"

    answered = threading.Event()
    observed: list[dict] = []

    def poll() -> None:
        observed.append(session.status())
        answered.set()

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    # A status poll must never queue behind an in-flight activation.
    assert answered.wait(2.0), "status() blocked behind the activation preflight"
    assert set(observed[0]) == STATUS_KEYS
    release.set()
    worker.join(10)
    poller.join(10)
    assert failures == []
    assert _wait_for(lambda: session.status()["state"] == "streaming"), session.status()


# ------------------------------------------------------------ 15. one writer


def test_a_second_owner_of_the_same_checkpoint_is_refused_until_the_first_releases(env):
    _, _, first = _approved(env, run_id="run-1")
    _activate_and_stream(env, first)
    cursor_before = _cursor_file(env).read_bytes()

    _, _, second = _approved(env, host=CollaborationHost(env.cursor_root), run_id="run-2")
    assert _refused(second.activate, env.workspace).code == "busy"
    assert second.active is False
    # The refused owner shares one namespace; it wrote nothing of its own.
    assert len(sorted(env.cursor_root.glob("*.json"))) == 1
    assert len(sorted(env.cursor_root.glob("*.lock"))) == 1
    assert _cursor_file(env).read_bytes() == cursor_before

    first.deactivate()
    _activate_and_stream(env, second)
    assert _cursor_file(env).read_bytes() == cursor_before


def test_a_failed_consumer_close_keeps_the_lease_held_until_the_retry_succeeds(env, monkeypatch):
    _, _, session = _approved(env, run_id="run-1")
    _activate_and_stream(env, session)

    # Per-test failure seam on the live consumer; the public surface is .close().
    consumer = session._consumer
    real_close = consumer.close
    attempts: list[bool] = []

    def flaky_close() -> None:
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("simulated consumer close failure")
        real_close()

    monkeypatch.setattr(consumer, "close", flaky_close)
    _, _, second = _approved(env, host=CollaborationHost(env.cursor_root), run_id="run-2")

    assert _refused(session.deactivate).code == "storage"
    assert len(attempts) == 1
    assert session.active is True  # ownership stays reachable
    # The lease is still held, so the second owner is still refused.
    assert _refused(second.activate, env.workspace).code == "busy"

    session.deactivate()
    assert len(attempts) == 2
    assert session.active is False
    _activate_and_stream(env, second)


def test_the_checkpoint_lease_is_exclusive_across_processes(env):
    _, _, session = _approved(env)
    _activate_and_stream(env, session)
    lock = _lock_file(env)
    session.deactivate()
    repo_root = str(Path(__file__).resolve().parents[1])

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\n"
         "from pathlib import Path\n"
         f"sys.path.insert(0, {repo_root!r})\n"
         "from collab_runtime.host import _CheckpointLease\n"
         "lease = _CheckpointLease(Path(sys.argv[1]))\n"
         "print('held', flush=True)\n"
         "sys.stdin.readline()\n"
         "lease.close()\n",
         str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(child.stdout.readline()), daemon=True)
    reader.start()
    reader.join(20)
    assert line == ["held\n"], f"the child never acquired the lease: {line}"

    # A separate process holds the same path: both this process and a real run
    # activation are refused.
    assert _refused(_CheckpointLease, lock).code == "busy"
    assert _refused(session.activate, env.workspace).code == "busy"

    child.stdin.write("release\n")
    child.stdin.flush()
    child.wait(20)
    reader.join(5)
    assert child.returncode == 0, child.stderr.read()

    lease = _CheckpointLease(lock)
    try:
        assert lease.fd is not None
    finally:
        lease.close()
    _activate_and_stream(env, session)


# -------------------------------------------------------------- 16. storage


@pytest.mark.skipif(fcntl is None or not Path("/proc/self/fd").is_dir(),
                    reason="flock failure injection and descriptor accounting are POSIX-specific")
def test_a_failing_lock_leaves_no_open_descriptor_behind(env, monkeypatch):
    directory = _private_cursor_dir(env.cursor_root)
    probe = directory / "probe.lock"
    assert _descriptors_under(directory) == 0

    def exploding_flock(fd, operation):
        raise OSError(errno.EIO, "simulated flock failure")

    monkeypatch.setattr(fcntl, "flock", exploding_flock)
    for _ in range(16):
        assert _refused(_CheckpointLease, probe).code == "storage"
    assert _descriptors_under(directory) == 0

    # The busy path must not leak either: exactly one descriptor stays open.
    monkeypatch.undo()
    held = _CheckpointLease(probe)
    try:
        assert _refused(_CheckpointLease, probe).code == "busy"
        assert _descriptors_under(directory) == 1
    finally:
        held.close()
    assert _descriptors_under(directory) == 0


def test_the_private_namespace_refuses_symlinked_permissive_and_foreign_files(env):
    link = env.trust / "link"
    try:
        link.symlink_to(env.trust, target_is_directory=True)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        pytest.skip("symlink creation requires platform permission")
    assert _refused(_private_cursor_dir, link).code == "storage"

    permissive = env.trust / "permissive"
    permissive.mkdir(mode=0o755)
    assert _refused(_private_cursor_dir, permissive).code == "storage"

    directory = _private_cursor_dir(env.cursor_root)
    assert directory == env.cursor_root
    assert directory.stat().st_mode & 0o777 == 0o700
    path = directory / "checkpoint.lock"

    path.mkdir()
    assert _refused(_CheckpointLease, path).code == "storage"
    path.rmdir()

    target = directory / "elsewhere.lock"
    target.write_bytes(b"")
    try:
        path.symlink_to(target)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        pytest.skip("symlink creation requires platform permission")
    assert _refused(_CheckpointLease, path).code == "storage"
    path.unlink()

    path.write_bytes(b"x" * 65)
    assert _refused(_CheckpointLease, path).code == "storage"
    path.unlink()

    path.write_bytes(b"")
    path.chmod(0o644)
    assert _refused(_CheckpointLease, path).code == "storage"
    path.chmod(0o600)

    lease = _CheckpointLease(path)
    lease.close()
    assert path.is_file()
