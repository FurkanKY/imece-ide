"""decision_runtime.baseline.run_baseline_check — real git worktree rerun.

Uses an actual throwaway git repository (never the surrounding suite's own
repo) to prove: the baseline rerun sees the PRE-CHANGE content (not the
current, uncommitted working tree), the temp worktree is always cleaned up
(even the git-internal administrative entry), and any missing-shape/failure
case degrades to None rather than raising (see module docstring: "a
best-effort SIGNAL, not a requirement").
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.baseline import run_baseline_check  # noqa: E402
from process_runtime.models import ProcessRequest  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "value.txt").write_text("baseline-content\n")
    _git(["add", "value.txt"], root)
    _git(["commit", "-q", "-m", "baseline"], root)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
    ).stdout.strip()
    return commit


def _fake_workspace(root: Path, snapshot_commit: str):
    return SimpleNamespace(root=root, snapshot=SimpleNamespace(snapshot_commit=snapshot_commit))


def _cat_request() -> ProcessRequest:
    return ProcessRequest(argv=(sys.executable, "-c", "print(open('value.txt').read(), end='')"))


def test_baseline_rerun_sees_pre_change_content_not_current_worktree(tmp_path):
    root = tmp_path / "repo"
    baseline_commit = _init_repo(root)
    # Simulate the run having since changed the file, uncommitted — the real
    # scenario this module exists for (a failing check on the CURRENT tree).
    (root / "value.txt").write_text("changed-content\n")

    workspace = _fake_workspace(root, baseline_commit)
    result = run_baseline_check(workspace, _cat_request())

    assert result is not None
    assert result.exit_code == 0
    assert result.stdout == "baseline-content\n"
    # the run's own worktree must be untouched by the baseline rerun.
    assert (root / "value.txt").read_text() == "changed-content\n"


def test_baseline_rerun_cleans_up_the_temp_worktree(tmp_path):
    root = tmp_path / "repo"
    baseline_commit = _init_repo(root)
    workspace = _fake_workspace(root, baseline_commit)

    run_baseline_check(workspace, _cat_request())

    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(root), check=True, capture_output=True, text=True,
    ).stdout
    # Only the main worktree (root itself) should remain registered.
    worktree_paths = [line.split(" ", 1)[1] for line in listing.splitlines() if line.startswith("worktree ")]
    assert len(worktree_paths) == 1


def test_returns_none_when_workspace_has_no_snapshot_commit(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    workspace = SimpleNamespace(root=root)  # no .snapshot at all
    assert run_baseline_check(workspace, _cat_request()) is None


def test_returns_none_for_an_unknown_baseline_commit(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    workspace = _fake_workspace(root, "0" * 40)  # well-formed but nonexistent sha
    assert run_baseline_check(workspace, _cat_request()) is None


def test_returns_none_when_root_is_not_a_git_repository(tmp_path):
    root = tmp_path / "not_a_repo"
    root.mkdir()
    workspace = _fake_workspace(root, "deadbeef")
    assert run_baseline_check(workspace, _cat_request()) is None
