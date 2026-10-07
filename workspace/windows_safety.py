"""Fail-closed Win32 no-reparse handles for durable workspace fingerprints.

Every traversed directory and opened file denies FILE_SHARE_DELETE and remains
open while its descendants are inspected. This pins path components against
rename/replacement during a snapshot. Reparse points and non-regular entries
are rejected. The implementation uses only the Windows API and stdlib.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
GENERIC_READ = 0x80000000
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_READONLY = 0x1
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("creation", _FILETIME),
                ("access", _FILETIME), ("write", _FILETIME),
                ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
                ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
                ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32)]


class Win32FileTree:
    """Handle-anchored snapshot walker. Construct only on Windows."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Win32 handle walker is available only on Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_void_p]
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        self.kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
        self.kernel.GetFileInformationByHandle.restype = ctypes.c_int
        self.kernel.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.kernel.ReadFile.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int

    def open(self, path: Path, *, directory: bool):
        flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if directory else 0)
        access = (FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES) if directory else GENERIC_READ
        # Deny concurrent write/delete sharing. If a caller cannot obtain a
        # stable read-only snapshot, the fingerprint fails closed.
        handle = self.kernel.CreateFileW(str(path), access, FILE_SHARE_READ,
                                         None, OPEN_EXISTING, flags, None)
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _BY_HANDLE_FILE_INFORMATION()
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.kernel.CloseHandle(handle)
            raise error
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            self.kernel.CloseHandle(handle)
            raise OSError("Reparse point refused")
        if bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
            self.kernel.CloseHandle(handle)
            raise OSError("File type changed during inspection")
        identity = (info.volume, (info.index_high << 32) | info.index_low)
        return handle, info, identity

    def open_regular_file(self, path: Path):
        handle, info, identity = self.open(path, directory=False)
        return handle, identity

    def close(self, handle):
        if handle:
            self.kernel.CloseHandle(handle)

    def read(self, handle, limit):
        chunks, remaining = [], limit
        while remaining:
            capacity = min(64 * 1024, remaining)
            buf = ctypes.create_string_buffer(capacity)
            count = ctypes.c_uint32()
            if not self.kernel.ReadFile(handle, buf, capacity, ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                break
            chunks.append(buf.raw[:count.value])
            remaining -= count.value
        return b"".join(chunks)

    @staticmethod
    def mode(info):
        return 0o444 if info.attributes & FILE_ATTRIBUTE_READONLY else 0o666


@contextmanager
def pinned_regular_file(path: Path):
    """Hold a non-reparse regular file open without write/delete sharing."""
    tree = Win32FileTree()
    handle, _identity = tree.open_regular_file(path)
    try:
        yield handle
    finally:
        tree.close(handle)


def validate_directory_chain(path: Path) -> bool:
    """Open each absolute directory component without following reparse points.

    Parent handles stay open until the leaf has been inspected, with delete
    sharing denied, so path components cannot be renamed during validation.
    """
    tree = Win32FileTree()
    path = Path(os.path.abspath(path))
    if not path.is_absolute():
        return False
    held = []
    try:
        current = Path(path.anchor)
        handle, _info, _identity = tree.open(current, directory=True)
        held.append(handle)
        for component in path.parts[1:]:
            current = current / component
            handle, _info, _identity = tree.open(current, directory=True)
            held.append(handle)
        return True
    except OSError:
        return False
    finally:
        for handle in reversed(held):
            tree.close(handle)


def _walk(root: Path, originals: tuple[str, ...], *, max_depth: int,
          entry_budget: int, max_file: int, max_total: int, ignore_dirs=frozenset(),
          inventory_only: bool = False):
    """Return canonical records and paths from a bounded, pinned walk."""
    tree = Win32FileTree()
    original_set = frozenset(originals)
    original_dirs = frozenset("/".join(p.split("/")[:i])
                              for p in originals for i in range(1, len(p.split("/"))))
    records, files = [], []
    state = {"entries": 0, "bytes": 0, "complete": True}
    def record(kind, rel, **extra):
        if kind == "marker": state["complete"] = False
        records.append(json.dumps({"kind": kind, "path": rel, **extra}, ensure_ascii=True,
                                  sort_keys=True, separators=(",", ":")).encode("utf-8"))
    def visit(path: Path, prefix: str, depth: int, held: list[int]):
        if depth > max_depth:
            record("marker", prefix or ".", reason="depth-limit"); return
        try:
            names = []
            with os.scandir(path) as scanner:
                for entry in scanner:
                    if entry.name == ".git": continue
                    if len(names) >= entry_budget - state["entries"]:
                        record("marker", prefix or ".", reason="overrun-entries"); return
                    names.append(entry.name)
        except OSError:
            record("marker", prefix or ".", reason="unreadable"); return
        for name in sorted(names):
            if state["entries"] >= entry_budget:
                record("marker", prefix or ".", reason="overrun-entries"); return
            state["entries"] += 1
            rel = f"{prefix}/{name}" if prefix else name
            child = path / name
            if not inventory_only and name.endswith(".pyc") and rel not in original_set:
                continue
            try:
                # Open as an opaque handle first; directory/file kind comes from
                # the handle itself, never from an untrusted path-following stat.
                h, info, _identity = tree.open(child, directory=True)
            except OSError:
                try:
                    h, info, _identity = tree.open(child, directory=False)
                except OSError:
                    record("marker", rel, reason="reparse-or-unreadable"); continue
                try:
                    if inventory_only:
                        files.append(rel)
                        continue
                    data = tree.read(h, max_file + 1)
                    if len(data) > max_file:
                        record("marker", rel, reason="oversize"); continue
                    state["bytes"] += len(data)
                    if state["bytes"] > max_total:
                        record("marker", rel, reason="overrun-bytes"); return
                    files.append(rel)
                    record("file", rel, mode=f"{tree.mode(info):04o}",
                           sha256=hashlib.sha256(data).hexdigest())
                except OSError:
                    record("marker", rel, reason="unreadable")
                finally:
                    tree.close(h)
                continue
            if name.casefold() in ignore_dirs and rel not in original_dirs:
                tree.close(h); continue
            held.append(h)
            try: visit(child, rel, depth + 1, held)
            finally: tree.close(held.pop())
    # Pin every absolute ancestor, not only the worktree root. Otherwise its
    # parent could be renamed/replaced between the ownership check and this
    # path-based scandir/open walk. Delete sharing is denied by Win32FileTree.
    held_ancestors = []
    try:
        absolute = Path(os.path.abspath(root))
        if not absolute.is_absolute():
            raise OSError("Workspace root is not absolute")
        current = Path(absolute.anchor)
        handle, _info, _identity = tree.open(current, directory=True)
        held_ancestors.append(handle)
        for component in absolute.parts[1:]:
            current = current / component
            handle, _info, _identity = tree.open(current, directory=True)
            held_ancestors.append(handle)
    except OSError:
        for handle in reversed(held_ancestors):
            tree.close(handle)
        record("marker", ".", reason="root-unreadable")
    else:
        try: visit(absolute, "", 1, held_ancestors)
        finally:
            for handle in reversed(held_ancestors):
                tree.close(handle)
    return records, tuple(files), state["complete"]


def inventory(root: Path, *, max_depth: int, entry_budget: int,
              max_file: int = 8 * 1024 * 1024, max_total: int = 64 * 1024 * 1024):
    _records, paths, complete = _walk(root, (), max_depth=max_depth,
                                      entry_budget=entry_budget, max_file=max_file,
                                      max_total=max_total, inventory_only=True)
    return paths, complete


def fingerprint_records(root: Path, originals: tuple[str, ...], *, max_depth: int,
                         entry_budget: int, max_file: int, max_total: int,
                         ignore_dirs=frozenset()):
    records, _paths, complete = _walk(root, originals, max_depth=max_depth,
        entry_budget=entry_budget, max_file=max_file, max_total=max_total,
        ignore_dirs=ignore_dirs)
    return hashlib.sha256(b"\n".join(records)).hexdigest(), complete
