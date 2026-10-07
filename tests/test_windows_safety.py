"""Portable contract tests for the Windows no-reparse tree walker."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import workspace.windows_safety as safety


class FakeTree:
    REPARSE = 0x400
    DIRECTORY = 0x10
    READONLY = 0x1

    def __init__(self, nodes):
        self.nodes = nodes
        self.closed = []
        self.opened_directories = []

    def open(self, path, *, directory):
        rel = "/".join(path.parts[-2:]) if path.parent.name != "root" else path.name
        item = self.nodes.get(rel, self.nodes.get(path.name))
        # Portable tests model the outside-root ancestors as stable directories;
        # the native Windows test exercises their real handle identities.
        if item is None and directory and path.name not in {"safe.txt", "escape.txt"}:
            item = {"directory": True}
        if item is None or item.get("reparse", False):
            raise OSError("missing/reparse")
        is_dir = item["directory"]
        if is_dir != directory:
            raise OSError("kind changed")
        info = SimpleNamespace(attributes=(self.DIRECTORY if is_dir else 0)
                               | (self.READONLY if item.get("readonly") else 0))
        if is_dir:
            self.opened_directories.append(path)
        return path, info, (1, hash(rel))

    def close(self, handle):
        self.closed.append(handle)

    @staticmethod
    def mode(info):
        return 0o444 if info.attributes & 1 else 0o666

    def read(self, handle, limit):
        rel = "/".join(handle.parts[-2:]) if handle.parent.name != "root" else handle.name
        return self.nodes[rel]["data"][:limit]


def test_windows_walker_contract_hashes_regular_files_and_refuses_reparse(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "safe.txt").write_text("safe")
    (root / "escape.txt").write_text("must-not-read")
    tree = FakeTree({"root": {"directory": True}, "safe.txt": {"data": b"safe", "directory": False},
                     "escape.txt": {"data": b"secret", "directory": False, "reparse": True}})
    monkeypatch.setattr(safety, "Win32FileTree", lambda: tree)
    records, paths, complete = safety._walk(root, (), max_depth=8, entry_budget=100,
                                           max_file=1024, max_total=1024)
    assert paths == ("safe.txt",)
    assert not complete
    assert any(b'"reason":"reparse-or-unreadable"' in row for row in records)
    assert tree.closed
    # The absolute chain through the workspace root is pinned for the walk.
    assert root.absolute() in tree.opened_directories
    assert all(parent in tree.opened_directories for parent in root.absolute().parents)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Exercises native Win32 handle/reparse APIs")
def test_native_windows_handle_walker_and_junction_fail_closed(tmp_path):
    import subprocess
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir(); external.mkdir()
    (root / "safe.txt").write_bytes(b"safe")
    (external / "secret.txt").write_bytes(b"secret")
    junction = root / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                            capture_output=True, text=True)
    if result.returncode:
        pytest.skip("Windows runner does not permit junction creation")
    _records, paths, complete = safety._walk(root, (), max_depth=8, entry_budget=100,
        max_file=1024, max_total=1024)
    assert not complete
    assert paths == ("safe.txt",)
    assert not safety.validate_directory_chain(junction)
    assert safety.validate_directory_chain(root)
    from agent_execution_runtime.execution import _workspace_fingerprint, _workspace_inventory
    from workspace.local import LocalWorkspace
    files, inventory_complete = _workspace_inventory(LocalWorkspace(root))
    digest, fingerprint_complete = _workspace_fingerprint(LocalWorkspace(root), files)
    assert files == ("safe.txt",) and inventory_complete is False
    assert digest and fingerprint_complete is False


def test_windows_walker_enforces_budgets_and_original_cache_inclusion(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "__pycache__").mkdir(parents=True)
    (root / "__pycache__" / "original.pyc").write_bytes(b"original")
    tree = FakeTree({"root": {"directory": True}, "__pycache__": {"directory": True},
                     "original.pyc": {"data": b"original", "directory": False},
                     "__pycache__/original.pyc": {"data": b"original", "directory": False}})
    monkeypatch.setattr(safety, "Win32FileTree", lambda: tree)
    records, paths, complete = safety._walk(root, ("__pycache__/original.pyc",),
        max_depth=8, entry_budget=100, max_file=1024, max_total=1024,
        ignore_dirs=frozenset({"__pycache__"}))
    assert complete and paths == ("__pycache__/original.pyc",)
    assert any(b'"path":"__pycache__/original.pyc"' in row for row in records)
