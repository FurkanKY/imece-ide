"""Negative durable owner-shared candidate workflow regressions."""
import json
import subprocess
import sys

import pytest

from change_runtime.candidate import CombinedCandidates
from collab_runtime.candidates import CandidateConflictError
from collab_runtime.context import SharedSnapshot
from collab_runtime.errors import StaleRevisionError
from collab_runtime.models import build_task
from collab_runtime.owner import OwnerError, OwnerSessionManager
from collab_runtime.proposals import capture_proposal, publish_proposal
from run_runtime import RunRuntime, RunStore


def git(root, *args):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), *args], text=True).strip()


def shared_world(tmp_path, *, conflict=False):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("base\n")
    (source / ".imece").mkdir()
    (source / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "ok", "title": "valid", "argv": [sys.executable, "-c", "pass"], "timeout_ms": 5000,
    }]))
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    git(source, "add", ".")
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
    base = git(source, "rev-parse", "HEAD")
    manager = OwnerSessionManager(tmp_path / "private")
    tasks = [
        {"id": "task-a", "owner": "bob", "goal": "A", "scopes": ["a.txt"]},
        {"id": "task-b", "owner": "carol", "goal": "B", "scopes": ["a.txt" if conflict else "other.txt"]},
    ]
    plan = manager.preview_create(source, session_id="shared", target_version="v1", goal="goal",
                                  owner_id="alice", member_ids=["alice", "bob", "carol"], tasks=tasks)
    manager.create(plan["previewId"], source)
    return source, base, manager


def publish_change(manager, source, proposal_id, task, replacement):
    store = manager._config["store"]
    original = (source / "a.txt").read_bytes()
    (source / "a.txt").write_text(replacement)
    revision, state = store.fetch_state()
    binding = SharedSnapshot(revision=revision, context_hash=state.context_hash, state=state, task_id=task)
    proposal = capture_proposal(store, source, proposal_id, task, ["a.txt"], revision, binding=binding)
    publish_proposal(store, proposal, expected_revision=revision)
    (source / "a.txt").write_bytes(original)


def candidate_service(tmp_path, manager, runtime_path="runs.sqlite3"):
    runtime = RunRuntime(RunStore(tmp_path / runtime_path))
    output = tmp_path / "candidates"
    service = CombinedCandidates(runtime, output,
        owner_context_supplier=lambda root, provenance: manager.validate_shared_candidate(root, provenance))
    return service, output


def source_identity(source):
    status = git(source, "status", "--porcelain", "--untracked-files=all")
    return ((source / "a.txt").read_bytes(), (source / ".git" / "index").read_bytes(),
            git(source, "rev-parse", "HEAD"), status)


def reassign(manager, task_id, owner):
    store = manager._config["store"]
    revision, state = store.fetch_state()
    previous = state.tasks[task_id]
    changed = build_task(task_id=task_id, owner=owner, goal=previous.goal, scopes=list(previous.scopes),
                         status=previous.status, context_revision=previous.context_revision)
    return store.publish(state.with_task(changed), expected_revision=revision)


def test_conflicting_shared_proposals_create_no_candidate_or_source_change(tmp_path):
    source, base, manager = shared_world(tmp_path, conflict=True)
    try:
        publish_change(manager, source, "proposal-a", "task-a", "from-a\n")
        publish_change(manager, source, "proposal-b", "task-b", "from-b\n")
        identity = manager.shared_candidate_store(source, expected_session_id="shared",
                                                   expected_epoch=manager.status()["epoch"])
        service, output = candidate_service(tmp_path, manager)
        before = source_identity(source)
        with pytest.raises(CandidateConflictError, match="the selected proposals conflict on one or more paths; no candidate directory was created and verification was not run.") as failure:
            service.prepare_shared(source, identity["store"], ["proposal-a", "proposal-b"], verify=True,
                expected_revision=identity["revision"], session_id="shared", epoch=identity["epoch"],
                store_path=identity["storePath"], hub_path=identity["hubPath"])
        assert failure.value.conflict_paths == ["a.txt"]
        assert source_identity(source) == before
        assert git(source, "rev-parse", "HEAD") == base
        assert not output.exists()
        assert service.list(str(source)) == []
    finally:
        manager.stop()


def test_published_proposal_reassigned_before_prepare_is_rejected(tmp_path):
    source, _base, manager = shared_world(tmp_path)
    try:
        publish_change(manager, source, "proposal-a", "task-a", "proposal\n")
        reassign(manager, "task-a", "carol")
        identity = manager.shared_candidate_store(source, expected_session_id="shared",
                                                   expected_epoch=manager.status()["epoch"])
        service, output = candidate_service(tmp_path, manager)
        before = source_identity(source)
        with pytest.raises(StaleRevisionError, match="the selected task was superseded") as failure:
            service.prepare_shared(source, identity["store"], ["proposal-a"], verify=True,
                expected_revision=identity["revision"], session_id="shared", epoch=identity["epoch"],
                store_path=identity["storePath"], hub_path=identity["hubPath"])
        assert str(failure.value) == "the selected task was superseded (owner, goal or scopes changed); re-capture."
        assert source_identity(source) == before
        assert not output.exists()
        assert service.list(str(source)) == []
    finally:
        manager.stop()


def test_prepared_candidate_reassignment_refuses_apply_without_authority(tmp_path):
    source, base, manager = shared_world(tmp_path)
    try:
        publish_change(manager, source, "proposal-a", "task-a", "proposal\n")
        identity = manager.shared_candidate_store(source, expected_session_id="shared",
                                                   expected_epoch=manager.status()["epoch"])
        service, _output = candidate_service(tmp_path, manager)
        receipt = service.prepare_shared(source, identity["store"], ["proposal-a"], verify=True,
            expected_revision=identity["revision"], session_id="shared", epoch=identity["epoch"],
            store_path=identity["storePath"], hub_path=identity["hubPath"])
        assert receipt["verification"]["status"] == "pass"
        assert receipt["state"] == "prepared"
        reassign(manager, "task-a", "carol")
        before = source_identity(source)
        with pytest.raises(OwnerError, match="product_stale"):
            service.apply(str(source), receipt["candidateId"])
        assert source_identity(source) == before
        assert git(source, "rev-parse", "HEAD") == base
        current = service.get(str(source), receipt["candidateId"])
        assert current["state"] == "prepared"
        assert not current.get("checkpointId")
        events = service.runtime.events(receipt["candidateId"], after_seq=0, limit=20)
        assert not any(event.type == "candidate.applied" for event in events.events)
    finally:
        manager.stop()
