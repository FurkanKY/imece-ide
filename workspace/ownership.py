"""Durable workspace descriptors plus kernel-held, process-exclusive ownership.

SQLite events are the authority; lock files contain no prompt or credentials.
A free lock alone NEVER proves crashed subprocesses stopped: only a persisted
quiescent seal permits restart. Unsealed crash workspaces remain read-only.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import stat

from workspace.errors import WorkspaceError
from workspace.snapshot import WorkspaceSnapshot
from workspace.worktree import GitWorktreeWorkspace, _git_text


class OwnershipError(WorkspaceError):
    pass


class WorkspaceLease:
    def __init__(self, fd: int):
        self.fd = fd

    @classmethod
    def acquire(cls, db_path, run_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise OwnershipError("Invalid workspace identity")
        directory = Path(db_path).absolute().parent / "workspace-leases"
        for ancestor in (directory, *directory.parents):
            if ancestor.is_symlink():
                raise OwnershipError("Unsafe lease directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (run_id + ".lock")
        if path.is_symlink():
            raise OwnershipError("Unsafe lease file")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            opened = os.fstat(fd)
            named = path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                raise OwnershipError("Unsafe lease identity")
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt
                if opened.st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                raise OwnershipError("Workspace leases unsupported on this platform")
        except Exception as exc:
            os.close(fd)
            raise OwnershipError("Workspace is owned or its lease is unsafe") from exc
        return cls(fd)

    def close(self):
        if self.fd is not None:
            # Do not unlink or explicitly unlock: the inode must remain stable.
            os.close(self.fd)
            self.fd = None


def _descriptor(workspace):
    snapshot = workspace.snapshot
    return {
        "version": 1, "state": "busy", "run_id": snapshot.run_id,
        "source_root": str(snapshot.source_root), "repository_root": str(snapshot.repository_root),
        "source_head": snapshot.source_head, "snapshot_commit": snapshot.snapshot_commit,
        "project_relative_root": snapshot.project_relative_root.as_posix(),
        "worktree_dir": str(workspace._worktree_dir),
        "created_at": snapshot.created_at.isoformat(),
        "tracked_dirty_paths": list(snapshot.tracked_dirty_paths),
        "untracked_paths": list(snapshot.untracked_paths),
    }


def _parse_worktree_porcelain_z(raw: str) -> tuple[str, ...]:
    """Parse NUL-delimited `git worktree list --porcelain -z` records."""
    return tuple(record[len("worktree "):] for record in raw.split("\0")
                 if record.startswith("worktree "))


def _worktree_path_matches(registered: str, expected: Path) -> bool:
    candidate = registered.replace("\\", os.sep).replace("/", os.sep)
    wanted = str(expected).replace("\\", os.sep).replace("/", os.sep)
    return os.path.normcase(os.path.normpath(candidate)) == os.path.normcase(os.path.normpath(wanted))


def _restore(descriptor, project_root):
    if descriptor.get("version") != 1 or descriptor.get("state") != "quiescent":
        raise OwnershipError("No safely sealed workspace")
    source = Path(descriptor["source_root"])
    repo = Path(descriptor["repository_root"])
    directory = Path(descriptor["worktree_dir"])
    relative = Path(descriptor["project_relative_root"])
    if (str(source) != project_root or not source.is_absolute() or not repo.is_absolute()
            or not directory.is_absolute() or relative.is_absolute() or ".." in relative.parts
            or source != repo / relative or directory == repo or repo in directory.parents):
        raise OwnershipError("Workspace project identity mismatch")
    for path in (source, directory, repo):
        if any(ancestor.is_symlink() for ancestor in (path, *path.parents)):
            raise OwnershipError("Workspace traverses a symbolic link")
    if os.name == "nt":
        from workspace.windows_safety import validate_directory_chain
        if not all(validate_directory_chain(path) for path in (source, directory, repo)):
            raise OwnershipError("Workspace traverses a Win32 reparse point or unstable directory")
    if not directory.is_dir() or directory.name != descriptor["run_id"]:
        raise OwnershipError("Workspace is missing or renamed")
    if os.name == "nt":
        from workspace.windows_safety import pinned_regular_file
        metadata_guard = pinned_regular_file(directory / ".git")
    else:
        from contextlib import nullcontext
        if (directory / ".git").is_symlink():
            raise OwnershipError("Workspace Git metadata is a symlink")
        metadata_guard = nullcontext(None)
    with metadata_guard:
        return _restore_git_identity(descriptor, source, repo, directory, relative)


def _restore_git_identity(descriptor, source, repo, directory, relative):
    if os.name == "nt":
        from workspace.windows_safety import validate_directory_chain
        if not all(validate_directory_chain(path) for path in (source, directory, repo)):
            raise OwnershipError("Workspace path changed during Git identity validation")
    if _git_text(["rev-parse", "HEAD"], cwd=repo).strip() != descriptor["source_head"]:
        raise OwnershipError("Source baseline changed")
    head = descriptor["snapshot_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise OwnershipError("Invalid workspace baseline")
    # Real registered detached worktree, in the expected repository, at the
    # original immutable baseline. A user-created lookalike is not adopted.
    common = Path(_git_text(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=directory).strip())
    expected_common = Path(_git_text(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo).strip())
    listing = _git_text(["worktree", "list", "--porcelain", "-z"], cwd=repo)
    registered = _parse_worktree_porcelain_z(listing)
    if (common != expected_common or not any(_worktree_path_matches(path, directory) for path in registered)
            or _git_text(["rev-parse", "HEAD"], cwd=directory).strip() != head):
        raise OwnershipError("Workspace Git identity mismatch")
    from workspace.worktree import _run_git
    if _run_git(["symbolic-ref", "-q", "HEAD"], cwd=directory, check=False).returncode != 1:
        raise OwnershipError("Workspace is not detached")
    snapshot = WorkspaceSnapshot(
        run_id=descriptor["run_id"], source_root=source, repository_root=repo,
        source_head=descriptor["source_head"], snapshot_commit=head,
        project_relative_root=relative, created_at=datetime.fromisoformat(descriptor["created_at"]),
        tracked_dirty_paths=tuple(descriptor["tracked_dirty_paths"]),
        untracked_paths=tuple(descriptor["untracked_paths"]),
    )
    return GitWorktreeWorkspace(root=directory / relative, worktree_dir=directory,
                               repo_root=repo, snapshot=snapshot)


class WorkspaceOwnership:
    def __init__(self, runtime, run_id, workspace, lease, descriptor):
        self.runtime, self.run_id, self.workspace = runtime, run_id, workspace
        self.lease, self.descriptor = lease, descriptor

    @classmethod
    def attach(cls, runtime, run_id, workspace):
        if not isinstance(workspace, GitWorktreeWorkspace):
            return None  # Legacy/test adapters do not claim durable ownership.
        lease = WorkspaceLease.acquire(runtime.store.db_path, run_id)
        owner = cls(runtime, run_id, workspace, lease, _descriptor(workspace))
        try:
            owner._save("busy")
        except Exception:
            lease.close()
            raise
        workspace.ownership = owner
        return owner

    @classmethod
    def adopt(cls, runtime, run_id, project_root):
        lease = WorkspaceLease.acquire(runtime.store.db_path, run_id)
        try:
            run = runtime.get_run(run_id)
            if run.status.value not in {"waiting_user", "cancelled", "failed", "interrupted"}:
                raise OwnershipError("Execution is not quiescent")
            task = runtime.store.get_task(run.task_id)
            if task.project_root != project_root or not 0 < len(task.prompt) <= 20_000:
                raise OwnershipError("Task project mismatch")
            provider = run.routing.get("agent_provider")
            if type(provider) is not str or not 0 < len(provider) <= 128:
                raise OwnershipError("Not a native execution")
            descriptor = dict(run.workspace_snapshot or {})
            if descriptor.get("run_id") != run_id:
                raise OwnershipError("Workspace run identity mismatch")
            workspace = _restore(descriptor, project_root)
            owner = cls(runtime, run_id, workspace, lease, descriptor)
            owner._require_producer_quiescence()
            from agent_execution_runtime.execution import _workspace_fingerprint
            digest, complete = _workspace_fingerprint(workspace)
            if not complete or digest != descriptor.get("fingerprint"):
                raise OwnershipError("Workspace changed after its quiescent seal")
            from change_runtime.git import GitWorktreeChangeProvider
            source_view = GitWorktreeWorkspace(root=workspace.snapshot.source_root,
                worktree_dir=workspace.snapshot.repository_root, repo_root=workspace.snapshot.repository_root,
                snapshot=workspace.snapshot)
            if GitWorktreeChangeProvider().capture(source_view).changed_paths:
                raise OwnershipError("Source changed since execution began")
            # CAS binds ownership to the exact canonical trajectory inspected.
            runtime.record(run_id=run_id, type="workspace.claimed", payload={},
                           expected_last_event_seq=run.last_event_seq, source="workspace_ownership")
            workspace.ownership = owner
            return owner
        except Exception:
            lease.close()
            raise

    def _save(self, state, **extra):
        descriptor = {**self.descriptor, "state": state, **extra}
        self.runtime.record(run_id=self.run_id, type="workspace.saved", payload=descriptor,
                            source="workspace_ownership")
        self.descriptor = descriptor

    def busy(self):
        if self.lease.fd is None:
            raise OwnershipError("Workspace lease was relinquished")
        self._save("busy", fingerprint=None)

    def _require_producer_quiescence(self):
        run = self.runtime.get_run(self.run_id)
        import engine_factory
        is_acp = run.routing.get("agent_provider") in engine_factory._ACP_CLI_PRESETS
        latest_acp_execution = None
        completed_acp = set()
        after = 0
        count = 0
        outstanding_tools = {}
        outstanding_checks = {}
        pure_workspace_tools = {
            "read_file", "write_file", "list_files", "search_text", "delete_path",
            "repo_map", "search_code",
        }
        while True:
            page = self.runtime.events(self.run_id, after_seq=after, limit=200)
            for event in page.events:
                count += 1
                if count > 2000:
                    raise OwnershipError("Producer history exceeds seal validation bound")
                payload = event.payload
                if is_acp and event.type == "execution.started":
                    latest_acp_execution = event.execution_id
                elif is_acp and event.type in {"execution.completed", "execution.failed"}:
                    if payload.get("transport") == "acp" and payload.get("producer_quiescent") is True:
                        completed_acp.add(event.execution_id)
                if event.type == "tool.started":
                    name = payload.get("tool_name")
                    if name not in pure_workspace_tools:
                        key = (event.execution_id, payload.get("call_id"))
                        if key in outstanding_tools:
                            raise OwnershipError("Duplicate process tool start is ambiguous")
                        outstanding_tools[key] = name
                elif event.type == "tool.completed":
                    key = (event.execution_id, payload.get("call_id"))
                    if key in outstanding_tools:
                        started_name = outstanding_tools[key]
                        metadata = payload.get("metadata") or {}
                        if (payload.get("tool_name") != started_name
                                or metadata.get("producer_quiescent") is not True):
                            raise OwnershipError("Process tool lacks matching supervisor quiescence receipt")
                        outstanding_tools.pop(key)
                elif event.type in {"tool.failed", "tool.interrupted"}:
                    key = (event.execution_id, payload.get("call_id"))
                    if key in outstanding_tools:
                        if payload.get("metadata", {}).get("producer_quiescent") is not True:
                            raise OwnershipError("Process tool ended without matching quiescence receipt")
                        outstanding_tools.pop(key)
                elif event.type == "verification.check_started":
                    key = (event.execution_id, payload.get("verification_id"), payload.get("check_id"))
                    if key in outstanding_checks:
                        raise OwnershipError("Duplicate verification check start is ambiguous")
                    outstanding_checks[key] = True
                elif event.type in {"verification.check_completed", "verification.check_failed", "verification.check_interrupted"}:
                    key = (event.execution_id, payload.get("verification_id"), payload.get("check_id"))
                    if key in outstanding_checks:
                        if payload.get("producer_quiescent") is not True:
                            raise OwnershipError("Verification check lacks matching supervisor quiescence receipt")
                        outstanding_checks.pop(key)
            if not page.has_more:
                break
            if not page.events or page.events[-1].seq <= after:
                raise OwnershipError("Incomplete producer history")
            after = page.events[-1].seq
        if outstanding_tools or outstanding_checks:
            raise OwnershipError("Producer completion lacks quiescence receipt")
        if is_acp and (latest_acp_execution is None or latest_acp_execution not in completed_acp):
            raise OwnershipError("ACP execution lacks authenticated producer quiescence receipt")

    def seal(self):
        if self.lease.fd is None or self.workspace._disposed:
            return
        if self.runtime.get_run(self.run_id).status.value not in {"waiting_user", "cancelled", "failed", "interrupted"}:
            return
        from agent_execution_runtime.execution import _workspace_fingerprint
        digest, complete = _workspace_fingerprint(self.workspace)
        if not complete:
            raise OwnershipError("Workspace fingerprint is incomplete; restart is unavailable")
        self._require_producer_quiescence()
        self._save("quiescent", fingerprint=digest)

    def stash(self):
        self.seal()
        if self.descriptor.get("state") != "quiescent":
            raise OwnershipError("Cannot relinquish an unsealed producer workspace")
        self.lease.close()

    def disposed(self):
        self._save("disposed", fingerprint=None)
        self.lease.close()


def continuation_available(runtime, project_root, run_id):
    """An advisory descriptor only; actual claim always revalidates under lease."""
    try:
        run = runtime.get_run(run_id)
        descriptor = run.workspace_snapshot or {}
        return (run.status.value in {"waiting_user", "cancelled", "failed", "interrupted"}
                and descriptor.get("version") == 1 and descriptor.get("state") == "quiescent"
                and descriptor.get("run_id") == run_id
                and descriptor.get("source_root") == project_root)
    except Exception:
        return False
