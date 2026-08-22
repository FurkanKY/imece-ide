"""GitWorktreeWorkspace için entegrasyon testleri (geçici git repoları).

Git yalnızca genel olarak mevcut değilse atlanır (bkz. pytestmark). Testler
kullanıcının GLOBAL git yapılandırmasına bağımlı olmamak için her repoda
yerel `user.name`/`user.email` ayarlar ve commit kimliğini env değişkenleri
üzerinden verir.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace.errors import (  # noqa: E402
    UnsupportedRepositoryStateError,
    WorkspaceCleanupError,
    WorkspaceCreationError,
)
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, cwd, check=True):
    cp = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_env(), capture_output=True, timeout=30
    )
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stderr.decode('utf-8', 'replace')}")
    return cp.stdout.decode("utf-8", "replace")


def _head(repo):
    return _git(["rev-parse", "HEAD"], repo).strip()


def _branch(repo):
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).strip()


def _status(repo):
    return _git(["status", "--porcelain=v1"], repo)


def _init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    return root


@pytest.fixture
def repo(tmp_path):
    root = _init_repo(tmp_path / "repo")
    (root / "foo.py").write_text("A\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "initial"], root)
    return root


def test_clean_repo_shadow_matches_head_and_source_untouched(repo, tmp_path):
    before_head, before_branch, before_status = _head(repo), _branch(repo), _status(repo)
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-clean", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.read_text("foo.py") == "A\n"
        assert ws.snapshot.snapshot_commit == ws.snapshot.source_head == before_head
        # shadow'da yapılan değişiklik kaynağa sızmaz
        ws.write_text("foo.py", "C\n")
        assert (repo / "foo.py").read_text(encoding="utf-8") == "A\n"
        assert _head(repo) == before_head
        assert _branch(repo) == before_branch
        assert _status(repo) == before_status
    finally:
        ws.dispose()


def test_dirty_tracked_file_shadow_sees_working_copy(repo, tmp_path):
    (repo / "foo.py").write_text("B\n", encoding="utf-8")
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-dirty", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.read_text("foo.py") == "B\n"
    finally:
        ws.dispose()


def test_staged_and_unstaged_resolve_to_working_content(repo, tmp_path):
    (repo / "foo.py").write_text("B\n", encoding="utf-8")
    _git(["add", "foo.py"], repo)
    (repo / "foo.py").write_text("B2\n", encoding="utf-8")
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-staged", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.read_text("foo.py") == "B2\n"
    finally:
        ws.dispose()


def test_tracked_deletion_reflected_in_initial_shadow(repo, tmp_path):
    (repo / "foo.py").unlink()
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-deleted", base_dir=tmp_path / "workspaces"
    )
    try:
        assert not ws.exists("foo.py")
    finally:
        ws.dispose()


def test_untracked_copied_ignored_excluded(repo, tmp_path):
    (repo / "new_file.txt").write_text("hello\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret\n", encoding="utf-8")
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-untracked", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.read_text("new_file.txt") == "hello\n"
        assert not ws.exists("ignored.txt")
        assert "new_file.txt" in ws.snapshot.untracked_paths
        assert "ignored.txt" not in ws.snapshot.untracked_paths
    finally:
        ws.dispose()


def test_synthetic_snapshot_baseline_reflects_pre_agent_state(repo, tmp_path):
    """Kritik B->C anlamı: baseline HEAD(A) değil, ajan öncesi çalışma kopyası (B) olmalı.

    Yalnızca snapshot_commit'teki blob içeriğini (B) doğrulamakla kalmaz;
    ajanın shadow'da yaptığı C değişikliğinden SONRA snapshot_commit'e karşı
    gerçek bir `git diff` alıp silinen/eklenen satırların tam olarak B->C
    olduğunu (A->C DEĞİL) doğrular.
    """
    head_a = _head(repo)
    (repo / "foo.py").write_text("B\n", encoding="utf-8")
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-baseline", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.snapshot.source_head == head_a
        assert ws.snapshot.snapshot_commit != head_a
        baseline_foo = _git(["show", f"{ws.snapshot.snapshot_commit}:foo.py"], repo)
        assert baseline_foo == "B\n"
        head_a_foo = _git(["show", f"{head_a}:foo.py"], repo)
        assert head_a_foo == "A\n"

        # Ajan shadow'u B'den C'ye değiştiriyor.
        ws.write_text("foo.py", "C\n")
        assert ws.read_text("foo.py") == "C\n"

        diff_output = _git(
            ["diff", "--no-color", ws.snapshot.snapshot_commit, "--", "foo.py"], ws.root
        )
        removed = [
            line[1:] for line in diff_output.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        added = [
            line[1:] for line in diff_output.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        assert removed == ["B"], "diff'in eski tarafı B olmalı, A değil"
        assert added == ["C"]
    finally:
        ws.dispose()


def test_clean_working_tree_reuses_head_as_snapshot(repo, tmp_path):
    """Çalışma kopyası zaten HEAD ile aynıysa gereksiz sentetik commit oluşmaz."""
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-noop-snapshot", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.snapshot.snapshot_commit == ws.snapshot.source_head
    finally:
        ws.dispose()


def test_nested_project_root_maps_correctly(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    frontend = repo_root / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("console.log('a')\n", encoding="utf-8")
    (repo_root / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "-A"], repo_root)
    _git(["commit", "-q", "-m", "initial"], repo_root)

    ws = GitWorktreeWorkspace.create(
        source_root=frontend, run_id="run-nested", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.root.name == "frontend"
        assert ws.read_text("app.js") == "console.log('a')\n"
        assert not ws.exists("README.md")  # workspace kökü frontend'e sabit
        assert ws.snapshot.project_relative_root == Path("frontend")
        assert ws.snapshot.repository_root == repo_root.resolve()
    finally:
        ws.dispose()


def test_dispose_removes_worktree_and_is_idempotent(repo, tmp_path):
    base = tmp_path / "workspaces"
    ws = GitWorktreeWorkspace.create(source_root=repo, run_id="run-dispose", base_dir=base)
    worktree_dir = base / "run-dispose"
    assert worktree_dir.exists()
    ws.dispose()
    assert not worktree_dir.exists()
    ws.dispose()  # ikinci çağrı zararsız olmalı


def test_dispose_retries_after_failed_cleanup(repo, tmp_path, monkeypatch):
    """İlk dispose() denemesi başarısız olursa obje 'disposed' sayılmamalı;
    ikinci çağrı temizliği yeniden deneyebilmeli (ve başarabilmeli)."""
    import workspace.worktree as wt_mod

    base = tmp_path / "workspaces"
    ws = GitWorktreeWorkspace.create(source_root=repo, run_id="run-retry", base_dir=base)
    worktree_dir = base / "run-retry"

    original_remove = wt_mod._remove_worktree
    calls = {"n": 0}

    def flaky_remove(repo_root, wt_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WorkspaceCleanupError("simulated first-attempt failure")
        return original_remove(repo_root, wt_dir)

    monkeypatch.setattr(wt_mod, "_remove_worktree", flaky_remove)

    with pytest.raises(WorkspaceCleanupError):
        ws.dispose()
    assert calls["n"] == 1
    assert worktree_dir.exists()  # temizlik gerçekleşmedi, workspace hâlâ var

    ws.dispose()  # yeniden deneme başarılı olmalı
    assert calls["n"] == 2
    assert not worktree_dir.exists()

    ws.dispose()  # başarılı temizlikten sonra idempotent
    assert calls["n"] == 2


def test_failed_creation_cleans_up_worktree(repo, tmp_path, monkeypatch):
    import workspace.worktree as wt_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(wt_mod, "_write_synthetic_snapshot", boom)
    base = tmp_path / "workspaces"
    with pytest.raises(WorkspaceCreationError):
        GitWorktreeWorkspace.create(source_root=repo, run_id="run-fail", base_dir=base)
    assert not (base / "run-fail").exists()


def test_unresolved_merge_conflict_rejected(tmp_path):
    root = _init_repo(tmp_path / "repo")
    (root / "foo.py").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "base"], root)
    base_branch = _branch(root)

    _git(["checkout", "-q", "-b", "feature"], root)
    (root / "foo.py").write_text("feature\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "feature change"], root)

    _git(["checkout", "-q", base_branch], root)
    (root / "foo.py").write_text("main change\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "main change"], root)

    _git(["merge", "feature"], root, check=False)  # kasıtlı çakışma, başarısız olması beklenir

    with pytest.raises(UnsupportedRepositoryStateError):
        GitWorktreeWorkspace.create(
            source_root=root, run_id="run-conflict", base_dir=tmp_path / "workspaces"
        )


def test_repository_with_gitlink_rejected(repo, tmp_path):
    """Submodule/gitlink (mode 160000) içeren repolar v1'de desteklenmiyor.

    Gerçek bir submodule klonlamak yerine (ağ erişimi olmadan, deterministik
    biçimde) plumbing ile doğrudan bir gitlink indeks girişi eklenir — bunun
    işaret ettiği obje gerçekten var olmak zorunda değildir, Git bunu commit
    sırasında doğrulamaz.
    """
    fake_submodule_sha = "a" * 40
    _git(
        ["update-index", "--add", "--cacheinfo", f"160000,{fake_submodule_sha},vendor/lib"],
        repo,
    )
    _git(["commit", "-q", "-m", "add gitlink"], repo)
    with pytest.raises(UnsupportedRepositoryStateError):
        GitWorktreeWorkspace.create(
            source_root=repo, run_id="run-gitlink", base_dir=tmp_path / "workspaces"
        )


def test_dirty_source_repo_state_unchanged_after_full_lifecycle(repo, tmp_path):
    """Kaynak repoda staged + unstaged + untracked değişiklikler varken bile
    workspace oluşturma/dispose döngüsü, kullanıcının görünür durumunu
    (dal, HEAD, indeks, çalışma dosyaları, untracked dosyalar) değiştirmez."""
    (repo / "foo.py").write_text("staged content\n", encoding="utf-8")
    _git(["add", "foo.py"], repo)
    (repo / "foo.py").write_text("staged content\nunstaged tail\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")

    before_head = _head(repo)
    before_branch = _branch(repo)
    before_status = _status(repo)
    before_diff = _git(["diff"], repo)
    before_diff_cached = _git(["diff", "--cached"], repo)
    before_foo = (repo / "foo.py").read_text(encoding="utf-8")
    before_scratch = (repo / "scratch.txt").read_text(encoding="utf-8")

    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-dirty-invariance", base_dir=tmp_path / "workspaces"
    )
    ws.dispose()

    assert _head(repo) == before_head
    assert _branch(repo) == before_branch
    assert _status(repo) == before_status
    assert _git(["diff"], repo) == before_diff
    assert _git(["diff", "--cached"], repo) == before_diff_cached
    assert (repo / "foo.py").read_text(encoding="utf-8") == before_foo
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == before_scratch


def test_missing_head_rejected(tmp_path):
    root = _init_repo(tmp_path / "repo-empty")
    with pytest.raises(UnsupportedRepositoryStateError):
        GitWorktreeWorkspace.create(
            source_root=root, run_id="run-nohead", base_dir=tmp_path / "workspaces"
        )


def test_not_a_git_repository_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(UnsupportedRepositoryStateError):
        GitWorktreeWorkspace.create(
            source_root=plain, run_id="run-norepo", base_dir=tmp_path / "workspaces"
        )


def test_run_id_path_traversal_rejected(repo, tmp_path):
    with pytest.raises(WorkspaceCreationError):
        GitWorktreeWorkspace.create(
            source_root=repo, run_id="../escape", base_dir=tmp_path / "workspaces"
        )
    with pytest.raises(WorkspaceCreationError):
        GitWorktreeWorkspace.create(
            source_root=repo, run_id="a/b", base_dir=tmp_path / "workspaces"
        )


def test_paths_with_spaces_and_unicode(repo, tmp_path):
    (repo / "dosya adı ünïcode.txt").write_text("merhaba\n", encoding="utf-8")
    sub = repo / "alt klasör"
    sub.mkdir()
    (sub / "iç dosya.txt").write_text("içerik\n", encoding="utf-8")
    ws = GitWorktreeWorkspace.create(
        source_root=repo, run_id="run-unicode", base_dir=tmp_path / "workspaces"
    )
    try:
        assert ws.read_text("dosya adı ünïcode.txt") == "merhaba\n"
        assert ws.read_text("alt klasör/iç dosya.txt") == "içerik\n"
    finally:
        ws.dispose()


def test_context_manager_disposes_workspace(repo, tmp_path):
    base = tmp_path / "workspaces"
    with GitWorktreeWorkspace.create(source_root=repo, run_id="run-ctx", base_dir=base) as ws:
        assert ws.read_text("foo.py") == "A\n"
    assert not (base / "run-ctx").exists()
