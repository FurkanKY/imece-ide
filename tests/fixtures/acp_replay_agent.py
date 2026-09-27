"""A real ACP agent process (official agent-client-protocol SDK) that plays
back a recorded, sanitized transcript instead of an LLM. Used only by
acp_runtime/executor_runtime integration tests -- never imported by
production code.

See tests/fixtures/acp_replay/README.md for the transcript format and the
provenance of tests/fixtures/acp_replay/calc_average_fix.jsonl.

Configuration (environment variables):
  ACP_REPLAY_TRANSCRIPT   Required. Path to the .jsonl transcript to play.
  ACP_REPLAY_OUTSIDE      Optional. Absolute path substituted for the
                          "{OUTSIDE}" placeholder in the transcript
                          (defaults to a fixed path outside any worktree).

The transcript's "{WORKTREE}" placeholder is substituted with the `cwd`
the real ACP client passes to `session/new`, exactly as a real agent would
resolve paths relative to the session it was launched into.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import acp

_DEFAULT_OUTSIDE = "/tmp/acp-replay-outside-fixture/secrets.txt"


def _load_transcript(path: str) -> list[dict]:
    steps = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            steps.append(json.loads(line))
    return steps


class ReplayAgent:
    def __init__(self, conn: acp.Client, transcript: list[dict], outside_path: str) -> None:
        self._conn = conn
        self._transcript = transcript
        self._outside_path = outside_path
        self._cwd: str | None = None

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        return acp.InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=acp.schema.AgentCapabilities(
                session_capabilities=acp.schema.SessionCapabilities(
                    close=acp.schema.SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
        )

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        self._cwd = cwd
        return acp.NewSessionResponse(session_id="replay-session-1")

    async def close_session(self, session_id, **kwargs):
        return None

    async def cancel(self, session_id, **kwargs):
        return None

    def _sub(self, raw_path: str) -> str:
        return raw_path.replace("{WORKTREE}", self._cwd or "").replace("{OUTSIDE}", self._outside_path)

    async def prompt(self, session_id, prompt, **kwargs):
        stop_reason = "end_turn"
        for step in self._transcript:
            op = step["op"]

            if op == "session_update":
                if step.get("kind") == "agent_message_chunk":
                    await self._conn.session_update(
                        session_id=session_id, update=acp.update_agent_message_text(step["text"])
                    )
                continue

            if op == "request_permission":
                locations = [
                    acp.schema.ToolCallLocation(path=self._sub(raw)) for raw in step.get("locations", [])
                ]
                options = [
                    acp.schema.PermissionOption(
                        option_id=option["option_id"], name=option["name"], kind=option["kind"]
                    )
                    for option in step["options"]
                ]
                # Announce the tool call first (ToolCallStart), exactly like
                # the real recorded session did before every
                # session/request_permission call.
                await self._conn.session_update(
                    session_id=session_id,
                    update=acp.schema.ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=step["id"],
                        title=step["title"],
                        kind=step["tool_kind"],
                        status="pending",
                        locations=locations,
                    ),
                )
                response = await self._conn.request_permission(
                    session_id=session_id,
                    tool_call=acp.schema.ToolCallUpdate(
                        tool_call_id=step["id"],
                        title=step["title"],
                        kind=step["tool_kind"],
                        locations=locations,
                    ),
                    options=options,
                )
                # A "selected" outcome only means the client picked one of
                # the offered options -- it may have picked a reject option
                # (see acp_runtime.permission_policy.WorktreeEditAcpPermissionPolicy,
                # which selects "reject_once" rather than cancelling
                # outright whenever one is offered). What actually
                # determines whether the agent may write is whether the
                # SPECIFIC option it selected is this step's "allow" option.
                allow_option_id = next(
                    (o["option_id"] for o in step["options"] if o["kind"] == "allow_once"), None
                )
                selected_option_id = getattr(response.outcome, "option_id", None)
                allowed = allow_option_id is not None and selected_option_id == allow_option_id
                effect = step.get("on_allow") if allowed else step.get("on_deny")
                if effect:
                    write = effect.get("write_file")
                    if write:
                        target = Path(self._sub(write["path"]))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(write["content"], encoding="utf-8")
                    message = effect.get("message")
                    if message:
                        await self._conn.session_update(
                            session_id=session_id, update=acp.update_agent_message_text(message)
                        )
                continue

            if op == "complete":
                stop_reason = step.get("stop_reason", "end_turn")
                continue

            raise ValueError(f"Unknown replay transcript op: {op!r}")

        return acp.PromptResponse(stop_reason=stop_reason)


def main() -> None:
    transcript_path = os.environ["ACP_REPLAY_TRANSCRIPT"]
    outside_path = os.environ.get("ACP_REPLAY_OUTSIDE", _DEFAULT_OUTSIDE)
    transcript = _load_transcript(transcript_path)
    asyncio.run(
        acp.run_agent(lambda conn: ReplayAgent(conn, transcript, outside_path), use_unstable_protocol=True)
    )


if __name__ == "__main__":
    main()
