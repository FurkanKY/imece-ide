"""AcpLaunchSpec / AcpPromptRequest / AcpClientLimits / AcpRunResult."""

import os
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp_runtime.errors import AcpInputError  # noqa: E402
from acp_runtime.models import (  # noqa: E402
    AcpClientLimits,
    AcpLaunchSpec,
    AcpPromptRequest,
    AcpRunResult,
)


# ---------------- AcpLaunchSpec ----------------


def test_empty_argv_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=())


def test_empty_argv_member_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent", ""))


def test_relative_argv0_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("agent",))


def test_nul_in_argv_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent", "x\x00y"))


def test_invalid_env_key_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent",), env={1: "v"})


def test_invalid_env_value_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent",), env={"K": 1})


def test_nul_in_env_key_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent",), env={"K\x00": "v"})


def test_nul_in_env_value_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=("/usr/bin/agent",), env={"K": "v\x00"})


def test_env_not_visible_in_repr():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",), env={"SECRET": "shh"})
    assert "shh" not in repr(spec)
    assert "SECRET" not in repr(spec)


def test_caller_env_mutation_after_construction_does_not_mutate_spec():
    env = {"K": "v1"}
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",), env=env)
    env["K"] = "v2"
    env["NEW"] = "x"
    assert dict(spec.env) == {"K": "v1"}


def test_env_is_read_only_mapping_proxy():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",), env={"K": "v1"})
    assert isinstance(spec.env, MappingProxyType)


def test_mutation_through_spec_env_raises():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",), env={"A": "1"})
    with pytest.raises(TypeError):
        spec.env["A"] = "2"
    assert dict(spec.env) == {"A": "1"}


def test_dict_of_spec_env_still_works_for_subprocess_launch():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",), env={"A": "1", "B": "2"})
    materialized = dict(spec.env)
    assert materialized == {"A": "1", "B": "2"}
    materialized["A"] = "mutated-copy-only"
    assert dict(spec.env) == {"A": "1", "B": "2"}


@pytest.mark.skipif(os.name != "nt", reason="Windows-only drive-relative path semantics")
def test_windows_drive_relative_argv0_rejected():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=(r"C:foo\agent.exe",))


@pytest.mark.skipif(os.name != "nt", reason="Windows-only absolute path semantics")
def test_windows_drive_rooted_argv0_accepted():
    spec = AcpLaunchSpec(argv=(r"C:\Program Files\agent.exe",))
    assert spec.argv == (r"C:\Program Files\agent.exe",)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only UNC path semantics")
def test_windows_unc_argv0_accepted():
    spec = AcpLaunchSpec(argv=(r"\\server\share\agent.exe",))
    assert spec.argv == (r"\\server\share\agent.exe",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only relative-with-colon acceptance-as-non-absolute check")
def test_posix_windows_style_path_not_treated_as_absolute():
    with pytest.raises(AcpInputError):
        AcpLaunchSpec(argv=(r"C:foo",))


def test_valid_launch_spec_accepted():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent", "--flag"), env={"K": "v"})
    assert spec.argv == ("/usr/bin/agent", "--flag")
    assert dict(spec.env) == {"K": "v"}


def test_default_env_is_empty():
    spec = AcpLaunchSpec(argv=("/usr/bin/agent",))
    assert dict(spec.env) == {}


# ---------------- AcpPromptRequest ----------------


def test_relative_cwd_rejected():
    with pytest.raises(AcpInputError):
        AcpPromptRequest(cwd="relative/path", prompt="do the thing")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only drive-relative path semantics")
def test_windows_drive_relative_cwd_rejected():
    with pytest.raises(AcpInputError):
        AcpPromptRequest(cwd=r"C:relative\path", prompt="task")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only absolute path semantics")
def test_windows_drive_rooted_cwd_accepted():
    request = AcpPromptRequest(cwd=r"C:\Users\someone", prompt="task")
    assert request.cwd == r"C:\Users\someone"


def test_empty_prompt_rejected():
    with pytest.raises(AcpInputError):
        AcpPromptRequest(cwd="/tmp", prompt="")


def test_whitespace_only_prompt_rejected():
    with pytest.raises(AcpInputError):
        AcpPromptRequest(cwd="/tmp", prompt="   \n\t  ")


def test_nul_in_cwd_rejected():
    with pytest.raises(AcpInputError):
        AcpPromptRequest(cwd="/tmp/x\x00y", prompt="task")


def test_prompt_not_visible_in_repr():
    request = AcpPromptRequest(cwd="/tmp", prompt="super secret task body")
    assert "super secret task body" not in repr(request)


def test_valid_prompt_request_accepted():
    request = AcpPromptRequest(cwd="/tmp", prompt="do the thing")
    assert request.cwd == "/tmp"
    assert request.prompt == "do the thing"


# ---------------- AcpClientLimits ----------------


def test_default_limits_are_positive_ints():
    limits = AcpClientLimits()
    for value in (
        limits.max_prompt_chars,
        limits.max_updates,
        limits.max_update_chars,
        limits.max_total_update_chars,
        limits.prompt_timeout_ms,
        limits.cancel_grace_ms,
        limits.session_close_timeout_ms,
    ):
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value > 0


def test_bool_limit_rejected():
    with pytest.raises(AcpInputError):
        AcpClientLimits(max_prompt_chars=True)


def test_zero_limit_rejected():
    with pytest.raises(AcpInputError):
        AcpClientLimits(max_updates=0)


def test_negative_limit_rejected():
    with pytest.raises(AcpInputError):
        AcpClientLimits(prompt_timeout_ms=-1)


def test_non_int_limit_rejected():
    with pytest.raises(AcpInputError):
        AcpClientLimits(cancel_grace_ms="2000")


# ---------------- AcpRunResult ----------------


def test_run_result_field_presence():
    result = AcpRunResult(
        session_id="sess-1",
        stop_reason="end_turn",
        update_count=3,
        update_chars=120,
        permission_request_count=0,
        session_close_supported=True,
        session_close_succeeded=True,
    )
    assert result.session_id == "sess-1"
    assert result.stop_reason == "end_turn"
    assert result.update_count == 3
    assert result.update_chars == 120
    assert result.permission_request_count == 0
    assert result.session_close_supported is True
    assert result.session_close_succeeded is True


def test_run_result_is_frozen():
    result = AcpRunResult(
        session_id="sess-1", stop_reason="end_turn", update_count=0, update_chars=0,
        permission_request_count=0, session_close_supported=False, session_close_succeeded=None,
    )
    with pytest.raises(Exception):
        result.session_id = "other"
