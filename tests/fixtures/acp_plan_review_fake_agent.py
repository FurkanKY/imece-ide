"""A real ACP agent process, built on the official agent-client-protocol
SDK, used only by the Planner/Reviewer ACP attempt-runner tests (T1.3). Never
imported by production code. A NEW fixture, kept separate from
tests/fixtures/acp_fake_agent.py (the ACP Worker fixture) rather than
extending it, since it needs a different, more flexible text/behavior
contract than the Worker fixture's fixed per-mode outputs.

Mode is selected by argv[1]:

  text        - initialize -> new_session -> prompt -> emits the contents of
                $ACP_FAKE_TEXT as one or more agent_message_chunk updates
                (split at $ACP_FAKE_TEXT_CHUNK_SIZE boundaries, default: one
                chunk) -> stop_reason="end_turn". Drives the Planner/
                Reviewer happy-path and protocol-error tests: whatever text
                is supplied is exactly what parse_plan_decision/
                parse_review_decision will see as final_text.
  thought     - like "text", but the SAME content is first emitted as one
                agent_thought_chunk update (which must be dropped, never
                folded into final_text) before the real agent_message_chunk
                update with $ACP_FAKE_TEXT.
  mutate      - writes acp_semantic_mutated.txt under the received cwd, then
                behaves like "text" (drives the read-only-violation tests:
                the resulting workspace mutation must be caught even though
                a normal-looking final answer was also produced).
  permission  - issues one real session/request_permission call (which the
                ACP client core already auto-denies) before behaving like
                "text" (drives the permission-auto-deny-is-not-fatal test).
  fail        - raises from prompt (drives ACP session infrastructure
                failure settlement).
"""

from __future__ import annotations

import asyncio
import os
import sys

import acp


class FakeAgent:
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
        return acp.NewSessionResponse(session_id="fake-plan-review-session-1")

    async def close_session(self, session_id, **kwargs):
        return None

    async def cancel(self, session_id, **kwargs):
        return None

    async def _emit_text(self, session_id: str) -> None:
        text = os.environ.get("ACP_FAKE_TEXT", "")
        chunk_size = int(os.environ.get("ACP_FAKE_TEXT_CHUNK_SIZE", str(len(text) or 1)))
        if not text:
            return
        for start in range(0, len(text), chunk_size):
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text(text[start : start + chunk_size]),
            )

    async def prompt(self, session_id, prompt, **kwargs):
        if self._mode == "fail":
            raise RuntimeError("fake plan/review ACP failure")

        if self._mode == "mutate":
            with open("acp_semantic_mutated.txt", "w", encoding="utf-8") as handle:
                handle.write("mutated during a read-only Planner/Reviewer attempt\n")

        if self._mode == "permission":
            await self._conn.request_permission(
                session_id=session_id,
                tool_call=acp.schema.ToolCallUpdate(tool_call_id="tool-1", title="Write a file"),
                options=[
                    acp.schema.PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
                ],
            )

        if self._mode == "thought":
            # Deliberately NOT valid JSON and unrelated to $ACP_FAKE_TEXT: if
            # this content ever leaked into final_text (instead of being
            # dropped), parse_plan_decision/parse_review_decision would fail
            # even when the real agent_message_chunk below carries valid
            # output.
            await self._conn.session_update(
                session_id=session_id,
                update=acp.schema.AgentThoughtChunk(
                    sessionUpdate="agent_thought_chunk",
                    content=acp.schema.TextContentBlock(
                        type="text", text="thinking out loud, this is not json at all"
                    ),
                ),
            )

        await self._emit_text(session_id)
        return acp.PromptResponse(stop_reason="end_turn")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "text"
    asyncio.run(acp.run_agent(lambda conn: FakeAgent(conn, mode), use_unstable_protocol=True))


if __name__ == "__main__":
    main()
