"""Immutable process request/result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from process_runtime.errors import ProcessInputError
from workspace.base import normalize_workspace_relative_path
from workspace.errors import WorkspaceBoundaryError

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
MAX_ARGV = 128
MAX_ARGUMENT_LENGTH = 16_384
MAX_TOTAL_ARGUMENT_LENGTH = 256 * 1024
MAX_ENV_ENTRIES = 128
MAX_ENV_KEY_LENGTH = 256
MAX_ENV_VALUE_LENGTH = 16_384


def _normalized_cwd(value: str) -> str:
    try:
        return normalize_workspace_relative_path(value, allow_root=True)
    except WorkspaceBoundaryError as exc:
        raise ProcessInputError(str(exc)) from exc


def _validated_argv(value, *, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ProcessInputError(f"{field} must be a sequence of strings")
    try:
        argv = tuple(value)
    except TypeError as exc:
        raise ProcessInputError(f"{field} must be a sequence of strings") from exc
    if not argv:
        raise ProcessInputError(f"{field} must not be empty")
    if len(argv) > MAX_ARGV:
        raise ProcessInputError(f"{field} exceeds {MAX_ARGV} arguments")
    total = 0
    for index, argument in enumerate(argv):
        if not isinstance(argument, str) or "\x00" in argument:
            raise ProcessInputError(f"Every {field} element must be a NUL-free string")
        if index == 0 and not argument:
            raise ProcessInputError(f"{field}[0] must be non-empty")
        if len(argument) > MAX_ARGUMENT_LENGTH:
            raise ProcessInputError(f"A {field} argument is too long")
        total += len(argument)
    if total > MAX_TOTAL_ARGUMENT_LENGTH:
        raise ProcessInputError(f"Total {field} length is too large")
    return argv


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        argv = _validated_argv(self.argv, field="ProcessRequest.argv")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ProcessInputError("ProcessRequest.cwd must be a non-empty string")
        cwd = _normalized_cwd(self.cwd)
        if type(self.timeout_ms) is not int or not (0 < self.timeout_ms <= MAX_TIMEOUT_MS):
            raise ProcessInputError(
                f"timeout_ms must be a positive integer <= {MAX_TIMEOUT_MS}"
            )
        if not isinstance(self.env, Mapping):
            raise ProcessInputError("ProcessRequest.env must be a string mapping")
        if len(self.env) > MAX_ENV_ENTRIES:
            raise ProcessInputError(f"ProcessRequest.env exceeds {MAX_ENV_ENTRIES} entries")
        copied: dict[str, str] = {}
        for key, value in self.env.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or len(key) > MAX_ENV_KEY_LENGTH
            ):
                raise ProcessInputError("Invalid process environment key")
            if not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE_LENGTH:
                raise ProcessInputError("Invalid process environment value")
            if key.casefold() in {"path", "pathext"}:
                raise ProcessInputError("ProcessRequest cannot override PATH or PATHEXT")
            copied[key] = value
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env", MappingProxyType(copied))

    def permission_resource(self) -> str:
        import json

        return json.dumps(
            {
                "argv": list(self.argv),
                "cwd": self.cwd,
                "env": dict(sorted(self.env.items())),
                "timeout_ms": self.timeout_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int

    def __post_init__(self) -> None:
        argv = _validated_argv(self.argv, field="ProcessResult.argv")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ProcessInputError("ProcessResult.cwd must be a non-empty string")
        cwd = _normalized_cwd(self.cwd)
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ProcessInputError("ProcessResult.exit_code must be int or None")
        if type(self.timed_out) is not bool:
            raise ProcessInputError("ProcessResult.timed_out must be bool")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ProcessInputError("ProcessResult.duration_ms must be non-negative int")
        for name in ("stdout", "stderr"):
            if not isinstance(getattr(self, name), str):
                raise ProcessInputError(f"ProcessResult.{name} must be string")
        for name in ("stdout_truncated", "stderr_truncated"):
            if type(getattr(self, name)) is not bool:
                raise ProcessInputError(f"ProcessResult.{name} must be bool")
        for name in ("stdout_bytes", "stderr_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ProcessInputError(f"ProcessResult.{name} must be non-negative int")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
