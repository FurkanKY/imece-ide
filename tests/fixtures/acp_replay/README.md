# ACP replay fixtures

This directory holds sanitized, replayable transcripts of real ACP
(Agent Client Protocol) sessions, for tests that need to drive
`acp_runtime`/`executor_runtime` against something more realistic than the
hand-written fake agents in `tests/fixtures/acp_fake_agent.py` and
`tests/fixtures/acp_permission_worker_agent.py` (both of which script a
single synthetic tool call rather than replaying an actual recorded
session).

## Provenance: `calc_average_fix.jsonl`

Reconstructed from the first real end-to-end IMECE run where every role
(planner/worker/reviewer) was Claude Code speaking ACP, against the sample
repo `~/imece-deneme`. That run is `run_6eb8cb41-aa13-4417-bcc7-c1a2e8bab5e4`
in the canonical store `~/.multi_agent_ide/runtime.sqlite3` (`runs` /
`run_events` tables; read read-only, from a copy).

**The canonical store does not persist raw ACP JSON-RPC.** It persists
canonical `RunEvent`s that `run_runtime.acp.CanonicalAcpEventSink` derives
from the transient `acp_runtime.events` observations
(`AcpSessionUpdateObserved`, `AcpPermissionRequested`,
`AcpPermissionResolved`) as a run happens -- see `run_events.type in
{execution.output, permission.requested, permission.resolved,
execution.started, execution.completed}` for that run. This fixture is a
faithful reconstruction from those events (order, tool kinds, locations,
option ids, message content, and the real fix content), expressed through
the same ACP SDK builder calls a real fake agent uses (`acp.schema.*`,
`acp.update_agent_message_text`) -- not a byte-for-byte replay of captured
wire JSON, because that was never recorded.

What actually happened in that run (before commit `0d31ecf`'s
per-role-permission-policy fix, `acp_runtime/permission_policy.py`): the
real agent sent three `session/request_permission` calls (two
`Edit calc.py`, one `python3 -m pytest -q`), and the IMECE ACP client of
that time (`DenyAllAcpPermissionPolicy`'s unconditional behavior, before it
was even factored out as a named policy) responded `cancelled` to all
three -- so the worker could not edit the file and the run ended with
`run.completed {"reason": "no_changes"}`. This fixture and
`tests/test_acp_replay.py` cover that fix from both sides, replaying the
same multi-step transcript through each policy:

- with `acp_runtime.permission_policy.WorktreeEditAcpPermissionPolicy` (the
  policy `executor_runtime.acp_worker.AcpWorkerAttemptAdapter` supplies for
  the ACP Worker role), the in-worktree file edit is granted (`allow_once`)
  and the file change actually happens;
- the shell execute and the edit outside the worktree are both rejected
  (the policy selects an offered `reject_once` option rather than
  cancelling outright, whenever one is offered);
- with no policy at all (`DenyAllAcpPermissionPolicy`, still the default
  for every non-Worker caller such as the Planner/Reviewer), all three
  resolve `cancelled` and nothing is ever written -- reproducing the real
  run's actual pre-fix outcome cheaply, without needing a second fixture.

One step in this fixture (`p3`, "Edit outside-worktree file") is
**synthetic**: the real run never asked the agent to touch a path outside
the worktree, so there was nothing to reconstruct for that case. It was
added so the replay can assert the "outside path" half of the policy
without inventing a separate fixture. It is marked with a `"provenance"`
key in its transcript line.

### Sanitization

Compared to the real run:

- All absolute paths under the real machine's home directory (e.g.
  `/home/<user>/.multi_agent_ide/workspaces/<run_id>/calc.py`) were
  replaced with the placeholder `{WORKTREE}/calc.py`, substituted at
  replay time with the real `cwd` the ACP client passes to `session/new`
  (see `tests/fixtures/acp_replay_agent.py:ReplayAgent._sub`).
- The synthetic outside-path step uses `{OUTSIDE}`, substituted with a
  fixed path outside the worktree (overridable via the
  `ACP_REPLAY_OUTSIDE` environment variable).
- No usernames, emails, tokens, machine identifiers, or task/run UUIDs
  from the real run appear anywhere in the fixture.
- High-volume, low-signal updates from the real run
  (`available_commands_update`, `usage_update`, `session_info_update`)
  were dropped -- they carry no information any test asserts on and would
  only bloat the fixture. What remains is exactly the sequence relevant to
  the permission bug: two `agent_message_chunk` summaries (paraphrased,
  not the literal recorded text) and the three `request_permission` calls
  with their real tool kinds, locations, option ids, and (for the allowed
  edit) the real fix content (`if not numbers: return 0.0`).
- The task's own file content is the tiny sample repo's `calc.py` fix,
  which contains no private data.

## Format

One JSON object per line (`.jsonl`). Each object has an `"op"` field:

- `"session_update"` -- `{"op": "session_update", "kind": "agent_message_chunk", "text": "..."}`.
  Replayed as `acp.update_agent_message_text(text)`.
- `"request_permission"` -- one real `session/request_permission` call:
  ```json
  {
    "op": "request_permission",
    "id": "<tool_call_id>",
    "title": "<human-readable title>",
    "tool_kind": "edit | execute | ...",
    "locations": ["<absolute path, possibly with a {WORKTREE}/{OUTSIDE} placeholder>", ...],
    "options": [{"option_id": "...", "name": "...", "kind": "allow_once | allow_always | reject_once | reject_always"}, ...],
    "on_allow": {"write_file": {"path": "...", "content": "..."}, "message": "..."},
    "on_deny": {"message": "..."}
  }
  ```
  The replay agent sends the real `session/request_permission` request
  (through the official SDK) and applies `on_allow`/`on_deny` based on the
  client's actual response -- it does not assume the outcome.
- `"complete"` -- `{"op": "complete", "stop_reason": "end_turn"}`. Ends the
  turn with that `stop_reason`.

## Replaying a fixture

`tests/fixtures/acp_replay_agent.py` is a real ACP agent (built on the
`agent-client-protocol` SDK, same as `tests/fixtures/acp_fake_agent.py`)
that speaks ACP over stdio and plays one transcript:

```
ACP_REPLAY_TRANSCRIPT=tests/fixtures/acp_replay/calc_average_fix.jsonl \
  python tests/fixtures/acp_replay_agent.py
```

See `tests/test_acp_replay.py` for how `AcpClientRuntime` is launched
against it.

## Recording a new fixture

1. Run a real end-to-end task (any role set that includes at least one ACP
   role) against a small, throwaway sample repo -- never a real project
   with private content.
2. Find the run in `~/.multi_agent_ide/runtime.sqlite3` (`runs` table, by
   `task_id`/`created_at`; `run_id` is `run_<uuid>`). Copy the DB file
   before reading it -- never open the live store directly while IMECE
   might be writing to it.
3. Pull that run's `run_events` ordered by `seq`. The relevant types are
   `execution.started`, `execution.output` (payload `update` is the
   serialized ACP session update), `permission.requested`,
   `permission.resolved`, and `execution.completed`.
4. Reconstruct a `.jsonl` transcript from those events by hand, in order:
   - one `session_update` line per `agent_message_chunk` you want to keep
     (paraphrase free-text content; never keep it verbatim if it might
     contain anything task-specific/private -- the point is realistic
     shape, not the literal words);
   - one `request_permission` line per `permission.requested` event, using
     that event's `title`/`option_ids` and the matching `tool_call_update`
     content (`kind`, `locations`) from `execution.output` (join on
     `tool_call_id`); fill in `on_allow`/`on_deny` with what a client
     *should* do now (e.g. write the real edited content on an allowed
     in-worktree edit), not necessarily what the old buggy client did;
   - a trailing `complete` line with the real `stop_reason`.
5. Replace every absolute path under a real home directory with
   `{WORKTREE}` (or `{OUTSIDE}` for an intentionally-out-of-worktree case)
   and drop anything identifying (usernames, emails, tokens, task/run
   UUIDs, unrelated file contents).
6. Add a short provenance note at the top of this README describing which
   run it came from and what, if anything, was synthesized rather than
   reconstructed.
