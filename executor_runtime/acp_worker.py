"""ACP Worker launch profile and synchronous attempt adapter."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from acp_runtime.errors import AcpInputError
from acp_runtime.models import AcpClientLimits, AcpLaunchSpec, AcpPromptRequest

from executor_runtime.errors import ExecutorAdapterExecutionError, ExecutorAdapterInputError
from fix_runtime.errors import FixLoopInputError
from fix_runtime.models import FixWorkerRequest
from fix_runtime.ports import WorkerAttemptResult
from run_runtime.acp import CanonicalAcpEventSink
from run_runtime.service import RunRuntime
from workspace.worktree import GitWorktreeWorkspace


def _input_error(message: str, exc: BaseException | None = None) -> ExecutorAdapterInputError:
    error = ExecutorAdapterInputError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


SAFE_REDACTED_DIAGNOSTIC_MESSAGE = (
    "ACP Worker execution failed; diagnostic redacted because it contained "
    "sensitive input or launch data."
)


@dataclass(frozen=True, slots=True)
class AcpWorkerLaunchProfile:
    """Immutable command, arguments, and exact child environment for ACP."""

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command or "\x00" in self.command:
            raise ExecutorAdapterInputError(
                "AcpWorkerLaunchProfile.command must be a non-empty NUL-free string."
            )

        if isinstance(self.args, (str, bytes)):
            raise ExecutorAdapterInputError(
                "AcpWorkerLaunchProfile.args must be a sequence of strings."
            )
        try:
            args = tuple(self.args)
        except TypeError as exc:
            raise _input_error(
                "AcpWorkerLaunchProfile.args must be a sequence of strings.", exc
            ) from exc
        for argument in args:
            if not isinstance(argument, str) or not argument or "\x00" in argument:
                raise ExecutorAdapterInputError(
                    "Every AcpWorkerLaunchProfile argument must be a non-empty NUL-free string."
                )

        if not isinstance(self.env, Mapping):
            raise ExecutorAdapterInputError(
                "AcpWorkerLaunchProfile.env must be a mapping of strings."
            )
        copied_env: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ExecutorAdapterInputError(
                    "AcpWorkerLaunchProfile.env keys and values must be strings."
                )
            if not key or "\x00" in key or "=" in key:
                raise ExecutorAdapterInputError(
                    "AcpWorkerLaunchProfile.env keys must be non-empty, NUL-free, "
                    "and must not contain '=' -- subprocess creation deterministically "
                    "rejects such names."
                )
            if "\x00" in value:
                raise ExecutorAdapterInputError(
                    "AcpWorkerLaunchProfile.env values must be NUL-free strings."
                )

            copied_env[key] = value

        object.__setattr__(self, "args", args)
        object.__setattr__(self, "env", MappingProxyType(copied_env))


def _validated_executable(path: object, *, command: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or not os.path.isabs(path)
        or not os.path.isfile(path)
        or not os.access(path, os.X_OK)
    ):
        raise ExecutorAdapterInputError(
            f"ACP Worker executable could not be resolved as an absolute executable: {command!r}."
        )
    return path


def resolve_acp_worker_launch(profile: AcpWorkerLaunchProfile) -> AcpLaunchSpec:
    """Resolve one profile into the exact immutable ACP launch specification."""

    if not isinstance(profile, AcpWorkerLaunchProfile):
        raise ExecutorAdapterInputError(
            "resolve_acp_worker_launch requires an AcpWorkerLaunchProfile."
        )

    if os.path.isabs(profile.command):
        executable = _validated_executable(profile.command, command=profile.command)
    else:
        discovered = shutil.which(profile.command)
        executable = _validated_executable(discovered, command=profile.command)

    try:
        return AcpLaunchSpec(argv=(executable, *profile.args), env=profile.env)
    except AcpInputError as exc:
        raise _input_error("Invalid ACP Worker launch profile.", exc) from exc


class _AcpClientRunner(Protocol):
    async def run(
        self,
        launch: AcpLaunchSpec,
        request: AcpPromptRequest,
        *,
        limits: AcpClientLimits | None = None,
        event_sink=None,
    ):
        ...


class AcpWorkerAttemptAdapter:
    """Run one fresh ACP Worker attempt through the synchronous worker port."""

    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        launch_profile: AcpWorkerLaunchProfile,
        acp_client: _AcpClientRunner,
        *,
        limits: AcpClientLimits | None = None,
    ) -> None:
        if not isinstance(runtime, RunRuntime):
            raise ExecutorAdapterInputError("AcpWorkerAttemptAdapter.runtime must be a RunRuntime.")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ExecutorAdapterInputError("AcpWorkerAttemptAdapter.run_id must be non-empty.")
        if not isinstance(launch_profile, AcpWorkerLaunchProfile):
            raise ExecutorAdapterInputError(
                "AcpWorkerAttemptAdapter.launch_profile must be an AcpWorkerLaunchProfile."
            )
        if not callable(getattr(acp_client, "run", None)):
            raise ExecutorAdapterInputError(
                "AcpWorkerAttemptAdapter.acp_client must expose a callable run()."
            )
        if limits is None:
            self._limits = AcpClientLimits()
        elif not isinstance(limits, AcpClientLimits):
            raise ExecutorAdapterInputError(
                "AcpWorkerAttemptAdapter.limits must be an AcpClientLimits."
            )
        else:
            self._limits = limits
        self._runtime = runtime
        self._run_id = run_id
        self._launch_profile = launch_profile
        self._acp_client = acp_client

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(
        self, workspace, request: FixWorkerRequest, *, execution_id: str
    ) -> WorkerAttemptResult:
        if not isinstance(request, FixWorkerRequest):
            raise ExecutorAdapterInputError(
                "AcpWorkerAttemptAdapter.run requires a FixWorkerRequest."
            )
        if not isinstance(workspace, GitWorktreeWorkspace):
            raise ExecutorAdapterInputError(
                "The ACP Worker may only operate on a GitWorktreeWorkspace."
            )
        try:
            expected_result = WorkerAttemptResult(execution_id=execution_id)
        except FixLoopInputError as exc:
            raise ExecutorAdapterInputError(f"Invalid execution_id: {exc}") from exc

        launch_spec = resolve_acp_worker_launch(self._launch_profile)
        try:
            cwd = str(workspace.root)
            if not os.path.isabs(cwd) or not os.path.isdir(cwd):
                raise ExecutorAdapterInputError(
                    f"ACP Worker cwd must be an existing absolute directory: {cwd!r}."
                )
            prompt_request = AcpPromptRequest(
                cwd=cwd,
                prompt=request.rendered_input,
            )
        except ExecutorAdapterInputError:
            raise
        except (AcpInputError, OSError, TypeError, ValueError) as exc:
            raise ExecutorAdapterInputError(f"Invalid ACP Worker prompt/cwd: {exc}") from exc

        if len(prompt_request.prompt) > self._limits.max_prompt_chars:
            raise ExecutorAdapterInputError(
                "ACP Worker prompt exceeds max_prompt_chars "
                f"({len(prompt_request.prompt)} > {self._limits.max_prompt_chars})."
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ExecutorAdapterExecutionError(
                "AcpWorkerAttemptAdapter.run cannot execute inside a running event loop."
            )

        try:
            sink = CanonicalAcpEventSink(self._runtime, self._run_id, execution_id=execution_id)
        except ValueError as exc:
            raise ExecutorAdapterInputError(
                f"Cannot construct canonical ACP sink: {exc}"
            ) from exc
        sink.start(request.task)
        try:
            acp_result = asyncio.run(
                self._acp_client.run(
                    launch_spec,
                    prompt_request,
                    limits=self._limits,
                    event_sink=sink,
                )
            )
            sink.complete(acp_result)
        except Exception as original_failure:
            if sink.persistence_error is not None:
                # Canonical persistence has already been proven
                # conflicted/unavailable by this exact failure (or an
                # earlier one during this same attempt): the sink's
                # expected sequence is known stale, so a second append
                # (sink.fail()) must never be attempted. Canonical
                # integrity/storage failure has higher precedence than the
                # ACP transport/protocol failure that triggered it.
                raise ExecutorAdapterExecutionError(
                    "ACP Worker canonical persistence is unavailable or sequence-conflicted; "
                    "not attempting execution.failed."
                ) from sink.persistence_error
            try:
                sink.fail(
                    original_failure,
                    error_type=type(original_failure).__name__,
                    message=self._failure_message(
                        original_failure,
                        prompt=prompt_request.prompt,
                        launch=launch_spec,
                    ),
                )
            except Exception as terminal_failure:
                raise ExecutorAdapterExecutionError(
                    "ACP Worker execution and terminal failure recording both failed."
                ) from terminal_failure
            raise ExecutorAdapterExecutionError("ACP Worker execution failed.") from original_failure
        return expected_result

    @staticmethod
    def _failure_message(error: Exception, *, prompt: str, launch: AcpLaunchSpec) -> str:
        """Fail-closed diagnostic redaction.

        Sequential str.replace() of individual secrets is not overlap-safe:
        replacing a shorter literal first (e.g. an env key that is a prefix
        of its own value) can leave a fragment of a longer secret behind,
        and there is no reordering that is safe for every possible overlap
        between the prompt, environment keys/values, and launch arguments.
        Instead: if the raw message contains ANY sensitive literal at all,
        discard the entire human-readable diagnostic and substitute one
        fixed safe message. Only when no sensitive literal is present is
        the raw (NUL-stripped) diagnostic retained.
        """
        raw_message = str(error).replace("\x00", "")

        sensitive_literals: list[str] = []
        if prompt:
            sensitive_literals.append(prompt)
        for key, value in launch.env.items():
            if key:
                sensitive_literals.append(key)
            if value:
                sensitive_literals.append(value)
        for argument in launch.argv:
            if argument:
                sensitive_literals.append(argument)

        if any(literal in raw_message for literal in sensitive_literals):
            return SAFE_REDACTED_DIAGNOSTIC_MESSAGE
        return raw_message
