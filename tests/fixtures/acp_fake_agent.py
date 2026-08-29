"""A real ACP agent process, built on the official agent-client-protocol SDK,
used only by acp_runtime integration tests. Never imported by production
code. Mode is selected by argv[1]:

  echo          - initialize -> new_session -> prompt -> one
                  agent_message_chunk update -> stop_reason="end_turn".
  permission    - like echo, but first issues one real
                  session/request_permission with allow_once/allow_always
                  options before completing.
  hang          - never responds to prompt (drives the timeout test).
  child_process - spawns one harmless long-lived child process, writes its
                  PID to the path in $ACP_FAKE_AGENT_CHILD_PID_FILE, then
                  completes normally (drives descendant-cleanup test).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import acp


class FakeAgent:
    def __init__(self, conn: acp.Client, mode: str) -> None:
        self._conn = conn
        self._mode = mode
        self._children: list[subprocess.Popen] = []

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
        if self._mode == "hang":
            await asyncio.Event().wait()
            return acp.PromptResponse(stop_reason="end_turn")

        if self._mode == "permission":
            await self._conn.request_permission(
                session_id=session_id,
                tool_call=acp.schema.ToolCallUpdate(
                    tool_call_id="tool-1", title="Write a file",
                ),
                options=[
                    acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
                    acp.schema.PermissionOption(option_id="allow-always", name="Allow always", kind="allow_always"),
                ],
            )
            await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text("done"))
            return acp.PromptResponse(stop_reason="end_turn")

        if self._mode == "child_process":
            pid_file = os.environ["ACP_FAKE_AGENT_CHILD_PID_FILE"]
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._children.append(child)
            with open(pid_file, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
            await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text("spawned child"))
            return acp.PromptResponse(stop_reason="end_turn")

        if self._mode == "many_updates":
            count = int(os.environ.get("ACP_FAKE_AGENT_UPDATE_COUNT", "50"))
            for index in range(count):
                await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text(f"chunk {index}"))
            return acp.PromptResponse(stop_reason="end_turn")

        if self._mode == "env_probe":
            sentinel_file = os.environ["ACP_FAKE_AGENT_SENTINEL_FILE"]
            probe_var = os.environ.get("ACP_FAKE_AGENT_PROBE_VAR", "LOGNAME")
            probed_value = os.environ.get(probe_var, "<absent>")
            with open(sentinel_file, "w", encoding="utf-8") as handle:
                handle.write(f"{probe_var}={probed_value}\n")
            await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text("probed env"))
            return acp.PromptResponse(stop_reason="end_turn")

        # echo (default)
        await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text("hello from fake agent"))
        return acp.PromptResponse(stop_reason="end_turn")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    asyncio.run(acp.run_agent(lambda conn: FakeAgent(conn, mode), use_unstable_protocol=True))


if __name__ == "__main__":
    main()
