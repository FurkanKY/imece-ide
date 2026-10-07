"""Native result selection -> isolated verification -> explicit apply -> rollback."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from change_runtime.candidate import CombinedCandidates, CandidateError
from change_runtime.git import GitWorktreeChangeProvider
from run_runtime import RunRuntime, RunStore
from run_runtime.legacy import LegacyRunCoordinator
from webhost.run_registry import RunSlot
from workspace.worktree import GitWorktreeWorkspace

_STRICT_NOFOLLOW = os.name == "nt" or (os.scandir in os.supports_fd and os.open in os.supports_dir_fd)


@pytest.fixture
def candidates(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (source / "a.txt").write_text("".join(f"line{i}\n" for i in range(12)))
    (source / "delete.txt").write_text("old\n")
    (source / "script.sh").write_text("echo original\n")
    if os.name == "posix":
        (source / "script.sh").chmod(0o750)
    (source / ".imece").mkdir()
    (source / ".imece" / "verify.json").write_text(json.dumps([{
        "id": "combined", "title": "Combined output", "argv": [sys.executable, "-c",
        "from pathlib import Path; p=Path('a.txt').read_text(); assert p.startswith('A\\n') and p.endswith('B\\n')"],
        "timeout_ms": 5000,
    }]))
    git("add", ".")
    git("commit", "-m", "base")
    runtime = RunRuntime(RunStore(tmp_path / "runs.sqlite3"))
    slots = []
    for index in (0, 1):
        task = runtime.create_task(project_root=str(source), prompt=f"Edit {index}")
        run = runtime.create_run(task_id=task.task_id, routing={"agent_provider": "p"})
        runtime.record(run_id=run.run_id, type="run.started", payload={})
        workspace = GitWorktreeWorkspace.create(source_root=source, run_id=run.run_id,
                                                 base_dir=tmp_path / "workspaces")
        content = (source / "a.txt").read_text().replace("line0\n", "A\n") if index == 0 else (source / "a.txt").read_text().replace("line11\n", "B\n")
        (workspace.root / "a.txt").write_text(content)
        if index == 0:
            (workspace.root / "new.txt").write_text("no trailing newline")
            (workspace.root / "delete.txt").unlink()
            (workspace.root / "script.sh").write_text("echo changed\n")
        change = GitWorktreeChangeProvider().capture(workspace)
        runtime.record(run_id=run.run_id, type="run.waiting_user", payload={})
        coordinator = LegacyRunCoordinator(runtime, task_id=task.task_id, run_id=run.run_id,
                                           routing=run.routing)
        slots.append(RunSlot(run.run_id, task.task_id, str(source), "p", coordinator,
                             workspace=workspace, phase="waiting_user", proposals=[{"path": "a.txt"}],
                             evidence={"diff_sha256": change.diff_sha256}))
    service = CombinedCandidates(runtime, tmp_path / "candidates")
    yield source, runtime, slots, service
    for slot in slots:
        slot.workspace.dispose()


def test_combined_selection_verify_apply_rollback_survives_store_reopen(candidates):
    source, runtime, slots, service = candidates
    receipt = service.prepare(str(source), slots, verify=True)
    assert receipt["candidateDir"] != str(source)
    assert (source / "a.txt").read_text().startswith("line0")
    if not _STRICT_NOFOLLOW:
        assert receipt["verification"]["status"] != "pass"
        with pytest.raises(CandidateError):
            service.apply(str(source), receipt["candidateId"])
        return
    assert receipt["verification"]["status"] == "pass"
    assert not (source / "new.txt").exists()
    # Receipt persistence, not process-local ticket or browser storage.
    reopened = CombinedCandidates(RunRuntime(RunStore(runtime.store.db_path)), service.output_base)
    assert reopened.list(str(source))[0]["candidateId"] == receipt["candidateId"]
    applied = reopened.apply(str(source), receipt["candidateId"])
    assert set(applied["applied"]) == {"a.txt", "new.txt", "delete.txt", "script.sh"}
    assert (source / "a.txt").read_text().startswith("A\n")
    assert (source / "a.txt").read_text().endswith("B\n")
    assert (source / "new.txt").read_bytes() == b"no trailing newline"
    assert not (source / "delete.txt").exists()
    rollback = reopened.rollback(str(source), receipt["candidateId"])
    assert set(rollback["restored"]) == set(applied["applied"])
    assert (source / "a.txt").read_text().startswith("line0")
    assert not (source / "new.txt").exists()
    assert (source / "delete.txt").read_text() == "old\n"
    assert (source / "script.sh").read_text() == "echo original\n"
    if os.name == "posix":
        import stat
        assert stat.S_IMODE((source / "script.sh").stat().st_mode) == 0o750
    assert reopened.get(str(source), receipt["candidateId"])["state"] == "rolled_back"


@pytest.mark.parametrize("tamper", ["source", "candidate", "wrong_root", "not_verified"])
def test_apply_fails_closed_without_source_write(candidates, tamper):
    source, runtime, slots, service = candidates
    receipt = service.prepare(str(source), slots, verify=tamper != "not_verified")
    root = str(source)
    if tamper == "source":
        (source / "a.txt").write_text("user edits\n")
    elif tamper == "candidate":
        (Path(receipt["candidateDir"]) / "a.txt").write_text("tampered\n")
    elif tamper == "wrong_root":
        root += "/other"
    before = (source / "a.txt").read_bytes()
    with pytest.raises((CandidateError, OSError, ValueError)):
        service.apply(root, receipt["candidateId"])
    assert (source / "a.txt").read_bytes() == before
    assert not (source / "new.txt").exists()


def test_conflicting_results_are_not_materialized(candidates):
    source, runtime, slots, service = candidates
    (slots[1].workspace.root / "a.txt").write_text((source / "a.txt").read_text().replace("line0\n", "conflict\n"))
    slots[1].evidence = {"diff_sha256": GitWorktreeChangeProvider().capture(slots[1].workspace).diff_sha256}
    from collab_runtime.candidates import CandidateConflictError
    with pytest.raises(CandidateConflictError):
        service.prepare(str(source), slots, verify=True)
    assert not service.output_base.exists() or not list(service.output_base.iterdir())


@pytest.mark.skipif(not _STRICT_NOFOLLOW, reason="Strict verification fingerprint unavailable")
def test_rollback_refuses_user_edits_and_apply_persistence_failure_restores_source(candidates, monkeypatch):
    source, runtime, slots, service = candidates
    receipt = service.prepare(str(source), slots, verify=True)
    record = runtime.record
    def failing_record(**kwargs):
        if kwargs["type"] == "candidate.applied":
            raise RuntimeError("injected canonical failure")
        return record(**kwargs)
    monkeypatch.setattr(runtime, "record", failing_record)
    with pytest.raises(RuntimeError):
        service.apply(str(source), receipt["candidateId"])
    assert (source / "a.txt").read_text().startswith("line0")
    assert not (source / "new.txt").exists()
    monkeypatch.setattr(runtime, "record", record)
    service.apply(str(source), receipt["candidateId"])
    (source / "new.txt").write_text("user edit")
    with pytest.raises(CandidateError):
        service.rollback(str(source), receipt["candidateId"])
    assert (source / "new.txt").read_text() == "user edit"
