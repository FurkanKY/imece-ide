"""A real ACP agent process that behaves like real Claude Code did on its
first end-to-end pipeline run: it sends a `session/update` ToolCallStart for
the tool call it is about to make, then a real `session/request_permission`
for it, and only actually writes to disk if the client's response selected
an "allow" option. Used only by acp_runtime/executor_runtime integration
tests -- never imported by production code.

This is a SEPARATE fixture from acp_fake_agent.py's own "permission" mode:
that one never checks the permission response at all (it always proceeds),
which is exactly why the real first end-to-end run's deny-all client bug
went uncaught. This fixture is the one that reproduces the actual failure
mode: no write happens unless permission was actually granted.

Mode is selected by argv[1]; every mode requests permission for one tool
call, then writes (or doesn't) depending on the response:

  edit            - kind="edit", one location: "<cwd>/target.txt"
                    (ACP_FAKE_AGENT_TARGET overrides the filename).
  execute         - kind="execute" (a Bash-shaped tool call), no
                    locations -- must never be granted.
  edit_outside    - kind="edit", one location given by the absolute path in
                    $ACP_FAKE_AGENT_OUTSIDE_PATH (outside the worktree).
  edit_symlink    - kind="edit", one location: "<cwd>/<ACP_FAKE_AGENT_TARGET>"
                    where the caller has pre-created that path as a symlink
                    escaping the worktree -- the agent process itself does
                    not know or care that it is a symlink, it just writes
                    through the path it was told to use, exactly like a
                    real agent would.

Every mode offers the two option kinds a real Worker session realistically
offers: "allow_once" and "reject_once".
"""

from __future__ import annotations

import asyncio
import os
import sys

import acp

_PERMISSION_OPTIONS = [
    acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
    acp.schema.PermissionOption(option_id="reject-once", name="Reject once", kind="reject_once"),
]


def _target_path(cwd: str) -> str:
    name = os.environ.get("ACP_FAKE_AGENT_TARGET", "target.txt")
    return os.path.join(cwd, name)


class PermissionAwareFakeAgent:
    def __init__(self, conn: acp.Client, mode: str) -> None:
        self._conn = conn
        self._mode = mode

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
        return acp.NewSessionResponse(session_id="fake-session-1")

    async def close_session(self, session_id, **kwargs):
        return None

    async def cancel(self, session_id, **kwargs):
        return None

    async def prompt(self, session_id, prompt, **kwargs):
        cwd = os.getcwd()

        if self._mode == "edit":
            kind = "edit"
            locations = [acp.schema.ToolCallLocation(path=_target_path(cwd))]
            write_path = _target_path(cwd)
        elif self._mode == "execute":
            kind = "execute"
            locations = []
            write_path = os.path.join(cwd, "executed.txt")
        elif self._mode == "edit_outside":
            kind = "edit"
            outside = os.environ["ACP_FAKE_AGENT_OUTSIDE_PATH"]
            locations = [acp.schema.ToolCallLocation(path=outside)]
            write_path = outside
        elif self._mode == "edit_symlink":
            kind = "edit"
            path = _target_path(cwd)
            locations = [acp.schema.ToolCallLocation(path=path)]
            write_path = path
        else:
            raise ValueError(f"Unknown mode: {self._mode!r}")

        # Announce the tool call first (ToolCallStart), exactly like real
        # Claude Code does before asking for permission.
        await self._conn.session_update(
            session_id=session_id,
            update=acp.schema.ToolCallStart(
                session_update="tool_call",
                tool_call_id="tool-1",
                title="Edit target.txt" if kind == "edit" else "Run a command",
                kind=kind,
                status="pending",
                locations=locations,
            ),
        )

        response = await self._conn.request_permission(
            session_id=session_id,
            tool_call=acp.schema.ToolCallUpdate(
                tool_call_id="tool-1",
                title="Edit target.txt" if kind == "edit" else "Run a command",
                kind=kind,
                locations=locations,
            ),
            options=_PERMISSION_OPTIONS,
        )

        granted = getattr(response.outcome, "option_id", None) == "allow-once"
        if granted:
            with open(write_path, "w", encoding="utf-8") as handle:
                handle.write("written by permission-aware fake agent\n")
            await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text("done"))
        else:
            await self._conn.session_update(
                session_id=session_id, update=acp.update_agent_message_text("permission denied; nothing written")
            )

        return acp.PromptResponse(stop_reason="end_turn")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "edit"
    asyncio.run(acp.run_agent(lambda conn: PermissionAwareFakeAgent(conn, mode), use_unstable_protocol=True))


if __name__ == "__main__":
    main()
