"""Baseline verification rerun (docs/JEV-DESIGN.md Spike S1, "Plan" step S1a).

When a check fails, this reruns the SAME check against the pre-change
baseline (the run's own snapshot commit — see workspace.worktree's
"synthetic snapshot" docstring) to learn whether the failure is
pre-existing. This is the single highest-value deterministic fact for
verification failure triage: "The baseline run alone removes a whole class
of wrong decisions."

Safety, mirroring change_runtime.git's approach for the same reason (never
touch the user's real repository or the run's own worktree):

  - a throwaway, DETACHED `git worktree add` at the baseline commit, in a
    brand-new temp directory outside both the user's repo and the run's
    worktree;
  - argv-only subprocess calls (shell=False), `--no-optional-locks` +
    `-c core.fsmonitor=` on every git invocation (see change_runtime.git's
    docstring for why);
  - the check itself runs via process_runtime.ProcessRunner against that
    temp worktree's root only — it can never see or touch the run's actual
    (possibly modified) worktree;
  - the temp worktree and its git-internal administrative entry are always
    removed in a `finally`, even on error.

This is a best-effort SIGNAL, not a requirement: any failure anywhere in
this module (no baseline commit known, git not available, worktree add
failing, the check itself erroring) returns None rather than raising, so a
broken/unavailable baseline rerun degrades to "no baseline evidence" and
the pipeline falls back to today's behaviour — never to a hard failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from process_runtime.models import ProcessRequest, ProcessResult
from process_runtime.runner import ProcessRunner

_GIT_TIMEOUT_S = 60


@dataclass(frozen=True, slots=True)
class _BaselineWorkspace:
    """Minimal stand-in for a Workspace: ProcessRunner only ever reads `.root`
    (see process_runtime.runner._cwd_path/_resolve_executable) — a full
    GitWorktreeWorkspace is unnecessary and would require a real run_id/
    snapshot lifecycle this throwaway directory never has."""

    root: Path


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PYTHONUTF8": "1", "LC_ALL": "C.UTF-8"}


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=", *args],
            cwd=str(cwd),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def run_baseline_check(workspace, request: ProcessRequest) -> ProcessResult | None:
    """Rerun `request` (a verification check's own ProcessRequest) against the
    workspace's pre-change baseline commit.

    `workspace` must expose `.root` (Path) and `.snapshot.snapshot_commit`
    (str) — the same shape `change_runtime.git.GitWorktreeChangeProvider`
    already relies on. Returns None (never raises) whenever that shape is
    absent or any step fails; see module docstring.
    """
    root = getattr(workspace, "root", None)
    snapshot = getattr(workspace, "snapshot", None)
    baseline_commit = getattr(snapshot, "snapshot_commit", None)
    if not isinstance(root, Path) or not isinstance(baseline_commit, str) or not baseline_commit:
        return None

    tmp_parent = Path(tempfile.mkdtemp(prefix="imece-decision-baseline-"))
    tmp_worktree = tmp_parent / "wt"
    try:
        added = _run_git(
            ["worktree", "add", "--detach", "--force", str(tmp_worktree), baseline_commit],
            cwd=root,
        )
        if added is None or added.returncode != 0:
            return None
        try:
            return ProcessRunner().run(_BaselineWorkspace(root=tmp_worktree), request)
        except Exception:
            return None
    finally:
        _cleanup(root, tmp_worktree, tmp_parent)


def _cleanup(root: Path, tmp_worktree: Path, tmp_parent: Path) -> None:
    _run_git(["worktree", "remove", "--force", str(tmp_worktree)], cwd=root)
    shutil.rmtree(tmp_parent, ignore_errors=True)
    _run_git(["worktree", "prune"], cwd=root)
