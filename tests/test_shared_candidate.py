"""Owner-shared proposal assembly uses durable native candidate receipts."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from change_runtime.candidate import CombinedCandidates
from collab_runtime.context import SharedSnapshot
from collab_runtime.owner import OwnerSessionManager
from collab_runtime.proposals import capture_proposal, publish_proposal
from run_runtime import RunRuntime, RunStore


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.mark.skipif(os.name == "nt", reason="mode assertions require POSIX")
def test_shared_prepare_apply_reopen_rollback(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("base-a\n")
    (source / "b.txt").write_text("base-b\n")
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
    plan = manager.preview_create(source, session_id="shared", target_version="v1", goal="goal",
        owner_id="alice", member_ids=["alice", "bob", "carol"], tasks=[
            {"id": "task-a", "owner": "bob", "goal": "A", "scopes": ["a.txt"]},
            {"id": "task-b", "owner": "carol", "goal": "B", "scopes": ["b.txt"]},
        ])
    manager.create(plan["previewId"], source)
    try:
        store = manager._config["store"]
        for pid, task, path, replacement in (("proposal-a", "task-a", "a.txt", "new-a\n"),
                                               ("proposal-b", "task-b", "b.txt", "new-b\n")):
            original = (source / path).read_bytes()
            (source / path).write_text(replacement)
            revision, state = store.fetch_state()
            binding = SharedSnapshot(revision=revision, context_hash=state.context_hash,
                                     state=state, task_id=task)
            proposal = capture_proposal(store, source, pid, task, [path], revision, binding=binding)
            publish_proposal(store, proposal, expected_revision=revision)
            (source / path).write_bytes(original)
        identity = manager.shared_candidate_store(source, expected_session_id="shared", expected_epoch=manager.status()["epoch"])
        runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
        service = CombinedCandidates(runtime, tmp_path / "candidates",
            owner_context_supplier=lambda root, provenance: manager.validate_shared_candidate(root, provenance))
        receipt = service.prepare_shared(source, identity["store"], ["proposal-a", "proposal-b"],
            expected_revision=identity["revision"], verify=True, session_id="shared", epoch=identity["epoch"],
            store_path=identity["storePath"], hub_path=identity["hubPath"],
            context_supplier=lambda sid, epoch, rev, ctx: manager.validate_shared_candidate(source, {
                "sessionId": sid, "epoch": epoch, "revision": rev, "contextHash": ctx, "baseCommit": base,
                "storePath": identity["storePath"], "hubPath": identity["hubPath"]}))
        assert receipt["selected"] == []
        assert len(receipt["selectedProposals"]) == 2
        assert receipt["sharedProvenance"]["proposalIds"] == ["proposal-a", "proposal-b"]
        assert receipt["verification"]["status"] == "pass"
        assert (source / "a.txt").read_text() == "base-a\n"
        reopened = CombinedCandidates(RunRuntime(RunStore(runtime.store.db_path)), service.output_base,
            owner_context_supplier=lambda root, provenance: manager.validate_shared_candidate(root, provenance))
        result = reopened.apply(str(source), receipt["candidateId"])
        assert set(result["applied"]) == {"a.txt", "b.txt"}
        assert (source / "a.txt").read_text() == "new-a\n"
        rollback = reopened.rollback(str(source), receipt["candidateId"])
        assert set(rollback["restored"]) == {"a.txt", "b.txt"}
        assert (source / "a.txt").read_text() == "base-a\n"
        assert git(source, "rev-parse", "HEAD") == base
    finally:
        manager.stop()
