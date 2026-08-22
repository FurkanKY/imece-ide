"""Workspace-backed file tools for the provider-independent tool runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tool_runtime.errors import ToolInputValidationError
from tool_runtime.models import (
    PermissionRequest,
    ToolAnnotations,
    ToolExecutionContext,
    ToolObservation,
)
from tool_runtime.registry import ToolRegistry, ToolSpec
from workspace.base import normalize_workspace_relative_path
from workspace.errors import WorkspaceBoundaryError, WorkspaceError

READ_DEFAULT_LINES = 300
READ_MAX_LINES = 500
READ_MAX_OUTPUT_CHARS = 64 * 1024
READ_MAX_LINE_CHARS = 4000

LIST_DEFAULT_LIMIT = 200
LIST_MAX_LIMIT = 500

SEARCH_DEFAULT_LIMIT = 100
SEARCH_MAX_LIMIT = 200
SEARCH_MAX_OUTPUT_CHARS = 64 * 1024
SEARCH_MAX_LINE_CHARS = 1000

MAX_WRITE_CHARS = 1_000_000

_EXCLUDED_DIRS = frozenset({
    ".git", ".imece", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
})


class _BinaryContentError(Exception):
    """Decoded content contains the deterministic binary-file marker."""


def normalize_tool_path(raw: str, *, allow_root: bool = False) -> str:
    """Normalize a workspace-relative path without resolving traversal away."""
    try:
        return normalize_workspace_relative_path(raw, allow_root=allow_root)
    except WorkspaceBoundaryError as exc:
        raise ToolInputValidationError(str(exc)) from exc


def _path_argument(arguments: Mapping[str, Any], key: str = "path", *, allow_root: bool = False) -> str:
    return normalize_tool_path(arguments[key], allow_root=allow_root)


def _permission(action: str, path_key: str = "path", *, allow_root: bool = False):
    def resolver(arguments: Mapping[str, Any], _context: ToolExecutionContext):
        raw_path = arguments.get(path_key, ".") if allow_root else arguments[path_key]
        return [PermissionRequest(action, normalize_tool_path(raw_path, allow_root=allow_root))]

    return resolver


def _read_text_content(workspace, path: str) -> str:
    """Read strict UTF-8 text and reject decoded NUL-containing binaries."""
    content = workspace.read_text(path)
    if "\x00" in content:
        raise _BinaryContentError(f"Binary-like NUL content: {path}")
    return content


def _bounded_lines(
    lines: list[str], *, offset: int, limit: int, max_output: int, max_line: int
) -> tuple[str, dict[str, Any]]:
    selected = lines[offset - 1:offset - 1 + limit]
    output_lines: list[str] = []
    truncated = len(lines) > offset - 1 + len(selected)
    for index, raw_line in enumerate(selected, start=offset):
        preview = raw_line[:max_line]
        if len(raw_line) > max_line:
            truncated = True
        rendered = f"{index}: {preview}"
        candidate = rendered if not output_lines else "\n".join((*output_lines, rendered))
        if len(candidate) > max_output:
            truncated = True
            break
        output_lines.append(rendered)
    end_line = offset + len(output_lines) - 1 if output_lines else None
    next_line = end_line + 1 if end_line is not None and end_line < len(lines) else None
    return "\n".join(output_lines), {
        "start_line": offset,
        "end_line": end_line,
        "truncated": truncated,
        "next_line": next_line,
    }


class _ReadFileExecutor:
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        path = _path_argument(arguments)
        offset = arguments.get("offset", 1)
        limit = arguments.get("limit", READ_DEFAULT_LINES)
        content = _read_text_content(context.workspace, path)
        text, metadata = _bounded_lines(
            content.splitlines(),
            offset=offset,
            limit=limit,
            max_output=READ_MAX_OUTPUT_CHARS,
            max_line=READ_MAX_LINE_CHARS,
        )
        return ToolObservation(text, {"path": path, **metadata})


class _ListFilesExecutor:
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        scope = _path_argument(arguments, allow_root=True) if "path" in arguments else "."
        limit = arguments.get("limit", LIST_DEFAULT_LIMIT)
        paths = sorted(
            context.workspace.iter_files(scope, excluded_dirs=_EXCLUDED_DIRS)
        )
        selected = paths[:limit]
        return ToolObservation(
            "\n".join(selected),
            {"path": scope, "count": len(selected), "truncated": len(paths) > limit},
        )


class _SearchTextExecutor:
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        query = arguments["query"]
        scope = _path_argument(arguments, allow_root=True) if "path" in arguments else "."
        case_sensitive = arguments.get("case_sensitive", True)
        limit = arguments.get("limit", SEARCH_DEFAULT_LIMIT)
        needle = query if case_sensitive else query.casefold()
        results: list[str] = []
        skipped_unreadable = 0
        truncated = False
        files_searched = 0
        paths = sorted(context.workspace.iter_files(scope, excluded_dirs=_EXCLUDED_DIRS))
        for path in paths:
            files_searched += 1
            try:
                content = _read_text_content(context.workspace, path)
            except (UnicodeError, _BinaryContentError, OSError, WorkspaceError):
                skipped_unreadable += 1
                continue
            stop_search = False
            for number, raw_line in enumerate(content.splitlines(), start=1):
                haystack = raw_line if case_sensitive else raw_line.casefold()
                if needle not in haystack:
                    continue
                if len(results) >= limit:
                    truncated = True
                    stop_search = True
                    break
                preview = raw_line[:SEARCH_MAX_LINE_CHARS]
                if len(raw_line) > SEARCH_MAX_LINE_CHARS:
                    truncated = True
                rendered = f"{path}:{number}: {preview}"
                candidate = rendered if not results else "\n".join((*results, rendered))
                if len(candidate) > SEARCH_MAX_OUTPUT_CHARS:
                    truncated = True
                    stop_search = True
                    break
                results.append(rendered)
            if stop_search:
                break
        return ToolObservation(
            "\n".join(results),
            {
                "query": query,
                "path": scope,
                "count": len(results),
                "truncated": truncated,
                "skipped_unreadable": skipped_unreadable,
                "files_searched": files_searched,
            },
        )


class _WriteFileExecutor:
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        path = _path_argument(arguments)
        created = not context.workspace.exists(path)
        context.workspace.write_text(path, arguments["content"])
        return ToolObservation(
            f"Wrote {path}",
            {"path": path, "created": created, "char_count": len(arguments["content"])},
        )


class _DeletePathExecutor:
    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        path = _path_argument(arguments)
        context.workspace.delete_path(path)
        return ToolObservation(f"Deleted {path}", {"path": path})


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def register_workspace_tools(registry: ToolRegistry) -> None:
    read_only = ToolAnnotations(read_only=True, idempotent=True)
    destructive = ToolAnnotations(destructive=True, idempotent=True)
    registry.register(
        ToolSpec(
            "read_file",
            "Read a bounded UTF-8 text file window.",
            _schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": READ_MAX_LINES},
                },
                ["path"],
            ),
            read_only,
            _permission("read"),
        ),
        _ReadFileExecutor(),
    )
    registry.register(
        ToolSpec(
            "list_files",
            "List workspace files recursively.",
            _schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": LIST_MAX_LIMIT},
                }
            ),
            read_only,
            _permission("list", allow_root=True),
        ),
        _ListFilesExecutor(),
    )
    registry.register(
        ToolSpec(
            "search_text",
            "Search literal text in workspace files.",
            _schema(
                {
                    "query": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX_LIMIT},
                },
                ["query"],
            ),
            read_only,
            _permission("search", allow_root=True),
        ),
        _SearchTextExecutor(),
    )
    registry.register(
        ToolSpec(
            "write_file",
            "Create or replace a UTF-8 text file.",
            _schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "maxLength": MAX_WRITE_CHARS},
                },
                ["path", "content"],
            ),
            destructive,
            _permission("edit"),
        ),
        _WriteFileExecutor(),
    )
    registry.register(
        ToolSpec(
            "delete_path",
            "Delete a workspace path.",
            _schema({"path": {"type": "string", "minLength": 1}}, ["path"]),
            ToolAnnotations(destructive=True, idempotent=False),
            _permission("delete"),
        ),
        _DeletePathExecutor(),
    )
