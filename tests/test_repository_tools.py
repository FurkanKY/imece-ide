import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_runtime import (
    ApprovalGrant, Dispatcher, PermissionEffect, PermissionRule, PolicyEvaluator,
    ToolCall, ToolExecutionContext, ToolRegistry,
)
from tool_runtime.errors import ToolApprovalRequiredError, ToolDeniedError
from tool_runtime.tools.repository import register_repository_tools
from workspace.local import LocalWorkspace
from workspace.base import Workspace


def _setup(tmp_path, effect):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def search_target():\n    return 'needle'\n", encoding="utf-8")
    registry = ToolRegistry()
    register_repository_tools(registry)
    dispatcher = Dispatcher(registry, PolicyEvaluator([PermissionRule("search", ".", effect)]))
    return registry, dispatcher, ToolExecutionContext(LocalWorkspace(tmp_path))


def test_repository_specs_and_allow_execution(tmp_path):
    registry, dispatcher, context = _setup(tmp_path, PermissionEffect.ALLOW)
    specs = registry.list_specs()
    assert [spec.name for spec in specs] == ["repo_map", "search_code"]
    assert all(spec.annotations.read_only and spec.annotations.idempotent for spec in specs)
    prepared = dispatcher.prepare(ToolCall("map", "repo_map", {"query": "search_target", "max_chars": 500}), context)
    assert prepared.permission_requests[0].action == "search"
    assert prepared.permission_requests[0].resource == "."
    mapped = dispatcher.execute(prepared, context)
    assert "src/main.py" in mapped.content
    search = dispatcher.prepare(ToolCall("search", "search_code", {"query": "search_target", "max_chars": 1000}), context)
    result = dispatcher.execute(search, context)
    assert "search_target" in result.content
    assert result.metadata["segments_returned"] >= 1
    assert result.metadata["repository_fingerprint"]


def test_repo_map_has_map_specific_256_char_budget_and_metadata(tmp_path):
    _, dispatcher, context = _setup(tmp_path, PermissionEffect.ALLOW)
    for number in range(20):
        target = context.workspace.root / "many" / f"file_{number}.py"
        target.parent.mkdir(exist_ok=True)
        target.write_text("pass\n", encoding="utf-8")
    small = dispatcher.prepare(ToolCall("small-map", "repo_map", {"max_chars": 256}), context)
    small_result = dispatcher.execute(small, context)
    assert small_result.content
    assert len(small_result.content) <= 256
    assert small_result.metadata["used_chars"] == len(small_result.content)
    assert small_result.metadata["truncated"] is True
    large = dispatcher.prepare(ToolCall("large-map", "repo_map", {"max_chars": 5000}), context)
    large_result = dispatcher.execute(large, context)
    assert large_result.metadata["truncated"] is False
    assert large_result.metadata["used_chars"] == len(large_result.content)


class LogicalMapWorkspace(Workspace):
    def __init__(self):
        self._long = "verylong/" + ("x" * 260) + ".py"

    @property
    def root(self):
        return Path("/")

    def iter_files(self, relative_scope=".", *, excluded_dirs=()):
        yield self._long
        yield "src/a.py"

    def read_text(self, relative_path):
        return "needle\n"

    def dispose(self):
        return None


def test_repo_map_skips_unrenderable_entry_and_keeps_later_path():
    registry = ToolRegistry()
    register_repository_tools(registry)
    context = ToolExecutionContext(LogicalMapWorkspace())
    dispatcher = Dispatcher(registry, PolicyEvaluator([PermissionRule("search", ".", PermissionEffect.ALLOW)]))
    prepared = dispatcher.prepare(ToolCall("oversized-map", "repo_map", {"query": "verylong", "max_chars": 256}), context)
    result = dispatcher.execute(prepared, context)
    assert "src/a.py" in result.content
    assert len(result.content) <= 256
    assert result.metadata["truncated"] is True


def test_repository_tools_ask_and_deny_before_scanning(tmp_path, monkeypatch):
    registry, dispatcher, context = _setup(tmp_path, PermissionEffect.ASK)
    workspace = context.workspace
    calls = 0
    original = workspace.iter_files

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace, "iter_files", counted)
    prepared = dispatcher.prepare(ToolCall("ask", "search_code", {"query": "needle"}), context)
    with pytest.raises(ToolApprovalRequiredError):
        dispatcher.execute(prepared, context)
    assert calls == 0
    result = dispatcher.execute(prepared, context, ApprovalGrant("ask", prepared.approval_fingerprint))
    assert "needle" in result.content and calls == 1

    _, denied_dispatcher, denied_context = _setup(tmp_path / "denied", PermissionEffect.DENY)
    denied_workspace = denied_context.workspace
    monkeypatch.setattr(denied_workspace, "iter_files", lambda *args, **kwargs: pytest.fail("scan must not run"))
    denied = denied_dispatcher.prepare(ToolCall("deny", "repo_map", {}), denied_context)
    with pytest.raises(ToolDeniedError):
        denied_dispatcher.execute(denied, denied_context)
