"""executor_runtime.acp_presets -- data-only launch-profile sözleşmesi.

Gerçek `npx`/ağ çağrısı YAPILMAZ: yalnızca üretilen AcpWorkerLaunchProfile'ın
komut/argüman/env sözleşmesi doğrulanır (bkz. acp_presets.py docstring'i)."""

import os

from executor_runtime import (
    claude_code_acp_launch_profile,
    codex_acp_launch_profile,
    gemini_cli_acp_launch_profile,
)
from executor_runtime.acp_presets import (
    CLAUDE_AGENT_ACP_PACKAGE,
    CLAUDE_AGENT_ACP_VERSION,
    CODEX_ACP_PACKAGE,
    CODEX_ACP_VERSION,
    GEMINI_CLI_PACKAGE,
    GEMINI_CLI_VERSION,
)


def test_claude_code_preset_pins_version_and_passes_home_and_path():
    profile = claude_code_acp_launch_profile()
    assert profile.command == "npx"
    assert profile.args == ("-y", f"{CLAUDE_AGENT_ACP_PACKAGE}@{CLAUDE_AGENT_ACP_VERSION}")
    assert "HOME" in profile.env or os.environ.get("HOME") is None
    assert "PATH" in profile.env or os.environ.get("PATH") is None


def test_codex_preset_pins_version():
    profile = codex_acp_launch_profile()
    assert profile.command == "npx"
    assert profile.args == ("-y", f"{CODEX_ACP_PACKAGE}@{CODEX_ACP_VERSION}")


def test_gemini_cli_preset_pins_version_and_passes_acp_flag():
    profile = gemini_cli_acp_launch_profile()
    assert profile.command == "npx"
    assert profile.args == ("-y", f"{GEMINI_CLI_PACKAGE}@{GEMINI_CLI_VERSION}", "--acp")


def test_gemini_cli_preset_passes_base_env(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(var, raising=False)
    profile = gemini_cli_acp_launch_profile()
    assert profile.env == {"HOME": "/home/tester", "PATH": "/usr/bin"}


def test_gemini_cli_preset_passes_through_optional_google_vars_only_if_set(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    profile = gemini_cli_acp_launch_profile()
    assert profile.env == {
        "HOME": "/home/tester",
        "PATH": "/usr/bin",
        "GEMINI_API_KEY": "secret-key",
        "GOOGLE_CLOUD_PROJECT": "my-project",
    }


def test_gemini_cli_preset_never_invents_credentials(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    profile = gemini_cli_acp_launch_profile()
    for key in profile.env:
        assert key in ("HOME", "PATH")
