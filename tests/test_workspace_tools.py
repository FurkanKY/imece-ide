import os
import shutil
import subprocess
import sys
import threading
from itertools import count
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_runtime import (
    Dispatcher,
    PermissionEffect,
    PermissionRule,
    PolicyEvaluator,
    ToolCall,
    ToolExecutionContext,
    ToolRegistry,
)
from tool_runtime.errors import (
    ToolDeniedError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolPolicyError,
)
from tool_runtime.tools.workspace_files import (
    LIST_MAX_LIMIT,
    MAX_WRITE_CHARS,
    READ_MAX_LINES,
    READ_MAX_OUTPUT_CHARS,
    SEARCH_MAX_OUTPUT_CHARS,
    register_workspace_read_tools,
    register_workspace_tools,
)
from workspace.local import LocalWorkspace
from workspace.worktree import GitWorktreeWorkspace


_CALL_IDS = count(1)


class CountingWorkspace(LocalWorkspace):
    def __init__(self, root):
        super().__init__(root)
        self.read_paths = []

    def read_text(self, relative_path):
        self.read_paths.append(relative_path)
        return super().read_text(relative_path)


def _dispatcher(ws, rules=None):
    registry = ToolRegistry()
    register_workspace_tools(registry)
    policy = PolicyEvaluator(rules or [PermissionRule("*", "*", PermissionEffect.ALLOW)])
    return Dispatcher(registry, policy), ToolExecutionContext(workspace=ws)


def _run(dispatcher, context, tool_name, arguments, call_id=None):
    call_id = call_id or f"call-{next(_CALL_IDS)}-{tool_name}"
    prepared = dispatcher.prepare(ToolCall(call_id, tool_name, arguments), context)
    return dispatcher.execute(prepared, context)


@pytest.fixture
def ws(tmp_path):
    return LocalWorkspace(tmp_path)


def test_path_normalization_and_rejection(ws):
    (ws.root / "src").mkdir()
    (ws.root / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "read_file", {"path": r"src\main.py"})
    assert observation.metadata["path"] == "src/main.py"
    bad_paths = (
        "../x", "a/../b", "/etc/passwd", "C:", "C:foo", "C:foo/bar",
        "D:dir/file.txt", r"C:\Users\x", "C:/Users/x", r"\\server\share",
        "~/x", "$HOME/x", "a\x00b",
    )
    for bad in bad_paths:
        with pytest.raises(ToolPolicyError):
            dispatcher.prepare(ToolCall(f"bad-{repr(bad)}", "read_file", {"path": bad}), context)
    with pytest.raises(ToolPolicyError):
        dispatcher.prepare(ToolCall("root-delete", "delete_path", {"path": "."}), context)


def test_registration_exposes_five_tools_and_declares_permissions():
    registry = ToolRegistry()
    register_workspace_tools(registry)
    specs = {spec.name: spec for spec in registry.list_specs()}
    assert set(specs) == {"read_file", "list_files", "search_text", "write_file", "delete_path"}
    assert specs["read_file"].annotations.read_only is True
    assert specs["write_file"].annotations.destructive is True
    assert specs["delete_path"].annotations.idempotent is False


def test_read_file_window_line_numbers_and_metadata(ws):
    (ws.root / "file.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "read_file", {"path": "file.txt", "offset": 2, "limit": 2})
    assert observation.content == "2: two\n3: three"
    assert observation.metadata == {
        "path": "file.txt", "start_line": 2, "end_line": 3,
        "truncated": True, "next_line": 4,
    }
    empty = _run(dispatcher, context, "read_file", {"path": "file.txt", "offset": 99})
    assert empty.content == ""
    assert empty.metadata["end_line"] is None
    assert empty.metadata["next_line"] is None


def test_read_file_caps_lines_long_lines_and_global_output(ws):
    (ws.root / "long.txt").write_text("x" * 5000 + "\n", encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    long_observation = _run(dispatcher, context, "read_file", {"path": "long.txt"})
    assert len(long_observation.content.split(": ", 1)[1]) == 4000
    assert long_observation.metadata["truncated"] is True
    with pytest.raises(ToolInputValidationError):
        dispatcher.prepare(ToolCall("too-many", "read_file", {"path": "long.txt", "limit": READ_MAX_LINES + 1}), context)
    (ws.root / "many.txt").write_text(("y" * 3900 + "\n") * 500, encoding="utf-8")
    capped = _run(dispatcher, context, "read_file", {"path": "many.txt", "limit": READ_MAX_LINES})
    assert len(capped.content) <= READ_MAX_OUTPUT_CHARS
    assert capped.metadata["truncated"] is True


def test_read_file_non_utf8_is_execution_failure(ws):
    (ws.root / "binary.bin").write_bytes(b"\xff\xfe\x00")
    dispatcher, context = _dispatcher(ws)
    with pytest.raises(ToolExecutionError) as error:
        _run(dispatcher, context, "read_file", {"path": "binary.bin"})
    assert isinstance(error.value.__cause__, UnicodeDecodeError)


def test_nul_containing_utf8_file_is_binary_for_read_and_search(ws):
    (ws.root / "nul.bin").write_bytes(b"prefix\x00needle\n")
    dispatcher, context = _dispatcher(ws)
    with pytest.raises(ToolExecutionError):
        _run(dispatcher, context, "read_file", {"path": "nul.bin"})
    observation = _run(dispatcher, context, "search_text", {"query": "needle"})
    assert observation.content == ""
    assert observation.metadata["skipped_unreadable"] == 1


def test_search_output_cap_stops_later_file_reads(tmp_path):
    (tmp_path / "a.txt").write_text(("needle " + "x" * 990 + "\n") * 200, encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle later\n", encoding="utf-8")
    ws = CountingWorkspace(tmp_path)
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "search_text", {"query": "needle", "limit": 200})
    assert observation.metadata["truncated"] is True
    assert observation.metadata["files_searched"] == 1
    assert ws.read_paths == ["a.txt"]


def test_list_files_is_recursive_deterministic_and_excludes_heavy_dirs(ws):
    for path in ("z.txt", "a.txt", "src/main.py", ".github/workflow.yml", "node_modules/pkg.js", ".venv/hidden.py", "build/out.js"):
        target = ws.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "list_files", {})
    assert observation.content.splitlines() == [".github/workflow.yml", "a.txt", "src/main.py", "z.txt"]
    assert observation.metadata == {"path": ".", "count": 4, "truncated": False}
    scoped = _run(dispatcher, context, "list_files", {"path": "src"})
    assert scoped.content == "src/main.py"


def test_list_files_limit_and_scope_normalization(ws):
    for name in ("c.txt", "a.txt", "b.txt"):
        (ws.root / name).write_text(name, encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "list_files", {"limit": 2})
    assert observation.content.splitlines() == ["a.txt", "b.txt"]
    assert observation.metadata["count"] == 2
    assert observation.metadata["truncated"] is True
    with pytest.raises(ToolInputValidationError):
        dispatcher.prepare(ToolCall("list-cap", "list_files", {"limit": LIST_MAX_LIMIT + 1}), context)


def test_search_literal_case_modes_ordering_and_line_numbers(ws):
    (ws.root / "z.txt").write_text("Abc\nliteral a.*b\n", encoding="utf-8")
    (ws.root / "a.txt").write_text("abc here\n", encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    literal = _run(dispatcher, context, "search_text", {"query": "a.*b"})
    assert literal.content == "z.txt:2: literal a.*b"
    insensitive = _run(dispatcher, context, "search_text", {"query": "abc", "case_sensitive": False})
    assert insensitive.content.splitlines() == ["a.txt:1: abc here", "z.txt:1: Abc"]
    assert insensitive.metadata["count"] == 2


def test_search_caps_preview_output_and_skips_binary(ws):
    (ws.root / "binary.bin").write_bytes(b"\xff\xfe\x00")
    (ws.root / "long.txt").write_text("needle-" + "x" * 2000 + "\n", encoding="utf-8")
    (ws.root / "many.txt").write_text(("needle " + "x" * 990 + "\n") * 200, encoding="utf-8")
    dispatcher, context = _dispatcher(ws)
    observation = _run(dispatcher, context, "search_text", {"query": "needle", "limit": 200})
    assert len(observation.content) <= SEARCH_MAX_OUTPUT_CHARS
    assert observation.metadata["truncated"] is True
    assert observation.metadata["skipped_unreadable"] == 1
    assert all(len(line.split(": ", 1)[1]) <= 1000 for line in observation.content.splitlines())


def test_write_create_replace_exact_content_and_delete(ws):
    dispatcher, context = _dispatcher(ws)
    created = _run(dispatcher, context, "write_file", {"path": r"nested\new.txt", "content": "exact"})
    assert created.metadata == {"path": "nested/new.txt", "created": True, "char_count": 5}
    assert (ws.root / "nested" / "new.txt").read_text(encoding="utf-8") == "exact"
    replaced = _run(dispatcher, context, "write_file", {"path": "nested/new.txt", "content": "changed"})
    assert replaced.metadata["created"] is False
    assert (ws.root / "nested" / "new.txt").read_text(encoding="utf-8") == "changed"
    _run(dispatcher, context, "delete_path", {"path": "nested/new.txt"})
    assert not (ws.root / "nested" / "new.txt").exists()
    with pytest.raises(ToolExecutionError):
        _run(dispatcher, context, "delete_path", {"path": "nested/new.txt"})
    with pytest.raises(ToolInputValidationError):
        dispatcher.prepare(ToolCall("write-cap", "write_file", {"path": "x", "content": "x" * (MAX_WRITE_CHARS + 1)}), context)


def test_symlink_policy_is_shared_and_delete_unlinks_only_link(ws, tmp_path):
    outside = tmp_path.parent / f"external-{tmp_path.name}"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        file_link = ws.root / "link.txt"
        dir_link = ws.root / "linked-dir"
        os.symlink(outside / "secret.txt", file_link)
        os.symlink(outside, dir_link)
    except (OSError, NotImplementedError):
        pytest.skip("Bu platformda sembolik bağ oluşturulamıyor.")
    dispatcher, context = _dispatcher(ws)
    with pytest.raises(ToolExecutionError):
        _run(dispatcher, context, "read_file", {"path": "link.txt"})
    with pytest.raises(ToolExecutionError):
        _run(dispatcher, context, "write_file", {"path": "link.txt", "content": "changed"})
    listed = _run(dispatcher, context, "list_files", {})
    searched = _run(dispatcher, context, "search_text", {"query": "secret"})
    assert "link.txt" not in listed.content and "secret.txt" not in listed.content
    assert searched.content == ""
    _run(dispatcher, context, "delete_path", {"path": "link.txt"})
    assert not file_link.exists() and not file_link.is_symlink()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_delete_directory_symlink_unlinks_only_link(ws, tmp_path):
    outside = tmp_path.parent / f"external-dir-{tmp_path.name}"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = ws.root / "external-dir"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("Bu platformda sembolik bağ oluşturulamıyor.")
    dispatcher, context = _dispatcher(ws)
    _run(dispatcher, context, "delete_path", {"path": "external-dir"})
    assert not link.exists() and not link.is_symlink()
    assert (outside / "keep.txt").exists()


def test_reviewer_policy_can_inspect_but_not_mutate(ws):
    (ws.root / "file.txt").write_text("before", encoding="utf-8")
    rules = [
        PermissionRule("read", "*", PermissionEffect.ALLOW),
        PermissionRule("list", "*", PermissionEffect.ALLOW),
        PermissionRule("search", "*", PermissionEffect.ALLOW),
        PermissionRule("edit", "*", PermissionEffect.DENY),
        PermissionRule("delete", "*", PermissionEffect.DENY),
    ]
    dispatcher, context = _dispatcher(ws, rules)
    assert "before" in _run(dispatcher, context, "read_file", {"path": "file.txt"}).content
    _run(dispatcher, context, "list_files", {})
    _run(dispatcher, context, "search_text", {"query": "before"})
    with pytest.raises(ToolDeniedError):
        _run(dispatcher, context, "write_file", {"path": "file.txt", "content": "after"})
    with pytest.raises(ToolDeniedError):
        _run(dispatcher, context, "delete_path", {"path": "file.txt"})
    assert (ws.root / "file.txt").read_text(encoding="utf-8") == "before"


def test_register_workspace_read_tools_exposes_exactly_the_read_only_surface():
    registry = ToolRegistry()
    register_workspace_read_tools(registry)
    names = {spec.name for spec in registry.list_specs()}
    assert names == {"read_file", "list_files", "search_text"}
    for spec in registry.list_specs():
        assert spec.annotations.read_only is True
        assert spec.annotations.destructive is False


def test_register_workspace_tools_still_registers_full_surface_in_same_order():
    registry = ToolRegistry()
    register_workspace_tools(registry)
    names = [spec.name for spec in registry.list_specs()]
    assert names == ["read_file", "list_files", "search_text", "write_file", "delete_path"]


def test_register_workspace_read_tools_schemas_match_full_registration(tmp_path):
    full = ToolRegistry()
    register_workspace_tools(full)
    read_only = ToolRegistry()
    register_workspace_read_tools(read_only)
    for name in ("read_file", "list_files", "search_text"):
        assert full.get(name).input_schema == read_only.get(name).input_schema
        assert full.get(name).description == read_only.get(name).description


def test_read_only_tools_execute_identically_through_read_only_registry(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    ws = LocalWorkspace(tmp_path)
    registry = ToolRegistry()
    register_workspace_read_tools(registry)
    policy = PolicyEvaluator([PermissionRule("*", "*", PermissionEffect.ALLOW)])
    dispatcher = Dispatcher(registry, policy)
    context = ToolExecutionContext(workspace=ws)
    observation = _run(dispatcher, context, "read_file", {"path": "a.txt"})
    assert "hello" in observation.content


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git bulunamadı")
def test_git_worktree_tools_do_not_mutate_source_before_apply(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    _git(["init", "-q"], source)
    _git(["config", "user.name", "Test"], source)
    _git(["config", "user.email", "test@example.com"], source)
    (source / "file.txt").write_text("source", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "initial"], source)
    workspace = GitWorktreeWorkspace.create(
        source_root=source, run_id="tool-test", base_dir=tmp_path / "workspaces"
    )
    try:
        dispatcher, context = _dispatcher(workspace)
        _run(dispatcher, context, "write_file", {"path": "file.txt", "content": "shadow"})
        assert (source / "file.txt").read_text(encoding="utf-8") == "source"
        assert workspace.read_text("file.txt") == "shadow"
        _run(dispatcher, context, "delete_path", {"path": "file.txt"})
        assert (source / "file.txt").read_text(encoding="utf-8") == "source"
    finally:
        workspace.dispose()
