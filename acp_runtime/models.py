"""Immutable, provider-neutral models for the ACP Client Core."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from acp_runtime.errors import AcpInputError


def _validate_argv(argv: object) -> tuple[str, ...]:
    if not isinstance(argv, tuple) or len(argv) == 0:
        raise AcpInputError("AcpLaunchSpec.argv must be a non-empty tuple of strings.")
    for member in argv:
        if not isinstance(member, str) or not member:
            raise AcpInputError("Every AcpLaunchSpec.argv member must be a non-empty string.")
        if "\x00" in member:
            raise AcpInputError("AcpLaunchSpec.argv members must not contain NUL characters.")
    if not os.path.isabs(argv[0]):
        raise AcpInputError("AcpLaunchSpec.argv[0] must be an absolute executable path.")
    return argv


def _validate_env(env: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(env, Mapping):
        raise AcpInputError("AcpLaunchSpec.env must be a mapping of str to str.")
    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AcpInputError("AcpLaunchSpec.env keys and values must be strings.")
        if "\x00" in key or "\x00" in value:
            raise AcpInputError("AcpLaunchSpec.env must not contain NUL characters.")
        normalized[key] = value
    # A read-only view over a defensively-copied dict: caller-side mutation
    # of the mapping passed to the constructor cannot affect the spec, and
    # mutation through spec.env itself raises TypeError.
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class AcpLaunchSpec:
    """The complete argv and environment used to spawn one local stdio ACP
    agent subprocess. `env` is the EXACT child environment; it is never
    merged with os.environ, or with any SDK-curated host subset, anywhere in
    acp_runtime (see acp_runtime/stdio.py)."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _validate_argv(self.argv))
        object.__setattr__(self, "env", _validate_env(self.env))


@dataclass(frozen=True, slots=True)
class AcpPromptRequest:
    """One prompt to send to a freshly created ACP session."""

    cwd: str
    prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, str) or not self.cwd:
            raise AcpInputError("AcpPromptRequest.cwd must be a non-empty string.")
        if "\x00" in self.cwd:
            raise AcpInputError("AcpPromptRequest.cwd must not contain NUL characters.")
        if not os.path.isabs(self.cwd):
            raise AcpInputError("AcpPromptRequest.cwd must be an absolute path.")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise AcpInputError("AcpPromptRequest.prompt must be a non-empty, non-whitespace string.")
        if "\x00" in self.prompt:
            raise AcpInputError("AcpPromptRequest.prompt must not contain NUL characters.")


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AcpInputError(f"AcpClientLimits.{field_name} must be an int.")
    if value <= 0:
        raise AcpInputError(f"AcpClientLimits.{field_name} must be > 0.")
    return value


@dataclass(frozen=True, slots=True)
class AcpClientLimits:
    """Bounds enforced by AcpClientRuntime. No zero-means-infinite mode."""

    max_prompt_chars: int = 128_000
    max_updates: int = 2_000
    max_update_chars: int = 65_536
    max_total_update_chars: int = 1_000_000
    prompt_timeout_ms: int = 300_000
    cancel_grace_ms: int = 2_000
    session_close_timeout_ms: int = 2_000

    def __post_init__(self) -> None:
        for name in (
            "max_prompt_chars", "max_updates", "max_update_chars", "max_total_update_chars",
            "prompt_timeout_ms", "cancel_grace_ms", "session_close_timeout_ms",
        ):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class AcpRunResult:
    """Stable execution facts for one AcpClientRuntime.run() invocation.

    Deliberately excludes the prompt text, full transcript, environment, and
    provider metadata."""

    session_id: str
    stop_reason: str
    update_count: int
    update_chars: int
    permission_request_count: int
    session_close_supported: bool
    session_close_succeeded: bool | None
