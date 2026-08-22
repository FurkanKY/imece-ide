"""Controlled provider-independent run_process Agent tool."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from process_runtime import ProcessRequest, ProcessRunner
from tool_runtime.models import PermissionRequest, ToolAnnotations, ToolExecutionContext, ToolObservation
from tool_runtime.registry import ToolRegistry, ToolSpec


RUN_PROCESS_SCHEMA = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string", "maxLength": 16384},
            "minItems": 1,
            "maxItems": 128,
        },
        "cwd": {"type": "string", "minLength": 1},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["argv"],
    "additionalProperties": False,
}


def _request(arguments: Mapping[str, Any]) -> ProcessRequest:
    return ProcessRequest(
        argv=tuple(arguments["argv"]),
        cwd=arguments.get("cwd", "."),
        timeout_ms=arguments.get("timeout_ms", 120000),
        env=arguments.get("env", {}),
    )


def _permission(arguments: Mapping[str, Any], _context: ToolExecutionContext):
    return [PermissionRequest("process.execute", _request(arguments).permission_resource())]


class RunProcessExecutor:
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolObservation:
        request = _request(arguments)
        result = self._runner.run(context.workspace, request)
        if result.timed_out:
            headline = f"Command timed out after {request.timeout_ms} ms."
        else:
            headline = f"Command exited with code {result.exit_code}."
        content = (
            f"{headline}\n\n"
            f"stdout:\n{result.stdout}\n\n"
            f"stderr:\n{result.stderr}"
        )
        metadata = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "cwd": result.cwd,
            "argv": list(result.argv),
            "execution_isolation": "host",
        }
        return ToolObservation(content, metadata)


def register_process_tool(registry: ToolRegistry, runner: ProcessRunner | None = None) -> None:
    registry.register(
        ToolSpec(
            name="run_process",
            description="Run one non-interactive argv process in the workspace cwd.",
            input_schema=RUN_PROCESS_SCHEMA,
            annotations=ToolAnnotations(
                read_only=False,
                destructive=True,
                idempotent=False,
                open_world=True,
            ),
            permission_resolver=_permission,
        ),
        RunProcessExecutor(runner),
    )
