# Milestone 3J2A — ACP Client Core (design)

Checkpoint: `e2263c8c48f3031a8940d4d25bc2421be691e15f` (tag `milestone-3j1`).

```
Milestone 3J2A
IMPLEMENTED
HARDENING ROUND 4 COMPLETE
INDEPENDENT ACTUAL CODE REVIEW APPROVED
```

This milestone has passed independent actual-code review but is not yet
closed, checkpointed, committed, tagged, or pushed. It has been implemented
and hardened through round 4; the round-4 diff is retained as review
provenance.

> **Hardening round 1 correction**: the original version of this document
> described `acp.spawn_agent_process`'s host-env inheritance as "acceptable
> and out of our control." That was wrong: it violated the approved
> invariant that `AcpLaunchSpec.env` is the **exact** child environment.
> `acp_runtime` no longer uses `acp.spawn_agent_process` in production code
> at all. See "Exact child environment" below for the corrected
> architecture.

> **Hardening round 2 corrections**: round 1's own teardown lifecycle
> (`close_session -> conn.close() -> terminate_process_tree(pid)`) was
> itself insufficient in three ways, found by an independent full-diff
> review:
> 1. `terminate_process_tree` discovers descendants by walking the process
>    tree from the root pid *at cleanup time*. If a graceful operation
>    (`close_session`, `conn.close()`) causes the root to exit first, a
>    descendant that already existed can be reparented and become
>    permanently undiscoverable from the (now-dead) root pid.
> 2. The abort/cancel path was not actually bounded: `conn.cancel(...)`
>    itself had no timeout, and `_run_prompt`'s finalizer awaited
>    `prompt_task` unboundedly after requesting its cancellation — either
>    one could deadlock connection/process teardown indefinitely.
> 3. `spawn_acp_agent_connection` had a window, after
>    `create_subprocess_exec` succeeded but before `acp.connect_to_agent`
>    returned, where a real OS process existed with no owner: a
>    connection-construction failure in that window leaked the process.
>
> A related latent gap was also found and fixed while implementing the
> above: `acp.connect_to_agent`'s `Connection.close()` never actually closed
> the underlying `process.stdin` stream (unlike `acp.spawn_agent_process`'s
> own transport, which does), so a well-behaved agent could never observe
> end-of-input and exit gracefully at all — every agent process was always
> hard-killed, never gracefully closed. See "Process-tree snapshot",
> "Bounded abort/cancel path", "Post-spawn connection ownership", and
> "Connection teardown now closes stdin" below for the corrected
> architecture. Every other section has been re-verified against the
> twice-hardened implementation.

## Purpose

3J2A introduces a provider-neutral, asynchronous ACP Client Core:
`acp_runtime/`. It lets IMECE launch a single generic local-stdio ACP agent
subprocess, run exactly one prompt through it, observe bounded streaming
updates, always deny permission requests, and deterministically tear the
process tree down. It does **not** connect ACP to `FixLoopRunner` or to any
`fix_runtime.ports` attempt port — that is 3J2B (`executor_runtime/acp_worker.py`,
`run_runtime/acp.py`). No canonical `run_runtime` events are written here.

```
caller -> AcpClientRuntime -> official `acp` SDK (agent-client-protocol) -> local stdio ACP agent subprocess
```

## SDK baseline (inspected against the installed 0.12.1 package)

`pip show agent-client-protocol` confirms `agent-client-protocol==0.12.1`,
importable as `acp`. Facts below were read directly from the installed
package (`~/.local/lib/python3.12/site-packages/acp/`), not assumed:

- `acp.PROTOCOL_VERSION == 1` (int). We pass this value through, never a
  hardcoded literal.
- `acp.spawn_agent_process(to_client, command, *args, env=None, cwd=None, ...)`
  is an `@asynccontextmanager` yielding `(ClientSideConnection, asyncio.subprocess.Process)`.
  It calls `acp.spawn_stdio_transport` internally, which **always** merges
  the caller's `env` on top of `acp.default_environment()` — a small
  MCP-style curated subset of the *host's* variables (`HOME`, `LOGNAME`,
  `PATH`, `SHELL`, `TERM`, `USER` — see `acp/transports.py:
  DEFAULT_INHERITED_ENV_VARS`). This is a real, unconditional inheritance:
  even an empty `env={}` still yields a child that sees the host's `PATH`/
  `HOME`/etc. That **violates** the approved invariant
  (`AcpLaunchSpec.env == COMPLETE child environment`), so
  `acp.spawn_agent_process`/`acp.spawn_stdio_transport` are **not used
  anywhere in production `acp_runtime` code** — see "Exact child
  environment" below for the corrected architecture using
  `acp.connect_to_agent` instead.
- `acp.connect_to_agent(client, input_stream, output_stream, *, use_unstable_protocol=False, **kwargs) -> ClientSideConnection`
  builds a `ClientSideConnection` directly over caller-supplied
  `asyncio.StreamWriter`/`asyncio.StreamReader` byte streams, without
  touching process creation or environment at all — this is the seam that
  lets IMECE own the subprocess (and therefore its exact environment) while
  the SDK continues to own 100% of the JSON-RPC/NDJSON framing.
  `ClientSideConnection(to_client, input_stream, output_stream)` is the same
  class `spawn_agent_process` itself builds internally, so this is not a
  lesser-supported code path.
- `ClientSideConnection.initialize(protocol_version, client_capabilities=None, client_info=None)`
  substitutes `client_capabilities or ClientCapabilities()` when omitted.
  `ClientCapabilities().model_dump(exclude_none=True)` is
  `{'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False, 'auth': {'terminal': False}}`
  — i.e. passing `client_capabilities=None` already advertises no fs/terminal/
  auth-terminal capability. We rely on this default rather than constructing
  our own `ClientCapabilities()`, to keep "no capability we don't implement"
  self-evidently true from the SDK's own default.
- `ClientSideConnection.new_session(cwd, additional_directories=None, mcp_servers=None)`
  returns `NewSessionResponse{session_id, modes, config_options}`.
- `ClientSideConnection.prompt(session_id, prompt: list[ContentBlock])` returns
  `PromptResponse{stop_reason, usage}`.
- `ClientSideConnection.cancel(session_id)` sends `session/cancel` (fire-and-forget
  notification per spec; returns `None`).
- `ClientSideConnection.close_session(session_id)` sends `session/close` and
  returns `CloseSessionResponse | None`; only call when advertised. Note:
  the installed SDK's own `build_agent_router` marks `session/close` (like
  `session/fork`/`session/resume`) `unstable=True` on the *agent* side,
  meaning a router built with the default `use_unstable_protocol=False`
  rejects it with `method_not_found` regardless of what capability the
  agent advertised. This is a property of whichever agent process we talk
  to (out of our control), not something `acp_runtime`'s client needs to opt
  into — our outgoing `close_session` call itself is never gated by client-
  side `use_unstable_protocol`. `tests/fixtures/acp_fake_agent.py` passes
  `use_unstable_protocol=True` to `acp.run_agent(...)` purely so the test
  double can exercise the capability-gated code path end-to-end; this has
  no bearing on `session/fork`, `session/resume`, or any other explicitly
  out-of-scope unstable/experimental feature, which `acp_runtime` never
  calls.
- `ClientSideConnection.close()` closes the JSON-RPC connection itself
  (distinct from `close_session`). We always call this in a `finally`.
- Capability gate for session close:
  `InitializeResponse.agent_capabilities.session_capabilities.close` is
  `Optional[SessionCloseCapabilities]` — presence (not `None`) is the gate,
  matched exactly as required by spec section 28.
- `acp.RequestError(code, message, data)` is the SDK's JSON-RPC error type.
  `acp.RequestError.auth_required(...)` builds `code=-32000`. Read directly
  from `acp/exceptions.py` in the installed package — this is the
  spec/SDK-defined code we match on, never a message substring.
- `acp.text_block(text) -> TextContentBlock` is the SDK helper used to wrap
  `request.prompt` as the sole content block sent to `prompt(...)`.
- `acp.Client` is a `Protocol`. `acp/router.py` resolves an incoming method
  to `getattr(client_impl, attr, None)`; if the attribute is missing it
  raises `RequestError.method_not_found` automatically. So our concrete
  `_ImeceAcpClient` simply does not define `read_text_file`, `write_text_file`,
  `create_terminal`, `create_elicitation`, etc. — combined with advertising no
  such capability, a spec-compliant agent will never call them, and if one
  does anyway, the SDK itself fails it closed.
- `acp.run_agent(agent, ...)` / `acp.AgentSideConnection` are the SDK's
  agent-side helpers, used only in `tests/fixtures/acp_fake_agent.py`
  (test-only), never in production `acp_runtime` code.

No hand-written JSON-RPC framing is written anywhere in this milestone.

## Exact child environment (hardening round 1)

Architecture:

```
IMECE: asyncio.create_subprocess_exec(*argv, env=dict(launch.env), cwd=..., stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
    -> process.stdin (StreamWriter) / process.stdout (StreamReader)
    -> acp.connect_to_agent(client, process.stdin, process.stdout) -> ClientSideConnection
    -> official SDK JSON-RPC/NDJSON protocol implementation (unchanged, SDK-owned)
```

`acp_runtime/stdio.py` (private, not exported from `acp_runtime/__init__.py`)
holds the one function that does this: `spawn_acp_agent_connection(client,
argv, env, cwd) -> (ClientSideConnection, asyncio.subprocess.Process)`.
IMECE now owns subprocess creation end-to-end — `env=dict(launch.env)` is
passed to `asyncio.create_subprocess_exec` with **no merge of any kind**
(`os.environ`, `acp.default_environment()`, or otherwise). `AcpLaunchSpec.env`
is therefore the literal, exact child environment. `shell=True` is never
used (`create_subprocess_exec` never shells out). The SDK's own
`ClientSideConnection`/`Connection`/NDJSON framing is unchanged and fully
reused — only process *creation* moved into IMECE; the protocol itself
remains 100% SDK-owned, satisfying "do not write ACP JSON-RPC/NDJSON framing
manually."

`AcpClientRuntime.__init__` accepts test-only constructor seams
(`_connect`, `_terminate_process_tree`) defaulting to
`spawn_acp_agent_connection`/`terminate_process_tree` respectively, so unit
tests can substitute fakes without monkeypatching module globals.
`tests/test_acp_fake_agent_integration.py::test_exact_child_environment_*`
proves this end-to-end over a real subprocess and the real SDK connection: a
host environment variable (`LOGNAME`) that the old `spawn_agent_process`
path would have silently inherited is proven absent from the child unless
explicitly included in `AcpLaunchSpec.env`, and an explicitly supplied value
is proven to reach the child exactly.

## Package layout

```
acp_runtime/__init__.py   - public re-exports only (stdio.py is NOT re-exported)
acp_runtime/errors.py     - AcpRuntimeError hierarchy
acp_runtime/models.py     - AcpLaunchSpec, AcpPromptRequest, AcpClientLimits, AcpRunResult
acp_runtime/events.py     - transient AcpRuntimeEvent + AcpEventSink protocol
acp_runtime/stdio.py      - private: exact-env subprocess spawn (with post-spawn rollback ownership)
                            + acp.connect_to_agent binding + connection/stdin teardown
acp_runtime/client.py     - AcpClientRuntime, private _ImeceAcpClient
```

## Error hierarchy

```
AcpRuntimeError
  AcpInputError                    - malformed launch/request/limits/sink, before spawn
  AcpSpawnError                    - OS-level process launch failure
  AcpProtocolError                 - non-auth RequestError, schema/connection failure, foreign-session update
  AcpAuthenticationRequiredError   - RequestError.code == -32000
  AcpLimitError                    - update/size/count budget breach
  AcpTimeoutError                  - prompt_timeout_ms expired
  AcpEventSinkError                - event_sink.emit() raised
  AcpCleanupError                  - process-tree survivors after cleanup
```

Every wrap preserves `__cause__` via `raise ... from exc` at the point of
raising. Errors discovered inside `_ImeceAcpClient` callbacks (sink
failures, update-serialization failures) cannot use `raise ... from` because
they are *stored* on `_FatalSignal` and raised later by `_run_prompt`, not
raised immediately at the point of discovery — those instead set
`error.__cause__ = exc` explicitly at construction time, which is
semantically equivalent. Both paths are tested (`AcpEventSinkError.__cause__`
is asserted to be the original sink exception in both the update-path and
permission-path sink-failure tests). Permission denial is *not* an error —
it is the expected, only supported protocol decision in 3J2A (spec section
22).

## Models

`AcpLaunchSpec` (frozen dataclass, `slots=True`):
- `argv: tuple[str, ...]` — non-empty, absolute `argv[0]` required (see
  "Executable contract" below), every member a non-empty NUL-free `str`.
- `env: Mapping[str, str]` — defaults to `{}` via `field(default_factory=dict, repr=False)`;
  validated into a **read-only** `types.MappingProxyType` over a
  defensively-copied `dict` at construction, so (a) later caller-side
  mutation of the mapping object they passed in cannot mutate the spec, and
  (b) mutation attempted directly through `spec.env[...] = ...` raises
  `TypeError` — `AcpLaunchSpec` is genuinely immutable, not just
  defensively-copied-but-still-mutable. `dict(spec.env)` still works
  wherever a plain mapping is needed (e.g. `asyncio.create_subprocess_exec(env=...)`).
  Keys/values must be `str` and NUL-free. This is the **exact, complete**
  child environment IMECE supplies to the ACP subprocess — see "Exact child
  environment" above.

**Executable contract**: `argv[0]` must be an absolute path in 3J2A,
validated with `os.path.isabs(...)` — current-platform runtime semantics
(POSIX `isabs`/Windows `ntpath.isabs`), not a hand-rolled string parser, so
Windows drive-relative paths like `C:foo` are correctly rejected while
drive-rooted (`C:\foo`) and UNC (`\\server\share\foo`) paths are correctly
accepted on Windows. `AcpPromptRequest.cwd` uses the same `os.path.isabs`
check. A relative command name would additionally require deciding
cross-platform `.cmd`/`.bat` resolution semantics (`shutil.which` differs
from what `asyncio.create_subprocess_exec` does on Windows), which is a
materially larger design surface than this milestone needs. 3J2B's provider
launch profiles are expected to resolve a configured agent command to an
absolute path before constructing `AcpLaunchSpec`. No shell is ever invoked
(`asyncio.create_subprocess_exec` never shells out, and `shell=True` is
never passed).

`AcpPromptRequest` (frozen dataclass, `slots=True`):
- `cwd: str` — absolute, NUL-free. Existence/directory-ness is checked at
  `AcpClientRuntime.run(...)` time (a runtime fact, not a pure-value
  invariant), raising `AcpInputError`.
- `prompt: str` — non-empty after `.strip()`, NUL-free. `repr()` omits the
  prompt body (only shows a length) so prompt text does not leak into logs
  incidentally through object repr.
- No `Workspace` dependency anywhere in `acp_runtime` — 3J2B converts
  `GitWorktreeWorkspace.root` to `cwd`.

`AcpClientLimits` (frozen dataclass, `slots=True`) — every field validated
`isinstance(x, int) and not isinstance(x, bool) and x > 0`:
```
max_prompt_chars = 128_000
max_updates = 2_000
max_update_chars = 65_536
max_total_update_chars = 1_000_000
prompt_timeout_ms = 300_000
cancel_grace_ms = 2_000
session_close_timeout_ms = 2_000
```
No zero-means-infinite mode.

`AcpRunResult` (frozen dataclass, `slots=True`) — stable execution facts
only: `session_id, stop_reason, update_count, update_chars,
permission_request_count, session_close_supported, session_close_succeeded`.
No prompt text, transcript, environment, or provider `_meta`.

## Transient events (`acp_runtime/events.py`)

`AcpRuntimeEvent` is a small union of dataclasses, explicitly *not*
`run_runtime` canonical events (no `RunEvent`, no `execution_id`, no
persistence):
- `AcpSessionUpdateObserved(session_id, update, serialized_chars)`
- `AcpPermissionRequested(session_id, tool_call_id, title, option_ids)`
- `AcpPermissionResolved(session_id, tool_call_id, outcome)` — `outcome` is
  always the literal string `"cancelled"` in 3J2A.

`update` on `AcpSessionUpdateObserved` is the raw SDK-typed update value
(e.g. `AgentMessageChunk`) — the event itself is not retained by
`AcpClientRuntime` after `emit()` returns; only counters survive (see
"No transcript accumulation").

`AcpEventSink` is a small synchronous `Protocol` (`emit(event) -> None`),
with `NullAcpEventSink` as the no-op default. Synchronous by design: 3J2B's
canonical recorder is itself synchronous and can implement this port
directly without a second async event system. If `emit()` raises, the
active prompt is cancelled and `AcpClientRuntime.run(...)` ultimately raises
`AcpEventSinkError`; cleanup still runs.

Provider `_meta` is never copied into these events, and is never used to
make a permission decision (spec section 15/22).

## `_ImeceAcpClient` (private, `acp_runtime/client.py`)

Implements only the two client-side handlers 3J2A needs:
- `session_update(session_id, update, **kwargs)` — fenced on the bound
  `session_id` (a `None`-bound or foreign `session_id` triggers
  `AcpProtocolError` via the fatal-signal path and is never merged into
  counters/events), then bounds-checks and emits `AcpSessionUpdateObserved`,
  or raises through the fatal-signal path on limit/sink failure.
- `request_permission(session_id, tool_call, options, **kwargs)` — fenced
  identically to `session_update` (a `None`-bound or foreign `session_id`
  triggers `AcpProtocolError`, emits no permission events, and does not
  increment `permission_request_count`). Otherwise: emits
  `AcpPermissionRequested` first; if that emission fails, the sink failure
  is fatal-signaled, `permission_request_count` is **not** incremented, and
  `AcpPermissionResolved` is **never attempted** (no risk of emitting a
  "resolved" event for a request the sink never successfully saw). Only if
  the request event succeeds does the count increment and
  `AcpPermissionResolved(outcome="cancelled")` get emitted (a failure there
  is likewise fatal-signaled, independently). In every case — fenced,
  sink-failed, or normal — the method still returns
  `RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))`
  regardless of `options` content, `tool_call.title`, `tool_call.kind`, or
  `_meta`. Never selects `options[0]`, `allow_once`, or `allow_always`: the
  ACP wire response is always fail-closed even when the *invocation* is
  simultaneously being aborted for an unrelated reason.

No `read_text_file`, `write_text_file`, `create_terminal`,
`create_elicitation`, or other `Client` Protocol method is defined on this
class — combined with the default (non-advertising) `ClientCapabilities`,
this makes "IMECE never advertises or serves fs/terminal/elicitation"
verifiable both from the initialize call arguments and from the concrete
class's method set.

## Concurrent abort signal

`conn.prompt(...)` is a single coroutine that only returns when the agent
finishes (or the connection breaks). A limit breach or sink failure is
discovered from *inside* `_ImeceAcpClient.session_update`, which runs as a
callback invoked by the SDK's own request-dispatch loop concurrently with
the outstanding `prompt()` future. `AcpClientRuntime.run` races:
- the `prompt()` task, and
- an `asyncio.Event` ("fatal signal") that `_ImeceAcpClient` sets and that
  carries the first fatal exception,

via `asyncio.wait(..., return_when=FIRST_COMPLETED)`.

**Race fix (hardening round 1)**: `_FatalSignal.error` is written
*synchronously* by `trigger()` (before the `asyncio.Event` waiter is even
scheduled to wake). Checking `fatal_task in done` alone is insufficient: it
is possible for the prompt to also complete successfully in the very same
scheduling window a fatal signal was raised, such that `asyncio.wait()`
returns with only `prompt_task` in `done` even though `fatal.error` is
already non-`None` (the underlying `asyncio.Event` waiter needs one more
loop tick to formally resolve `fatal_task`, but `trigger()`'s synchronous
attribute write already happened). `AcpClientRuntime._run_prompt` therefore
checks `fatal.error is not None` directly — the authoritative state,
immediately after `asyncio.wait()` returns and before any further `await` —
rather than relying on task-completion-set membership. **Fatal always wins
over prompt success.** `tests/test_acp_client.py::test_simultaneous_prompt_success_and_fatal_state_always_returns_fatal`
constructs exactly this scheduling window deterministically (the fake
`prompt_behavior` triggers a limit-breach fatal synchronously, then returns
a successful `PromptResult` in the same tick) and asserts the fatal error is
still raised.

If the fatal signal (or a timeout) wins, `AcpClientRuntime` sends
`session/cancel`, gives the prompt task up to `cancel_grace_ms` to settle,
proceeds to cleanup regardless, then raises the stored fatal error. A
`finally` block around the whole raced wait unconditionally cancels and
awaits both `prompt_task` and `fatal_task` if either is not already done —
this is the true backstop that guarantees no background task is ever left
running past `_run_prompt`'s return, including when the coroutine itself
exits via `asyncio.CancelledError` (caller-side cancellation), not only
along the timeout/fatal/success branches.

## Update bounds (spec section 23)

For every `session_update` call: serialize the update via the official
generated Pydantic model surface (`update.model_dump_json()`, inspected as
the correct SDK method on `SessionUpdate` variant types) to measure
`serialized_chars`. Check, in order: single-update size vs
`max_update_chars`; increment `update_count`/`total_chars`; count vs
`max_updates`; total vs `max_total_update_chars`. Any breach is terminal
(`AcpLimitError` via the fatal-signal path) — never truncate-and-continue.
Only counters are retained; no update payload or concatenated text is
stored by `AcpClientRuntime` beyond the single `emit()` call.

## Session-id fencing (spec section 24)

After `new_session` returns `session_id`, `_ImeceAcpClient` is told that
value. Any `session_update`/`request_permission` callback carrying a
different `session_id` fails closed via `AcpProtocolError` on the
fatal-signal path — it is never merged into the active invocation's
counters or events.

## Bounded abort/cancel path (spec section 26, corrected in hardening round 2)

Round 1's `_cancel_and_settle` sent `session/cancel` with no bound, and its
`_run_prompt` finalizer awaited a cancelled `prompt_task` with no bound
either. Either could deadlock: a stalled `conn.cancel(...)` call, or a
prompt coroutine that only settles once the connection is later closed
(exactly the shape a well-behaved agent's own prompt implementation can
take), would prevent `_run_prompt` from ever returning, which in turn
prevented `AcpClientRuntime.run`'s teardown `finally` from ever running.

Fixed shape: `_cancel_and_settle` bounds **both** operations independently
by `cancel_grace_ms` — sending `session/cancel`
(`asyncio.wait_for(conn.cancel(session_id), timeout=cancel_grace_ms/1000)`)
and the voluntary settle-wait
(`asyncio.wait_for(asyncio.shield(prompt_task), timeout=cancel_grace_ms/1000)`)
each get their own bound, so a stalled `cancel()` call can never by itself
block reaching outer teardown. If the prompt still hasn't settled after
both bounds, `_cancel_and_settle` requests cancellation
(`prompt_task.cancel()`) and returns immediately — it never awaits the task
further itself. Ownership of that still-possibly-running task moves to a
run-local `owned_tasks: set[asyncio.Task]` (populated the moment
`prompt_task`/`fatal_task` are created, never stored as `AcpClientRuntime`
instance state — see "Public API"), which `AcpClientRuntime.run`'s teardown
`finally` drains in one final bounded step **after** connection close and
process cleanup have already run (see "Lifecycle ownership" below) — by
then a real SDK task should settle quickly on its own, since the transport
it was waiting on no longer exists. A task that survives even that final
bounded drain raises `AcpCleanupError`, never silently ignored. On overall
`prompt_timeout_ms` expiry: send `session/cancel` (bounded), allow up to
`cancel_grace_ms` for the prompt call to settle (bounded), proceed to
teardown, then raise `AcpTimeoutError`. Never returns a synthetic
`AcpRunResult`. Never retries.

## Session close (spec section 28, corrected in hardening round 1)

Gated strictly on `InitializeResponse.agent_capabilities.session_capabilities.close is not None`
(never guessed from method presence). Ownership lives in the single
`finally` block inside `AcpClientRuntime.run` (see "Lifecycle ownership"
below) so best-effort `close_session(session_id)` — bounded by
`session_close_timeout_ms` via `asyncio.wait_for` — is attempted **on every
path that got as far as creating a session**: normal success, timeout, and
fatal-signal abort alike. Failure is recorded as
`session_close_succeeded=False` and does **not** invalidate an otherwise
successful prompt result, and does **not** mask an earlier typed failure —
the process is destroyed unconditionally right after anyway. If
unsupported: `session_close_supported=False`, `session_close_succeeded=None`.
If no session was ever created (e.g. `initialize`/`new_session` itself
failed), `close_session` is never called regardless of advertised
capability.

## Post-spawn connection ownership (hardening round 2, blocker 3)

`spawn_acp_agent_connection` (`acp_runtime/stdio.py`) now owns rollback for
the narrow window between a successful `create_subprocess_exec` and a
successful handoff of `(conn, process)` back to `AcpClientRuntime`: if
validating the stdio pipes or constructing `acp.connect_to_agent(...)`
fails (or the coroutine is cancelled) in that window, the helper itself
terminates the already-created process tree
(`await asyncio.to_thread(terminate_process_tree, process.pid)`) before
propagating — a connection-construction failure is mapped to
`AcpProtocolError` (cause preserved), rollback failure escalates to
`AcpCleanupError` (with the connection-construction failure preserved as
its cause), and `asyncio.CancelledError` propagates unmodified unless
rollback itself fails. A genuine pre-process-creation `OSError` from
`create_subprocess_exec` is untouched by this and still surfaces to
`AcpClientRuntime.run` to be mapped to `AcpSpawnError` (no process ever
existed to roll back in that case).

## Connection teardown now closes stdin (hardening round 2)

`acp.connect_to_agent`'s `ClientSideConnection.close()` (via `Connection.close()`
-> `NdjsonTransport.close()` -> `MessageSender.close()`) only stops the
SDK's own internal send-loop task — inspected directly in the installed
package, it never touches `process.stdin`. That stream-lifecycle
responsibility belongs to whoever owns the stream, which is now IMECE (see
"Exact child environment"), not the SDK (which normally would have handled
it inside `acp.spawn_stdio_transport`'s own teardown, unused here).
Without an explicit fix, no ACP agent process spawned by `acp_runtime`
could ever exit gracefully — every single run would end in a hard
`terminate_process_tree` kill, even for perfectly well-behaved agents.
Fixed by `acp_runtime/stdio.py: close_acp_agent_connection(conn, process)`:
`await conn.close()` followed by explicit `process.stdin.write_eof()` /
`drain()` / `close()` (each individually best-effort via
`contextlib.suppress`). `AcpClientRuntime.run`'s teardown calls this instead
of `conn.close()` directly.

## Process-tree snapshot (hardening round 2, blocker 1)

`terminate_process_tree`'s no-snapshot algorithm discovers descendants by
walking the tree from the root pid *at cleanup time* — always did, and
still does when called with no snapshot (byte-identical behavior for
existing `ProcessRunner` callers, proven by rerunning
`tests/test_process_runtime.py`/`tests/test_process_tool.py` unmodified).
The gap: if a **prior** teardown stage (`close_session`, the now-EOF-sending
`close_acp_agent_connection`) causes the agent root to exit *before* hard
cleanup runs, any descendant that existed at that point can be reparented
by the OS and become permanently undiscoverable from the now-dead root pid.

Fixed in `process_runtime/cleanup.py` with two additions, both additive
(no change to the existing no-snapshot call shape or behavior):

```python
@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshot:
    root: "psutil.Process | None"
    descendants: tuple["psutil.Process", ...]

def capture_process_tree(pid: int) -> ProcessTreeSnapshot: ...

def terminate_process_tree(pid: int, *, snapshot: ProcessTreeSnapshot | None = None) -> None: ...
```

`ProcessTreeSnapshot` stores live `psutil.Process` handles, not bare pid
ints — psutil tracks pid+create_time identity internally per handle, so a
handle captured before a graceful exit remains a valid, specific reference
to that exact process even after the OS potentially reuses its pid for
something unrelated later; termination through a stale/reused-pid handle
cannot occur. `AcpClientRuntime.run`'s teardown `finally` calls
`self._capture_process_tree(process.pid)` as its very first step — *before*
`close_session`/`close_acp_agent_connection` — then passes that snapshot to
the final `terminate_process_tree(process.pid, snapshot=snapshot)` call.
`terminate_process_tree` with a snapshot unions the snapshot's captured
processes with a **fresh** rescan from the (possibly now-dead) root pid, so
it catches both: descendants that existed before the root exited (via the
snapshot, when the fresh rescan finds nothing because the root is gone) and
descendants spawned *during* graceful teardown (via the fresh rescan,
which the snapshot alone would miss). A snapshot-capture failure
(`ProcessCleanupError`, e.g. a permissions error) is recorded but does
**not** skip the remaining teardown stages (spec section 10); it is folded
into the same `AcpCleanupError` precedence as every other cleanup failure
if the final hard-cleanup step doesn't already produce one.

## Lifecycle ownership: connection teardown + deterministic process-tree cleanup

Once `spawn_acp_agent_connection` returns a `(conn, process)` pair, exactly
one `finally` block in `AcpClientRuntime.run` owns unwinding, in this fixed
order, on **every** exit path (success, any typed `AcpRuntimeError`, an
unexpected exception, or `asyncio.CancelledError`):

1. `snapshot = self._capture_process_tree(process.pid)` — see "Process-tree
   snapshot" above; a capture failure is recorded but does not skip stages
   2-4;
2. best-effort, capability-gated `close_session` (see "Session close"
   above);
3. `await asyncio.wait_for(close_acp_agent_connection(conn, process), timeout=session_close_timeout_ms/1000)`
   — official-SDK connection teardown plus explicit stdin EOF (see
   "Connection teardown now closes stdin" above), bounded (never an
   unbounded wait). A failure here maps to `AcpProtocolError` **only if
   there is no earlier primary failure**; if a primary failure already
   exists, it is preserved unchanged (connection-close failure never masks
   it);
4. `await asyncio.to_thread(terminate_process_tree, process.pid, snapshot=snapshot)`
   — unconditional hard process-tree kill (snapshot-aware, see above), run
   off the event loop thread so the synchronous psutil waits never block
   it;
5. `await self._drain_owned_tasks(owned_tasks, limits)` — the final bounded
   reap of any still-owned `prompt_task`/`fatal_task` (see "Bounded
   abort/cancel path" above), run only now that the connection and process
   are already gone.

`process_runtime/runner.py`'s existing private `_terminate_tree(pid)` — a
security-critical psutil-based tree-walk/terminate/kill/verify algorithm —
was extracted verbatim (no behavioral redesign) to
`process_runtime/cleanup.py` as public `terminate_process_tree(pid: int, *, snapshot=None) -> None`,
raising the existing `ProcessCleanupError` on survivors.
`process_runtime/runner.py` calls the extracted function instead of a
module-private helper; `ProcessRunner` behavior is otherwise unchanged.
`acp_runtime` reuses this same function rather than duplicating the
algorithm.

**Precedence** (spec section 14): the *first* cleanup-stage failure
encountered (snapshot capture, hard termination, or a surviving owned task)
is what gets raised, wrapped as `AcpCleanupError` — it is always the single
most severe outcome, and is never itself silently overwritten by a later
cleanup stage succeeding. If a primary failure (typed error or
connection-close-mapped error) already existed, `AcpCleanupError.__cause__`
preserves *that*; if cleanup alone fails with no earlier primary failure,
`AcpCleanupError.__cause__` preserves the underlying cleanup exception
instead — either way no diagnostic is lost
(`raise AcpCleanupError(...) from (primary_exc or cleanup_error)`).

**Cancellation** (spec section 10): because none of the `finally` block's
own exception handling catches `asyncio.CancelledError` (only
`Exception`/`ProcessCleanupError`), a caller cancelling the `run()` call
while it is in flight still runs this full teardown sequence before the
`CancelledError` continues propagating unmodified — it is never converted
to `AcpProtocolError`. (The one exception: if cleanup itself then fails,
`AcpCleanupError` legitimately takes precedence over the propagating
cancellation, matching the general precedence rule above — cleanup failure
is treated as more severe than any other single outcome, cancellation
included.)

## Public API

```python
class AcpClientRuntime:
    def __init__(self, *, _connect=None, _terminate_process_tree=None, _capture_process_tree=None) -> None: ...

    async def run(
        self,
        launch: AcpLaunchSpec,
        request: AcpPromptRequest,
        *,
        limits: AcpClientLimits | None = None,
        event_sink: AcpEventSink | None = None,
    ) -> AcpRunResult: ...
```

`_connect`/`_terminate_process_tree`/`_capture_process_tree` are test-only
constructor seams (default to `spawn_acp_agent_connection`/
`terminate_process_tree`/`capture_process_tree`); real callers never pass
them. Async-only — no `run_sync()`, no internal `asyncio.run()`. 3J2B owns
the sync `WorkerAttemptRunner` <-> async ACP bridge. One invocation = one
fresh subprocess = one fresh session; no pooling, no reuse, no global
connection state on `AcpClientRuntime` itself. This is now an explicit,
tested invariant (hardening round 2): the constructor stores only the three
seam callables (immutable after construction); everything else — the
process-tree snapshot, the `owned_tasks` set tracking `prompt_task`/
`fatal_task`, the `_FatalSignal`, the `_ImeceAcpClient` instance — is a
fresh local created inside each `run()` call and never attached to `self`,
so one `AcpClientRuntime` instance is safe for independent/concurrent
`run()` invocations.

`event_sink.emit` must be a genuinely synchronous callable — passing an
`async def emit(...)` (a coroutine function) raises `AcpInputError` before
any subprocess is spawned, so a misconfigured sink can never leave an
un-awaited coroutine silently dropped.

## Out of scope for 3J2A (deferred)

`executor_runtime/acp_worker.py`, `run_runtime/acp.py`, ACP<->FixLoopRunner
wiring, `RoutingPolicy`, CLI fallback, HTTP/WebSocket ACP transport,
`session/fork`, provider extensions, interactive authentication, MCP
servers, `additionalDirectories`, provider-specific (Codex/Claude) launch
logic, UI changes.
