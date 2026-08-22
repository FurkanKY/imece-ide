"""Read-only ToolRuntime adapters for repository intelligence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from context_runtime import ContextBudget, ContextEngine, render_context_pack
from tool_runtime.models import PermissionRequest, ToolAnnotations, ToolExecutionContext, ToolObservation
from tool_runtime.registry import ToolRegistry, ToolSpec

REPOSITORY_TOOL_DEFAULT_CHARS = 12_000
REPOSITORY_TOOL_MAX_CHARS = 24_000


def _permission(_arguments: Mapping[str, Any], _context: ToolExecutionContext):
    return [PermissionRequest("search", ".")]


def _budget(max_chars: int) -> ContextBudget:
    return ContextBudget(
        total_chars=max_chars,
        map_chars=min(max_chars, max(256, max_chars // 2)),
        max_segment_chars=min(max_chars, 6_000),
    )


def _metadata(pack) -> dict[str, Any]:
    return {
        "query": pack.query,
        "repository_fingerprint": pack.repository_fingerprint,
        "files_considered": pack.diagnostics.files_considered,
        "symbols_found": pack.diagnostics.symbols_found,
        "segments_returned": len(getattr(pack, "segments", ())),
        "used_chars": pack.used_chars,
        "truncated": pack.truncated,
        "skipped_unreadable": pack.diagnostics.skipped_unreadable,
        "skipped_binary": pack.diagnostics.skipped_binary,
        "skipped_oversize": pack.diagnostics.skipped_oversize,
    }


class _RepoMapExecutor:
    def __init__(self, engine: ContextEngine) -> None:
        self._engine = engine

    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        max_chars = arguments.get("max_chars", REPOSITORY_TOOL_DEFAULT_CHARS)
        pack = self._engine.build_map(context.workspace, arguments.get("query", ""), max_chars=max_chars)
        metadata = _metadata(pack)
        metadata["used_chars"] = pack.used_chars
        metadata["truncated"] = pack.truncated
        metadata["segments_returned"] = 0
        return ToolObservation(pack.text, metadata)


class _SearchCodeExecutor:
    def __init__(self, engine: ContextEngine) -> None:
        self._engine = engine

    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        max_chars = arguments.get("max_chars", REPOSITORY_TOOL_DEFAULT_CHARS)
        pack = self._engine.build(context.workspace, arguments["query"], _budget(max_chars))
        return ToolObservation(render_context_pack(pack), _metadata(pack))


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def register_repository_tools(registry: ToolRegistry, *, engine: ContextEngine | None = None) -> None:
    """Register only bounded read-only repository map/search adapters."""
    engine = engine or ContextEngine()
    annotations = ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)
    limit = {"type": "integer", "minimum": 256, "maximum": REPOSITORY_TOOL_MAX_CHARS}
    query = {"type": "string", "maxLength": 4096}
    registry.register(
        ToolSpec(
            "repo_map", "Retrieve a bounded, query-aware repository map.",
            _schema({"query": query, "max_chars": limit}), annotations, _permission,
        ),
        _RepoMapExecutor(engine),
    )
    registry.register(
        ToolSpec(
            "search_code", "Retrieve ranked, bounded repository code excerpts.",
            _schema({"query": {**query, "minLength": 1}, "max_chars": limit}, ["query"]),
            annotations, _permission,
        ),
        _SearchCodeExecutor(engine),
    )
