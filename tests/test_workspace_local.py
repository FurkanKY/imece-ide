"""LocalWorkspace ve yol güvenliği (resolve_within_workspace) sözleşmesi."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace.errors import WorkspaceBoundaryError, WorkspaceCreationError  # noqa: E402
from workspace.local import LocalWorkspace  # noqa: E402


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret\n", encoding="utf-8")
    # İç içe/gömülü .git bileşenleri (kökte değil) de korunmalı.
    (tmp_path / "foo" / ".git").mkdir(parents=True)
    (tmp_path / "foo" / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (tmp_path / "packages" / "example" / ".git").mkdir(parents=True)
    (tmp_path / "packages" / "example" / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return LocalWorkspace(tmp_path)


def test_missing_root_raises():
    with pytest.raises(WorkspaceCreationError):
        LocalWorkspace("/definitely/does/not/exist/xyz-imece")


def test_read_write_normal_files(ws):
    assert ws.read_text("sub/a.txt") == "hello\n"
    ws.write_text("new/dir/b.txt", "content\n")
    assert ws.read_text("new/dir/b.txt") == "content\n"
    assert ws.exists("new/dir/b.txt")
    assert not ws.exists("does/not/exist.txt")


def test_dot_dot_escape_rejected(ws):
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("../outside.txt")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("sub/../../outside.txt")


def test_absolute_posix_path_escape_rejected(ws, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text(str(outside))


def test_windows_style_absolute_path_rejected(ws):
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text(r"C:\Windows\system32\drivers\etc\hosts")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text(r"\\server\share\file.txt")


def test_git_directory_access_rejected(ws):
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text(".git/config")
    with pytest.raises(WorkspaceBoundaryError):
        ws.write_text(".git/config", "pwned")
    with pytest.raises(WorkspaceBoundaryError):
        ws.delete_path(".git/config")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("sub/../.git/config")


def test_nested_git_directory_access_rejected(ws):
    """.git yalnızca yol kökündeyken değil, herhangi bir bileşen konumundayken de reddedilir."""
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("foo/.git/config")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("packages/example/.git/HEAD")
    with pytest.raises(WorkspaceBoundaryError):
        ws.write_text("packages/example/.git/HEAD", "pwned")
    with pytest.raises(WorkspaceBoundaryError):
        ws.delete_path("foo/.git/config")
    assert ws.exists("foo/.git/config") is False
    assert ws.exists("packages/example/.git/HEAD") is False


def test_git_directory_case_insensitive_rejected(ws):
    """Windows'ta dosya sistemleri genelde büyük/küçük harf duyarsızdır;
    .GIT / .Git / .gIt gibi varyantlar da (kökte veya iç içe) engellenmelidir."""
    for variant in (".GIT", ".Git", ".gIt"):
        with pytest.raises(WorkspaceBoundaryError):
            ws.read_text(f"{variant}/config")
        with pytest.raises(WorkspaceBoundaryError):
            ws.write_text(f"{variant}/config", "pwned")
        with pytest.raises(WorkspaceBoundaryError):
            ws.delete_path(f"{variant}/config")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("foo/.gIt/HEAD")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("packages/example/.GIT/config")


def test_symlink_resolving_into_git_directory_rejected(ws):
    """Lexical yol '.git' içermese bile, sembolik bağ çözümü .git'e yönleniyorsa reddedilir."""
    link = ws.root / "innocuous"
    try:
        os.symlink(ws.root / ".git", link)
    except (OSError, NotImplementedError):
        pytest.skip("Bu platformda sembolik bağ oluşturulamıyor.")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("innocuous/config")


def test_exists_returns_false_for_boundary_violation(ws):
    assert ws.exists("../outside.txt") is False
    assert ws.exists(".git/config") is False


def test_empty_relative_path_rejected(ws):
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("")


def test_symlink_escape_rejected(ws, tmp_path):
    outside_dir = tmp_path.parent / "imece-outside-target"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("top secret\n", encoding="utf-8")
    link = ws.root / "escape_link"
    try:
        os.symlink(outside_dir, link)
    except (OSError, NotImplementedError):
        pytest.skip("Bu platformda sembolik bağ oluşturulamıyor.")
    with pytest.raises(WorkspaceBoundaryError):
        ws.read_text("escape_link/secret.txt")


def test_delete_symlink_removes_link_not_target(ws, tmp_path):
    target_dir = tmp_path.parent / "imece-keep-me"
    target_dir.mkdir()
    (target_dir / "keep.txt").write_text("keep\n", encoding="utf-8")
    link = ws.root / "link_to_keep"
    try:
        os.symlink(target_dir, link)
    except (OSError, NotImplementedError):
        pytest.skip("Bu platformda sembolik bağ oluşturulamıyor.")
    ws.delete_path("link_to_keep")
    assert not link.exists()
    assert not link.is_symlink()
    assert (target_dir / "keep.txt").exists()  # hedef dokunulmadı


def test_delete_dot_dot_component_rejected(ws, tmp_path_factory):
    """delete_path, '..' bileşeni üzerinden workspace kökünü/üst dizinini hedefleyemez.

    Regresyon: resolve_final=False yolunda son bileşen kasıtlı olarak
    çözülmediği için, lexical bir '..' üst dizin sınırını atlatıp kök
    dışında bir shutil.rmtree()'ye yol açabiliyordu.
    """
    sentinel_dir = tmp_path_factory.mktemp("dotdot-sentinel")
    marker = sentinel_dir / "marker.txt"
    marker.write_text("do not delete\n", encoding="utf-8")

    for bad in ("..", "sub/..", "sub/../..", "sub/../../etc"):
        with pytest.raises(WorkspaceBoundaryError):
            ws.delete_path(bad)
        with pytest.raises(WorkspaceBoundaryError):
            ws.read_text(bad)
        with pytest.raises(WorkspaceBoundaryError):
            ws.write_text(bad, "pwned")

    # Workspace kökü, içeriği ve tümüyle alakasız harici bir sentinel bozulmadı.
    assert ws.root.exists()
    assert ws.read_text("sub/a.txt") == "hello\n"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "do not delete\n"


def test_delete_normal_file_and_directory(ws):
    ws.delete_path("sub/a.txt")
    assert not ws.exists("sub/a.txt")
    ws.write_text("dir/x.txt", "x\n")
    ws.delete_path("dir")
    assert not ws.exists("dir/x.txt")


def test_delete_missing_path_raises(ws):
    with pytest.raises(FileNotFoundError):
        ws.delete_path("does/not/exist.txt")


def test_dispose_is_noop(ws):
    ws.dispose()
    assert ws.exists("sub/a.txt")


def test_context_manager_calls_dispose(tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    with LocalWorkspace(tmp_path) as w:
        assert w.read_text("f.txt") == "x\n"
