"""Provider-neutral ACP launch presets for the account-driven (agent-CLI)
Planner/Worker/Reviewer paths.

Data-only by design: repointing a preset (e.g. bumping a pinned adapter
package version, or adding a new provider) never requires touching
AcpWorkerLaunchProfile, resolve_acp_worker_launch, or any attempt-runner
code -- only this module.

All three presets launch through `npx -y <package>@<pinned version>` rather
than a globally-installed binary: as of 2026-09-26 none of
@agentclientprotocol/claude-agent-acp, @agentclientprotocol/codex-acp or
@google/gemini-cli is installed globally on the target machine, and `npx`
transparently fetches/caches the exact pinned version on first use. `npx`
itself is resolved via PATH by resolve_acp_worker_launch
(AcpWorkerLaunchProfile.command is left relative), exactly like every other
ACP launch profile. Gemini CLI speaks ACP itself (no separate adapter
package) once started with `--acp` (the current, non-deprecated spelling --
it replaces `--experimental-acp`).

Environment: AcpLaunchSpec.env is the EXACT child environment (never merged
with os.environ -- see acp_runtime.models.AcpLaunchSpec / acp_runtime.stdio).
Both adapters need enough of a normal shell environment to (a) resolve their
own dependencies via `npx`/node's module resolution and (b) find the user's
existing CLI login state:

  - HOME: both `claude-agent-acp` (Claude Agent SDK) and `codex-acp` read
    the user's existing CLI login/credentials from files under $HOME
    (~/.claude for Claude Code, ~/.codex for Codex CLI) -- without it
    neither adapter can find the user's account login at all.
  - PATH: needed for `npx` to locate `node`/`npm`'s own resolution machinery
    and any toolchain the agent may shell out to.
  - npm/npx's own cache/config knobs (NPM_CONFIG_CACHE, npm_config_cache,
    ...) are deliberately NOT hardcoded here: when unset, npx/npm fall back
    to their own OS-standard cache location under HOME, which is exactly
    what a preset that must work unmodified on any machine wants.
  - ANTHROPIC_* (e.g. ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/
    ANTHROPIC_BASE_URL) is passed through ONLY if already present in the
    caller's own environment. Aşama 1's ACP path exists specifically to
    drive the user's Claude Code *account* login instead of API billing, so
    this preset never invents or requires an API key -- it only avoids
    silently breaking a caller that already has one set for an unrelated
    reason by refusing to drop it.
  - No CODEX_*/OPENAI_* passthrough is needed on the Codex side for the
    same reason in reverse: codex-acp reads the user's existing
    `codex login` state from ~/.codex, not from an environment variable, so
    there is nothing account-related to pass through.
  - GEMINI_API_KEY/GOOGLE_API_KEY/GOOGLE_CLOUD_PROJECT/
    GOOGLE_GENAI_USE_VERTEXAI are passed through ONLY if already present in
    the caller's own environment, same policy as ANTHROPIC_* above: Gemini
    CLI's default (no key set) is its own OAuth account login (~/.gemini),
    so this preset does not require an API key either -- it only avoids
    dropping one the caller already exported.

Callers that need additional passthrough (a corporate proxy, a custom
$XDG_CONFIG_HOME, CI-only overrides, ...) build their own
AcpWorkerLaunchProfile directly -- these two presets intentionally stay
minimal and easy to read in full.
"""

from __future__ import annotations

import os

from executor_runtime.acp_worker import AcpWorkerLaunchProfile

CLAUDE_AGENT_ACP_PACKAGE = "@agentclientprotocol/claude-agent-acp"
CLAUDE_AGENT_ACP_VERSION = "0.81.2"

CODEX_ACP_PACKAGE = "@agentclientprotocol/codex-acp"
CODEX_ACP_VERSION = "1.13.1"

# Gemini CLI itself speaks ACP (via `--acp`) -- there is no separate
# adapter package like claude-agent-acp/codex-acp. Pinned as of 2026-09-26
# (`npm view @google/gemini-cli version`). `--acp` replaced the deprecated
# `--experimental-acp` flag (both accepted by 0.61.0, but `--acp` is the
# current, non-deprecated spelling -- see `gemini --help`).
GEMINI_CLI_PACKAGE = "@google/gemini-cli"
GEMINI_CLI_VERSION = "0.61.0"

_BASE_PASSTHROUGH_VARS: tuple[str, ...] = ("HOME", "PATH")
_CLAUDE_OPTIONAL_PASSTHROUGH_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
_GEMINI_OPTIONAL_PASSTHROUGH_VARS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_GENAI_USE_VERTEXAI",
)


def _passthrough_env(optional_vars: tuple[str, ...] = ()) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in (*_BASE_PASSTHROUGH_VARS, *optional_vars):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def claude_code_acp_launch_profile() -> AcpWorkerLaunchProfile:
    """`npx -y @agentclientprotocol/claude-agent-acp@<pinned>` -- drives the
    user's Claude Code account login (Claude Agent SDK), not API billing."""
    return AcpWorkerLaunchProfile(
        command="npx",
        args=("-y", f"{CLAUDE_AGENT_ACP_PACKAGE}@{CLAUDE_AGENT_ACP_VERSION}"),
        env=_passthrough_env(_CLAUDE_OPTIONAL_PASSTHROUGH_VARS),
    )


def codex_acp_launch_profile() -> AcpWorkerLaunchProfile:
    """`npx -y @agentclientprotocol/codex-acp@<pinned>` -- drives the user's
    Codex/ChatGPT account login (`codex login`), not API billing."""
    return AcpWorkerLaunchProfile(
        command="npx",
        args=("-y", f"{CODEX_ACP_PACKAGE}@{CODEX_ACP_VERSION}"),
        env=_passthrough_env(),
    )


def gemini_cli_acp_launch_profile() -> AcpWorkerLaunchProfile:
    """`npx -y @google/gemini-cli@<pinned> --acp` -- drives the user's Gemini
    CLI account login (`gemini` OAuth) or, if the caller's own environment
    already has GEMINI_API_KEY/GOOGLE_API_KEY/GOOGLE_CLOUD_PROJECT/
    GOOGLE_GENAI_USE_VERTEXAI set, whatever auth mode those select --
    this preset never invents credentials, it only avoids dropping ones the
    caller already exported for an unrelated reason (same policy as the
    Claude/Codex presets above)."""
    return AcpWorkerLaunchProfile(
        command="npx",
        args=("-y", f"{GEMINI_CLI_PACKAGE}@{GEMINI_CLI_VERSION}", "--acp"),
        env=_passthrough_env(_GEMINI_OPTIONAL_PASSTHROUGH_VARS),
    )
