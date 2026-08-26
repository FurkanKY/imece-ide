"""GitWorktreeChangeProvider — deterministic, read-only, index-safe capture."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from change_runtime import ChangeCaptureError, ChangeInputError, GitWorktreeChangeProvider, WorkspaceChangeSet  # noqa: E402
from workspace.worktree import GitWorktreeWorkspace  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "a.txt").write_text("hello\n", encoding="utf-8")
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)
    return source


@pytest.fixture
def workspace(repo, tmp_path):
    ws = GitWorktreeWorkspace.create(source_root=repo, run_id="change-test", base_dir=tmp_path / "workspaces")
    yield ws
    ws.dispose()


@pytest.fixture
def provider():
    return GitWorktreeChangeProvider()


def test_clean_snapshot_yields_empty_change_set(provider, workspace):
    change = provider.capture(workspace)
    assert change.diff == ""
    assert change.changed_paths == ()
    assert change.diff_sha256 == hashlib.sha256(b"").hexdigest()


def test_tracked_modification_is_captured(provider, workspace):
    (workspace.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    change = provider.capture(workspace)
    assert change.changed_paths == ("a.txt",)
    assert "+world" in change.diff
    assert change.diff_sha256 == hashlib.sha256(change.diff.encode("utf-8")).hexdigest()


def test_staged_modification_is_visible(provider, workspace):
    (workspace.root / "a.txt").write_text("staged content\n", encoding="utf-8")
    _git(["add", "a.txt"], workspace.root)
    change = provider.capture(workspace)
    assert "staged content" in change.diff
    assert change.changed_paths == ("a.txt",)


def test_tracked_deletion_is_captured(provider, workspace):
    (workspace.root / "a.txt").unlink()
    change = provider.capture(workspace)
    assert change.changed_paths == ("a.txt",)
    assert "-hello" in change.diff


def test_new_untracked_utf8_file_is_captured(provider, workspace):
    (workspace.root / "new.txt").write_text("brand new\n", encoding="utf-8")
    change = provider.capture(workspace)
    assert "new.txt" in change.changed_paths
    assert "brand new" in change.diff
    assert "new file mode" in change.diff


def test_ignored_untracked_file_is_excluded(provider, workspace):
    (workspace.root / "ignored.txt").write_text("should not appear\n", encoding="utf-8")
    change = provider.capture(workspace)
    assert change.changed_paths == ()
    assert change.diff == ""


def test_multiple_changed_paths_sorted_deterministically(provider, workspace):
    (workspace.root / "z.txt").write_text("z\n", encoding="utf-8")
    (workspace.root / "b.txt").write_text("b\n", encoding="utf-8")
    (workspace.root / "a.txt").write_text("hello\nmodified\n", encoding="utf-8")
    change = provider.capture(workspace)
    assert change.changed_paths == ("a.txt", "b.txt", "z.txt")
    assert list(change.changed_paths) == sorted(change.changed_paths)


def test_nested_project_root_excludes_repository_siblings(tmp_path):
    repo_root = tmp_path / "monorepo"
    repo_root.mkdir()
    _git(["init", "-q"], repo_root)
    _git(["config", "user.name", "T"], repo_root)
    _git(["config", "user.email", "t@example.com"], repo_root)
    (repo_root / "project").mkdir()
    (repo_root / "project" / "in.txt").write_text("in\n", encoding="utf-8")
    (repo_root / "sibling").mkdir()
    (repo_root / "sibling" / "out.txt").write_text("out\n", encoding="utf-8")
    _git(["add", "-A"], repo_root)
    _git(["commit", "-q", "-m", "init"], repo_root)

    ws = GitWorktreeWorkspace.create(
        source_root=repo_root / "project", run_id="nested-test", base_dir=tmp_path / "workspaces",
    )
    try:
        (ws.root / "in.txt").write_text("in\nchanged\n", encoding="utf-8")
        (Path(ws.root).parent / "sibling" / "out.txt").write_text("out\nchanged\n", encoding="utf-8")
        change = GitWorktreeChangeProvider().capture(ws)
        assert change.changed_paths == ("in.txt",)
        assert "sibling" not in change.diff
    finally:
        ws.dispose()


def test_final_newline_difference_changes_representation_and_sha(provider, workspace):
    (workspace.root / "a.txt").write_text("hello\nworld", encoding="utf-8")  # no trailing newline
    no_newline = provider.capture(workspace)
    assert "\\ No newline at end of file" in no_newline.diff

    (workspace.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    with_newline = provider.capture(workspace)
    assert "\\ No newline at end of file" not in with_newline.diff
    assert no_newline.diff_sha256 != with_newline.diff_sha256


def test_binary_untracked_file_has_deterministic_metadata_not_raw_bytes(provider, workspace):
    data = bytes(range(256))
    (workspace.root / "blob.bin").write_bytes(data)
    change = provider.capture(workspace)
    assert "blob.bin" in change.changed_paths
    digest = hashlib.sha256(data).hexdigest()
    assert digest in change.diff
    assert f"bytes={len(data)}" in change.diff
    assert "\x00" not in change.diff  # raw bytes never injected into the textual diff


def test_two_different_binary_contents_have_different_diff_sha(provider, workspace):
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02binary-one")
    change_one = provider.capture(workspace)

    (workspace.root / "blob.bin").unlink()
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02binary-two-different")
    change_two = provider.capture(workspace)

    assert change_one.diff_sha256 != change_two.diff_sha256


@pytest.mark.skipif(os.name == "nt", reason="symlinks require elevated privileges on Windows")
def test_untracked_symlink_target_represented_without_dereferencing(provider, workspace):
    (workspace.root / "link.txt").symlink_to("some/target/outside.txt")
    change = provider.capture(workspace)
    assert "link.txt" in change.changed_paths
    assert "symlink target: some/target/outside.txt" in change.diff
    assert "new file mode 120000" in change.diff


def test_capture_does_not_alter_git_status_index_or_head(provider, workspace, repo):
    (workspace.root / "a.txt").write_text("hello\nmutated\n", encoding="utf-8")
    (workspace.root / "untracked.txt").write_text("u\n", encoding="utf-8")

    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout

    provider.capture(workspace)
    provider.capture(workspace)  # capture twice — must be side-effect free

    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout
    assert status_after == status_before
    assert head_after == head_before

    source_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
    ).stdout
    assert source_status == ""


def test_repeated_unchanged_capture_is_identical(provider, workspace):
    (workspace.root / "a.txt").write_text("hello\nstable\n", encoding="utf-8")
    first = provider.capture(workspace)
    second = provider.capture(workspace)
    assert first.diff == second.diff
    assert first.diff_sha256 == second.diff_sha256
    assert first.changed_paths == second.changed_paths


def test_change_then_revert_to_snapshot_returns_empty_change_set(provider, workspace):
    original = (workspace.root / "a.txt").read_text(encoding="utf-8")
    (workspace.root / "a.txt").write_text("temporary\n", encoding="utf-8")
    assert provider.capture(workspace).diff != ""
    (workspace.root / "a.txt").write_text(original, encoding="utf-8")
    reverted = provider.capture(workspace)
    assert reverted.diff == ""
    assert reverted.changed_paths == ()


def test_capture_rejects_non_git_worktree_workspace(provider, tmp_path):
    from workspace.local import LocalWorkspace

    with pytest.raises(ChangeInputError):
        provider.capture(LocalWorkspace(tmp_path))


def test_workspace_change_set_diff_sha256_is_not_a_constructor_parameter():
    with pytest.raises(TypeError):
        WorkspaceChangeSet(diff="", changed_paths=(), diff_sha256="a" * 64)  # type: ignore[call-arg]


def test_no_shell_true_and_no_working_tree_diff_or_checkout_family_command_used():
    import inspect

    from change_runtime import git as change_git_module

    source = inspect.getsource(change_git_module)
    assert "shell=True" not in source
    assert "subprocess.run([\"git\"" in source or 'subprocess.run(\n' in source
    assert "--no-optional-locks" in source
    assert '"-c", "core.fsmonitor="' in source
    # the module must never invoke a Git command that compares against the
    # live working tree (that is what feeds current file bytes through
    # repository-configured clean/process filters) or the checkout/stash
    # family (which can trigger hooks/filters too).
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            elts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if elts and elts[0] == "git":
                assert "diff" not in elts, f"working-tree-comparing git diff invoked: {elts}"
                assert "checkout" not in elts
                assert "stash" not in elts
                assert "add" not in elts
                assert "apply" not in elts


# ==================== 2/2A/2B: adversarial filter/hook/fsmonitor/mode/index tests ====================


def test_clean_filter_is_never_executed_during_capture(tmp_path):
    """2.A: a repository-configured filter.<driver>.clean must never run.

    The filter/attributes are registered AFTER GitWorktreeWorkspace.create()
    returns (that constructor is out of this milestone's scope and itself
    runs a working-tree `git diff` during creation — configuring the filter
    only afterward guarantees the marker can only fire from capture())."""
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="filter-test", base_dir=tmp_path / "workspaces")
    try:
        marker = tmp_path / "clean-filter-ran.marker"
        (ws.root / ".gitattributes").write_text("tracked.txt filter=evil\n", encoding="utf-8")
        script = ws.root / "evil-clean.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        script.chmod(0o755)
        _git(["config", "filter.evil.clean", str(script) + " %f"], ws.root)
        _git(["config", "filter.evil.required", "true"], ws.root)

        (ws.root / "tracked.txt").write_text("modified\n", encoding="utf-8")
        change = GitWorktreeChangeProvider().capture(ws)
        assert not marker.exists(), "filter.evil.clean executed during capture()"
        assert "modified" in change.diff
    finally:
        ws.dispose()


def test_process_filter_is_never_executed_during_capture(tmp_path):
    """2.B: a repository-configured filter.<driver>.process must never run.

    Registered after workspace creation (see test_clean_filter_is_never_
    executed_during_capture) so this intentionally-non-protocol-conformant
    script is only ever at risk of being spawned by capture() itself —
    which never runs `git add`/`git diff` against the working tree, so it
    can never trigger the process-filter protocol handshake at all."""
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="process-filter-test", base_dir=tmp_path / "workspaces")
    try:
        marker = tmp_path / "process-filter-ran.marker"
        (ws.root / ".gitattributes").write_text("tracked.txt filter=evilproc\n", encoding="utf-8")
        script = ws.root / "evil-process.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        script.chmod(0o755)
        _git(["config", "filter.evilproc.process", str(script)], ws.root)
        _git(["config", "filter.evilproc.required", "true"], ws.root)

        (ws.root / "tracked.txt").write_text("modified\n", encoding="utf-8")
        GitWorktreeChangeProvider().capture(ws)
        assert not marker.exists(), "filter.evilproc.process executed during capture()"
    finally:
        ws.dispose()


def test_core_fsmonitor_hook_is_never_executed_during_capture(tmp_path):
    """2.C: a configured core.fsmonitor helper must never run.

    Registered after workspace creation, for the same isolation reason as
    the filter tests above."""
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="fsmonitor-test", base_dir=tmp_path / "workspaces")
    try:
        marker = tmp_path / "fsmonitor-ran.marker"
        script = ws.root / "evil-fsmonitor.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
        script.chmod(0o755)
        _git(["config", "core.fsmonitor", str(script)], ws.root)

        (ws.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
        (ws.root / "new.txt").write_text("new\n", encoding="utf-8")
        GitWorktreeChangeProvider().capture(ws)
        assert not marker.exists(), "core.fsmonitor executed during capture()"
    finally:
        ws.dispose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit only")
def test_untracked_executable_mode_is_preserved_and_changes_the_sha(provider, workspace):
    script = workspace.root / "new-script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o644)
    change_a = provider.capture(workspace)
    assert "new file mode 100644" in change_a.diff

    script.chmod(0o755)
    change_b = provider.capture(workspace)
    assert "new file mode 100755" in change_b.diff
    assert change_a.diff_sha256 != change_b.diff_sha256


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit only")
def test_tracked_executable_mode_change_is_captured(provider, workspace):
    (workspace.root / "a.txt").chmod(0o755)
    change = provider.capture(workspace)
    assert "old mode 100644" in change.diff
    assert "new mode 100755" in change.diff


@pytest.fixture
def sha256_repo(tmp_path):
    source = tmp_path / "sha256-repo"
    source.mkdir()
    result = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256"], cwd=source, capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("bu git sürümü --object-format=sha256 desteklemiyor")
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)
    return source


@pytest.fixture
def sha256_workspace(sha256_repo, tmp_path):
    ws = GitWorktreeWorkspace.create(source_root=sha256_repo, run_id="sha256-test", base_dir=tmp_path / "sha256-workspaces")
    yield ws
    ws.dispose()


# ==================== 1: object-format-neutral blob identity ====================


def test_sha256_repo_clean_snapshot_yields_empty_change_set(provider, sha256_workspace):
    """1.A: a real `git init --object-format=sha256` repository, untouched,
    must yield an empty change set — the fast blob-oid comparison must use
    the repository's ACTUAL storage hash algorithm, not a hardcoded SHA-1."""
    change = provider.capture(sha256_workspace)
    assert change.diff == ""
    assert change.changed_paths == ()
    assert change.diff_sha256 == hashlib.sha256(b"").hexdigest()


def test_sha256_repo_tracked_modification_is_captured(provider, sha256_workspace):
    """1.B."""
    (sha256_workspace.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    change = provider.capture(sha256_workspace)
    assert change.changed_paths == ("a.txt",)
    assert "+world" in change.diff
    second = provider.capture(sha256_workspace)
    assert second.diff == change.diff
    assert second.diff_sha256 == change.diff_sha256


def test_sha1_repo_clean_and_modified_behavior_unchanged(provider, workspace):
    """1.C: ordinary SHA-1 repositories (the default fixture) are unaffected."""
    clean = provider.capture(workspace)
    assert clean.diff == ""
    (workspace.root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    modified = provider.capture(workspace)
    assert modified.changed_paths == ("a.txt",)
    assert "+world" in modified.diff


def test_git_blob_oid_helper_matches_real_git_hash_object(repo):
    """1.D: the object-format-aware blob oid helper must match Git's own
    `hash-object` output for known content, not just a self-computed
    expectation."""
    from change_runtime.git import _git_blob_oid, _repo_object_format

    data = b"hello world content for hash-object comparison\n"
    real = subprocess.run(
        ["git", "hash-object", "--stdin"], cwd=repo, input=data, capture_output=True, check=True,
    ).stdout.decode().strip()
    object_format = _repo_object_format(repo)
    assert object_format == "sha1"
    assert _git_blob_oid(data, object_format) == real


@pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")
def test_git_blob_oid_helper_matches_real_git_hash_object_sha256(sha256_repo):
    from change_runtime.git import _git_blob_oid, _repo_object_format

    data = b"hello world content for hash-object comparison\n"
    real = subprocess.run(
        ["git", "hash-object", "--stdin"], cwd=sha256_repo, input=data, capture_output=True, check=True,
    ).stdout.decode().strip()
    object_format = _repo_object_format(sha256_repo)
    assert object_format == "sha256"
    assert len(real) == 64
    assert _git_blob_oid(data, object_format) == real


def test_unknown_object_format_fails_closed():
    from change_runtime.errors import ChangeCaptureError
    from change_runtime.git import _git_blob_oid

    with pytest.raises(ChangeCaptureError):
        _git_blob_oid(b"data", "sha3-nonexistent")


def test_defensive_equality_seam_ignores_a_manufactured_fast_path_mismatch(provider, workspace, repo, monkeypatch):
    """1: even if the cheap oid fast-path incorrectly reports a mismatch,
    capture() must not render a diff for content that is actually
    byte-identical — the fast path is only ever an optimization, never the
    sole equality authority."""
    import change_runtime.git as change_git_module

    monkeypatch.setattr(change_git_module, "_git_blob_oid", lambda data, fmt: "deliberately-wrong-oid")
    change = provider.capture(workspace)  # a.txt is untouched relative to baseline
    assert change.diff == ""
    assert change.changed_paths == ()


# ==================== 2: parent-symlink containment ====================


def test_ancestor_symlink_directory_never_dereferenced_root_level(tmp_path):
    """2: a single-component ancestor symlink (`dir -> outside`) must never
    be traversed to read the tracked descendant's current content."""
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "dir").mkdir()
    (source / "dir" / "file.txt").write_text("baseline\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("OUTSIDE_SECRET_UNIQUE_MARKER\n", encoding="utf-8")

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="symlink-root-test", base_dir=tmp_path / "workspaces")
    try:
        shutil.rmtree(ws.root / "dir")
        (ws.root / "dir").symlink_to(outside)
        try:
            change = GitWorktreeChangeProvider().capture(ws)
        except ChangeCaptureError as exc:
            assert "OUTSIDE_SECRET_UNIQUE_MARKER" not in str(exc)
        else:
            assert "OUTSIDE_SECRET_UNIQUE_MARKER" not in change.diff
    finally:
        ws.dispose()


def test_ancestor_symlink_directory_never_dereferenced_nested(tmp_path):
    """2: nested ancestor symlink (`a/b -> outside`, tracked path `a/b/file.txt`)
    — protection must not be root-level only."""
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "T"], source)
    _git(["config", "user.email", "t@example.com"], source)
    (source / "a" / "b").mkdir(parents=True)
    (source / "a" / "b" / "file.txt").write_text("baseline\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "init"], source)

    outside = tmp_path / "outside-nested"
    outside.mkdir()
    (outside / "file.txt").write_text("OUTSIDE_SECRET_UNIQUE_MARKER_NESTED\n", encoding="utf-8")

    ws = GitWorktreeWorkspace.create(source_root=source, run_id="symlink-nested-test", base_dir=tmp_path / "workspaces")
    try:
        shutil.rmtree(ws.root / "a" / "b")
        (ws.root / "a" / "b").symlink_to(outside)
        try:
            change = GitWorktreeChangeProvider().capture(ws)
        except ChangeCaptureError as exc:
            assert "OUTSIDE_SECRET_UNIQUE_MARKER_NESTED" not in str(exc)
        else:
            assert "OUTSIDE_SECRET_UNIQUE_MARKER_NESTED" not in change.diff
    finally:
        ws.dispose()


@pytest.mark.skipif(os.name == "nt", reason="symlinks require elevated privileges on Windows")
def test_leaf_symlink_still_represented_without_dereference_after_boundary_fix(provider, workspace):
    """Regression guard: the ancestor-symlink fix must not break the
    existing (still-required) leaf-symlink behavior — a symlink AT the leaf
    itself is still fine to represent (target text only, never opened)."""
    (workspace.root / "link.txt").symlink_to("some/target/outside.txt")
    change = provider.capture(workspace)
    assert "link.txt" in change.changed_paths
    assert "symlink target: some/target/outside.txt" in change.diff
    assert "new file mode 120000" in change.diff


def test_capture_does_not_alter_the_actual_index_file_bytes(provider, workspace, repo):
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=workspace.root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    index_path = Path(git_dir)
    if not index_path.is_absolute():
        index_path = (workspace.root / index_path).resolve()
    index_path = index_path / "index"
    assert index_path.is_file(), f"expected an index file at {index_path}"

    before_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
    before_stat = index_path.stat()

    (workspace.root / "a.txt").write_text("hello\nmutated\n", encoding="utf-8")
    (workspace.root / "untracked.txt").write_text("u\n", encoding="utf-8")
    provider.capture(workspace)
    provider.capture(workspace)

    after_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
    after_stat = index_path.stat()
    assert after_sha == before_sha, "index file bytes changed during capture()"
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace.root, capture_output=True, text=True,
    ).stdout
    assert "a.txt" in status_after and "untracked.txt" in status_after
    assert head_after.strip()
