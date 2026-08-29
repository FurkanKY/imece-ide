"""Shared process-tree termination, reused by ProcessRunner and acp_runtime.

`terminate_process_tree` was originally moved from
process_runtime/runner.py's private _terminate_tree, and its no-snapshot
behavior preserves the legacy ProcessRunner contract/semantics for existing
callers. The optional `snapshot` parameter (hardening round 2) is strictly
additive: it lets a caller capture a process-tree identity
BEFORE a graceful operation that might cause the root process to exit and
orphan/reparent its descendants -- a fresh psutil tree-walk from a dead root
pid cannot discover already-reparented descendants, but psutil.Process
objects captured while the tree was still intact remain valid handles
(psutil tracks pid+create_time identity internally, so a captured handle is
never confused with an unrelated process that later reuses the same pid).
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from process_runtime.errors import ProcessCleanupError


@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshot:
    """A point-in-time capture of one process tree's identity.

    `root` is None if the root process no longer existed at capture time.
    `descendants` are the recursive children discoverable at capture time.
    Both are live psutil.Process handles (not bare ints), so termination
    against a stale snapshot cannot accidentally target an unrelated process
    that has since reused the same pid.
    """

    root: "psutil.Process | None"
    descendants: tuple["psutil.Process", ...]


def capture_process_tree(pid: int) -> ProcessTreeSnapshot:
    """Best-effort snapshot of `pid` and its recursive descendants, taken
    right now. Call this BEFORE any operation that might cause the root
    process to exit, so orphaned/reparented descendants remain findable."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return ProcessTreeSnapshot(root=None, descendants=())
    except psutil.Error as exc:
        raise ProcessCleanupError(f"Could not inspect process tree: {exc}") from exc
    try:
        descendants = tuple(root.children(recursive=True))
    except psutil.NoSuchProcess:
        return ProcessTreeSnapshot(root=None, descendants=())
    except psutil.Error as exc:
        raise ProcessCleanupError(f"Could not inspect process tree: {exc}") from exc
    return ProcessTreeSnapshot(root=root, descendants=descendants)


def _identity(process: "psutil.Process") -> tuple[int, float] | None:
    try:
        return (process.pid, process.create_time())
    except psutil.Error:
        return None


def terminate_process_tree(pid: int, *, snapshot: ProcessTreeSnapshot | None = None) -> None:
    """Terminate `pid` and all of its (currently discoverable) descendants.

    With no snapshot, this preserves the legacy no-snapshot ProcessRunner
    contract/semantics: a single fresh tree-walk from `pid`. With a snapshot, the
    processes it captured are unioned with a fresh rescan from `pid` (which
    may find NEW descendants spawned since the snapshot, or may find nothing
    if the root has since exited -- in which case the snapshot's captured
    handles are what let orphaned descendants still be reached).
    """
    candidates: list["psutil.Process"] = []
    if snapshot is not None:
        if snapshot.root is not None:
            candidates.append(snapshot.root)
        candidates.extend(snapshot.descendants)

    try:
        fresh_root: "psutil.Process | None" = psutil.Process(pid)
    except psutil.NoSuchProcess:
        fresh_root = None
    except psutil.Error as exc:
        raise ProcessCleanupError(f"Could not inspect process tree: {exc}") from exc

    if fresh_root is not None:
        candidates.append(fresh_root)
        try:
            candidates.extend(fresh_root.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            raise ProcessCleanupError(f"Could not inspect process tree: {exc}") from exc

    unique: dict[tuple[int, float], "psutil.Process"] = {}
    for process in candidates:
        identity = _identity(process)
        if identity is None:
            continue
        unique.setdefault(identity, process)
    processes = list(unique.values())

    if not processes:
        return

    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            continue
    _, survivors = psutil.wait_procs(processes, timeout=0.5)
    for process in survivors:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            continue
    _, survivors = psutil.wait_procs(survivors, timeout=1.0)
    if survivors:
        raise ProcessCleanupError(
            "Process timeout cleanup left survivors: "
            + ", ".join(str(process.pid) for process in survivors)
        )
