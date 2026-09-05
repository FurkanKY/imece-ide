# ACP Worker Attempt Adapter Design

## Status and purpose

This document specifies Milestone 3J2B, the narrow adapter that connects the
completed ACP Client Core to the existing synchronous
`fix_runtime.ports.WorkerAttemptRunner` port. Its only responsibility is to
answer how one Fix Worker attempt runs over ACP. It does not select an
executor, schedule an attempt, decide whether a fix is correct, or settle a
Run.

The checkpointed 3J2A baseline is
`7d30e94435747adf64a5fb96dc2f75da30a90edf` with tag `milestone-3j2a`.
3J2B begins from that contract and preserves the ACP Client Core's fresh
process, fresh session, exact-environment, permission-denial, cleanup, and
provider-neutral behavior.

## Scope

3J2B includes:

- a provider-neutral `AcpWorkerLaunchProfile` and one-time executable
  resolution;
- `AcpWorkerAttemptAdapter`, implementing the current synchronous
  `WorkerAttemptRunner` protocol;
- an ACP-only mutation boundary accepting `GitWorktreeWorkspace` and
  rejecting `LocalWorkspace` and arbitrary `Workspace` implementations;
- exact construction of `AcpPromptRequest` from the worktree root and
  `FixWorkerRequest.rendered_input`;
- one fresh `AcpClientRuntime.run()` call per adapter invocation;
- a synchronous canonical event sink in `run_runtime/acp.py`;
- canonical execution lifecycle, ACP output, and permission persistence;
- typed translation of expected ACP infrastructure failures;
- unit, bridge, FixLoop, and real fake-agent/worktree integration tests.

3J2B excludes provider-specific launch or authentication rules, routing,
executor selection, CLI fallback, session reuse/resume/fork, process pooling,
non-stdio ACP transports, interactive permission approval, waiting-user
semantics, MCP, ACP terminal/filesystem extensions, native AgentSession
lifecycle emulation, invented model/turn/tool events, token/cost fabrication,
and changes to `FixLoopRunner` or the `WorkerAttemptRunner` port.

3J3 owns CLI fallback. 3K owns routing policy and executor/provider
selection. Neither milestone is implemented or designed here beyond these
explicit boundaries.

## Current repository contracts

The implementation must consume these existing interfaces as they are
currently defined:

`WorkerAttemptRunner.run` is synchronous and has the shape

```python
run(workspace, request: FixWorkerRequest, *, execution_id: str) -> WorkerAttemptResult
```

`WorkerAttemptResult` validates the stable execution identifier. The adapter
must return `WorkerAttemptResult(execution_id=execution_id)` with the supplied
value unchanged. `FixLoopRunner` already records `fix_attempt.started`,
calls the worker, verifies the worker's canonical execution terminal, and
then records `fix_attempt.completed`; that ordering is not changed.

`FixWorkerRequest` already validates and carries the trust-boundary-rendered
`rendered_input`. `render_fix_worker_input()` owns its framing and budget.
The adapter never reconstructs it from `task`, `trigger`, or `plan`.

`AcpLaunchSpec` and `AcpPromptRequest` are frozen, slotted ACP models. The
launch spec defensively copies its mapping into an immutable view and treats
`env` as the complete child environment. `AcpPromptRequest` requires an
absolute cwd and a non-empty, non-whitespace, NUL-free prompt; it does not
enforce the configured `AcpClientLimits.max_prompt_chars` bound. 3J2B uses
the constructor for shape validation and duplicates that one configured
prompt-size check before canonical side effects.

`AcpClientRuntime.run()` is asynchronous, accepts an `AcpLaunchSpec`, an
`AcpPromptRequest`, optional `AcpClientLimits`, and a synchronous
`AcpEventSink`, and returns `AcpRunResult`. Transient ACP event models
remain in `acp_runtime`; they contain no canonical run identity or
persistence concerns.

`RunRuntime.record()` and `record_many()` append durable events with an
`expected_last_event_seq` optimistic precondition. `RunEventSpec` payloads
must satisfy the repository's strict canonical JSON value contract. A
canonical sink is constructed only for a `RunStatus.RUNNING` Run.

The requested test filenames do not all exist in this checkout. The current
equivalents are `tests/test_native_worker_adapter.py`,
`tests/test_fix_loop_runner.py`, `tests/test_run_service.py`, and the
other `tests/test_run_*.py` modules. New 3J2B coverage uses the proposed
`tests/test_acp_worker.py` and `tests/test_acp_worker_integration.py`
files, then extends current equivalents only where an existing integration
contract is the narrowest seam.

## Dependency direction

The dependency graph is:

```text
fix_runtime.ports.WorkerAttemptRunner
             ^
             |
executor_runtime.acp_worker
       |                 |
       v                 v
 acp_runtime       run_runtime.acp
                         |
                         v
                    acp_runtime events
```

`acp_runtime` remains provider-neutral and imports neither `run_runtime`,
`executor_runtime`, nor `fix_runtime`. `run_runtime.acp` may consume the
transient ACP event models and `AcpRunResult`; it does not make the ACP core
know about canonical storage. `executor_runtime.acp_worker` composes the
three layers. `fix_runtime.runner` remains an orchestration consumer of the
existing port.

## Launch profile and executable resolution

The provider-neutral configuration is conceptually:

```python
@dataclass(frozen=True, slots=True)
class AcpWorkerLaunchProfile:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
```

The exact annotation may use the repository's preferred mapping import, but
the observable semantics are fixed:

- `command` is a non-empty string with no NUL;
- each argument is a non-empty NUL-free string;
- mutable argument inputs are copied to a tuple;
- the environment is copied and exposed as an immutable mapping;
- no adapter call mutates the caller's profile or its source containers.

`resolve_acp_worker_launch(profile)` runs before the first canonical event
or ACP side effect and returns an `AcpLaunchSpec`.

If `command` is absolute, resolution validates that exact path as an
executable without consulting PATH. If it is not absolute, resolution calls
`shutil.which(command)` once and requires a resolved absolute executable
path. An empty result, a non-file, a non-executable path, or malformed profile
data raises `ExecutorAdapterInputError`. The resolved argv is exactly
`(absolute_executable, *profile.args)`.

The resolved spec's environment is exactly the frozen profile environment.
The host environment may be consulted only by the operating system's
executable-discovery operation for a relative command. The adapter never
merges `os.environ`, `os.get_exec_path()`, a default PATH, HOME,
authentication variables, or any SDK-curated environment into
`AcpLaunchSpec.env`. An empty profile environment remains empty. The
existing 3J2A stdio path receives that mapping as the complete child
environment.

## Adapter interface and ownership

The injected ACP dependency is structural rather than an exact concrete
instance requirement. The private adapter-side seam is conceptually:

```python
class _AcpClientRunner(Protocol):
    async def run(
        self,
        launch: AcpLaunchSpec,
        request: AcpPromptRequest,
        *,
        limits: AcpClientLimits | None = None,
        event_sink: AcpEventSink | None = None,
    ) -> AcpRunResult: ...
```

The production object is `AcpClientRuntime`, but a non-subclass test double
with a compatible callable `run` is valid. This protocol is not added to
`acp_runtime`; it is an adapter-local dependency-injection contract.

The primary class is:

```python
class AcpWorkerAttemptAdapter:
    def __init__(
        self,
        runtime: RunRuntime,
        run_id: str,
        launch_profile: AcpWorkerLaunchProfile,
        acp_client: _AcpClientRunner,
        *,
        limits: AcpClientLimits | None = None,
    ) -> None: ...

    @property
    def run_id(self) -> str: ...

    def run(
        self,
        workspace,
        request: FixWorkerRequest,
        *,
        execution_id: str,
    ) -> WorkerAttemptResult: ...
```

The constructor validates the bound `run_id`, runtime, profile, effective
limits, and that the injected ACP dependency exposes a callable `run`; it
does not require `isinstance(acp_client, AcpClientRuntime)`. It stores the
supplied `AcpClientLimits`, or one fresh default `AcpClientLimits()` when no
limits are supplied. Invalid limits configuration is translated to
`ExecutorAdapterInputError` before any attempt. The call performs remaining
request, workspace, ID, executable, cwd, prompt, effective prompt-limit, and
event-loop validation before `execution.started`, subprocess spawn,
connection, or mutation. There is no mutable per-run state on the adapter.
The adapter stores only immutable configuration and injected dependencies; a
call-local sink, prompt, launch spec, coroutine, and result are created for
that invocation.

The injected runner is stored as `self._acp_client`; the effective limits are
stored as `self._limits`. Both are immutable adapter configuration values and
are reused unchanged for sequential calls.

The effective limits field is exactly `self._limits`:

```python
if limits is None:
    self._limits = AcpClientLimits()
elif not isinstance(limits, AcpClientLimits):
    raise ExecutorAdapterInputError(...)
else:
    self._limits = limits
```

The field is immutable adapter configuration and is reused for every
sequential call.

One `run()` means exactly one `AcpClientRuntime.run()` invocation, one
fresh ACP subprocess, one fresh ACP session, and one prompt. A sequential
second call creates an independent invocation and cannot reuse a session,
process, event sequence, request, or result from the first call. The ACP
runtime's existing one-run ownership and cleanup semantics remain
authoritative.

## Pre-side-effect validation

Validation occurs in this order, with no canonical write or ACP call until all
checks pass:

1. `request` is a `FixWorkerRequest`;
2. `workspace` is a `GitWorktreeWorkspace`;
3. `execution_id` passes the existing stable-ID contract, by constructing the
   expected `WorkerAttemptResult`;
4. the launch profile is resolved and produces an absolute executable;
5. `workspace.root` is converted to an absolute string cwd and is an
   existing directory;
6. `AcpPromptRequest(cwd=str(workspace.root), prompt=request.rendered_input)`
   accepts the exact prompt;
7. `len(prompt_request.prompt) <= self._limits.max_prompt_chars`;
8. the current thread is not already running an asyncio event loop.

Every rejected input raises `ExecutorAdapterInputError`, except the
active-event-loop condition, which raises `ExecutorAdapterExecutionError`
because the input can be valid while the synchronous bridge is unusable in
that thread. No rejected case records an event, invokes
`AcpClientRuntime.run()`, connects or spawns an ACP process, or mutates a
workspace.

The adapter intentionally duplicates only the pre-spawn local prompt-size
check needed for its stronger no-side-effect contract. `AcpClientRuntime`
retains and remains authoritative for its own complete validation, including
the same prompt-size check. The active-loop check uses
`asyncio.get_running_loop()` and occurs before constructing the coroutine
passed to `asyncio.run()`. The adapter therefore does not create an unawaited
coroutine or emit a warning. The check is made after local input validation
but before `sink.start()`.

## Workspace isolation and prompt construction

Only `GitWorktreeWorkspace` is accepted. A `LocalWorkspace`, the abstract
`Workspace` base, and every unrelated implementation are rejected before
canonical lifecycle recording. This prevents the ACP process from receiving
the user's source checkout as its worker cwd.

The prompt request is built exactly once:

```python
AcpPromptRequest(
    cwd=str(workspace.root),
    prompt=request.rendered_input,
)
```

There is no prefix, suffix, provider instruction, markdown/JSON wrapper,
re-rendering, duplication of `request.task`, or concatenation of
`NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS`. Exact string equality of the ACP
prompt is tested.

The real integration uses `GitWorktreeWorkspace.create()`, a local fake ACP
agent, and a known file. The agent is launched with the shadow worktree cwd
and mutates that file. The test proves the shadow linked worktree contains the
mutation while the original source repository remains unchanged. Workspace
disposal is part of test cleanup even when assertions fail as far as the
fixture can guarantee.

## Canonical ACP event sink

`run_runtime/acp.py` introduces a one-execution-only
`CanonicalAcpEventSink`. It consumes the existing synchronous
`AcpEventSink.emit(event)` interface and owns the canonical ACP lifecycle.
Its constructor receives `RunRuntime`, `run_id`, and the supplied
`execution_id`; it requires the Run to be `RunStatus.RUNNING` and captures
the current `last_event_seq`. It exposes focused lifecycle operations
equivalent to:

```python
    sink.start(task: str)
sink.emit(transient_event)
sink.complete(acp_result)
sink.fail(acp_error)
```

The sink rejects a second start, events before start, events after terminal
settlement, foreign session IDs, mismatched execution identity, and a second
terminal event. It starts with `bound_session_id = None`. The first transient
event with a session ID binds that value; every later transient event must
match it. If no transient event arrived, `complete(result)` binds the result's
`session_id`; otherwise completion requires the result session ID to match
the bound value. `fail(error)` never invents a session ID. A mismatch is a
canonical provenance failure. Every generated `RunEventSpec` has:

```text
source = "acp_worker"
execution_id = supplied execution_id
correlation_id = supplied execution_id
```

No new execution ID is generated. The sink advances its local expected
sequence only after a successful `record_many()` call. It never refreshes
and silently retries after `EventSequenceError`; a concurrent writer is an
integrity conflict and surfaces to the adapter.

### Execution lifecycle

`start(task: str)` records exactly one `execution.started` with this exact
payload:

```json
{
  "transport": "acp",
  "task": "<FixWorkerRequest.task>"
}
```

The adapter passes `request.task` exactly. The payload contains no rendered
prompt, environment, argv, executable path, authentication material, or
other launch detail. A successful `complete(result)` records exactly one
`execution.completed`. A typed ACP infrastructure
failure after start invokes `fail(error)` once to record exactly one
`execution.failed`, when canonical persistence remains available.

The adapter calls `sink.start()` immediately before the ACP runtime call.
This preserves the existing FixLoop sequence:

```text
fix_attempt.started
execution.started
execution.output / permission.*
execution.completed or execution.failed
fix_attempt.completed or fix_attempt.interrupted
```

The sink is not responsible for `fix_attempt.*`, Run terminal events, or the
correctness of the workspace. `FixLoopRunner` and `RunCompletionGate`
retain those responsibilities.

### Session updates

`AcpSessionUpdateObserved` maps only to
`RunEventType.EXECUTION_OUTPUT`. Its payload has the bounded shape:

```json
{
  "transport": "acp",
  "session_id": "...",
  "update": {},
  "serialized_chars": 123
}
```

The `update` value is converted through the official SDK/Pydantic
serialization surface, using the update model's JSON-mode dump, then checked
with the repository's strict canonical JSON validator. Nested containers are
copied before persistence. The sink never calls `str()`, `repr()`, or an
arbitrary object serializer. If the official serialization surface is absent,
raises, produces a non-canonical value, or exceeds the existing bounded
update facts, `emit()` raises and the ACP client converts that failure
through the existing `AcpEventSinkError` path.

The bridge does not emit `model.*`, `turn.*`, `tool.*`, or
`usage.recorded`. An ACP session update is not evidence of the native
AgentSession lifecycle.

### Permissions

The canonical bridge constants are:

```text
MAX_CANONICAL_ACP_TEXT_CHARS = 2_000
MAX_CANONICAL_ACP_PERMISSION_OPTIONS = 128
```

For permission persistence, `session_id`, `tool_call_id`, `title`, every
`option_id`, and `outcome` must each be strings, contain no NUL, and have at
most `MAX_CANONICAL_ACP_TEXT_CHARS` characters. `option_ids` may contain at
most `MAX_CANONICAL_ACP_PERMISSION_OPTIONS` entries. These provenance-bearing
facts are rejected when malformed or over limit; they are never silently
truncated. The existing 3J2A update-size limits remain the only update-size
budget. They are not duplicated or replaced by a second update budget.

`AcpPermissionRequested` maps to `permission.requested` with ACP-native
facts `session_id`, `tool_call_id`, `title`, and `option_ids`. Each fact is
validated by the exact bounds above. `AcpPermissionResolved` maps to
`permission.resolved` with `session_id`, `tool_call_id`, and the ACP
`outcome`; the value `cancelled` is preserved exactly.

3J2A automatically denies/cancels permission requests. The adapter never
emits `run.waiting_user` or `run.resumed`, and it does not reinterpret a
cancelled permission as a tool failure. No native tool event is inferred.

## Success result and terminal payloads

On successful ACP execution, `sink.complete()` records only the real fields
provided by `AcpRunResult`:

- `transport` = `"acp"`;
- `session_id`;
- `stop_reason`;
- `update_count`;
- `update_chars`;
- `permission_request_count`;
- `session_close_supported`;
- `session_close_succeeded`.

It does not add `final_text`, model turns, tool calls/errors, token counts,
or cost. After the durable completed event succeeds, the adapter returns
`WorkerAttemptResult(execution_id=execution_id)`. Verification and Reviewer
remain the only correctness authorities.

Failure payloads contain only bounded diagnostics: `transport`, a stable
exception class name from the expected ACP hierarchy, and a human-readable
message with NUL characters removed and truncated to exactly the maximum
length `MAX_CANONICAL_ACP_TEXT_CHARS` (2,000 characters). The adapter does not
include the launch mapping, full child environment, authentication material,
the full prompt, or arbitrary exception representations.

## Error translation and precedence

Expected ACP runtime failures include `AcpSpawnError`,
`AcpProtocolError`, `AcpAuthenticationRequiredError`, `AcpLimitError`,
`AcpTimeoutError`, `AcpEventSinkError`, and `AcpCleanupError`.

| Situation | Canonical action | Raised adapter error |
| --- | --- | --- |
| Local input or workspace/profile/ID/cwd/prompt validation | no event | `ExecutorAdapterInputError` |
| Running event loop in current thread | no event | `ExecutorAdapterExecutionError` |
| ACP typed or ordinary `Exception`-based failure after `execution.started` and terminal store healthy | exactly one best-effort append of `execution.failed` | `ExecutorAdapterExecutionError` with the original failure as `__cause__` |
| ACP success and durable completed append | append `execution.completed` | `WorkerAttemptResult` |
| Post-start failure plus failed terminal failure append | no refresh/retry; surface canonical failure | `ExecutorAdapterExecutionError` caused by the terminal persistence error, with the original failure retained in the exception context/cause chain |
| Unexpected ordinary adapter dependency failure before `execution.started` | no event | `ExecutorAdapterExecutionError` with the dependency error as cause |

Canonical integrity/storage failure has higher precedence than the ACP
transport/protocol failure. After durable `execution.started`, every ordinary
`Exception`-based failure inside the adapter-owned execution path gets
exactly one best-effort `sink.fail(...)` attempt unless the sink is already
terminal or the failure itself proves canonical persistence unavailable or
conflicted. This includes ACP runtime failures, result validation failures,
completion-payload construction failures, and a `sink.complete()` failure
before terminal persistence succeeds. If `sink.fail()` succeeds, raise
`ExecutorAdapterExecutionError` from the original failure. If `sink.fail()`
fails, raise `ExecutorAdapterExecutionError` from the terminal canonical
failure; its context/cause chain retains the original failure. The terminal
failure append is attempted once with the sink's current optimistic sequence.
If it raises `EventSequenceError` or another run-runtime storage/validation
error, the adapter does not refresh and retry and does not replace that
conflict with a misleading ACP-only error.

`asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit` are not
casually caught by the synchronous adapter. Caller/task cancellation retains
the ACP Client Core's cleanup semantics and is allowed to propagate after the
async run returns control. Process-level termination exceptions are not
converted into ordinary adapter errors. Only expected `Exception`-based ACP
infrastructure errors and ordinary dependency failures go through the typed
adapter translation above.

## Synchronous-to-asynchronous bridge

After validation and `sink.start()`, the adapter calls:

```python
asyncio.run(
    self._acp_client.run(
        launch_spec,
        prompt_request,
        limits=self._limits,
        event_sink=sink,
    )
)
```

The coroutine expression is created only after the active-loop check. Every
call owns one fresh event loop through `asyncio.run()`; there is no
persistent loop thread, global loop, daemon worker, nested loop,
`nest_asyncio`, fire-and-forget task, or adapter-level asynchronous state.
`asyncio.run()` must return only after the ACP runtime's own cleanup has
finished.

If the current thread has a running loop, the adapter raises
`ExecutorAdapterExecutionError` before `sink.start()` and before
constructing the coroutine. The test captures warnings and proves there is no
unawaited coroutine warning, no process/connect call, and no canonical
execution event.

## Optimistic sequencing and canonical storage failure

The sink's sequence starts from the `RUNNING` Run's current
`last_event_seq`. Each append supplies that exact expected value and
advances only on success. ACP callback ordering is serialized by the
synchronous sink; each event is durable before the next ACP observation is
accepted.

If output or permission persistence fails, `CanonicalAcpEventSink.emit()`
raises the underlying serialization or run-runtime persistence exception. It
does not construct or own `AcpEventSinkError`. The ACP core's
`_ImeceAcpClient._emit()` catches that sink failure and wraps it as
`AcpEventSinkError`; the adapter then applies the post-start settlement rule
above. A direct sink test therefore sees the underlying canonical error,
while an ACP-core/adapter test sees `AcpEventSinkError` before adapter
translation. A terminal failure write error is never silently swallowed and
never allows a successful `WorkerAttemptResult`.

## FixLoop ordering and provenance

`FixLoopRunner` continues to create a fresh `worker_execution_id`, persist
`fix_attempt.started`, invoke the worker port, and require the latest
execution lifecycle event for that exact ID to be `execution.completed` before
it records `fix_attempt.completed`. The ACP adapter supplies the same
execution ID on every ACP canonical event and on its `WorkerAttemptResult`.

For an ACP infrastructure failure, the adapter records `execution.failed`
when possible and raises `ExecutorAdapterExecutionError`. Existing
`FixLoopRunner` infrastructure-failure settlement then observes the worker
failure and records its existing interrupted/failed fix-loop outcome. No
FixLoopRunner redesign or special ACP branch is permitted.

## Freshness and concurrency

The adapter instance is reusable for sequential calls and may be configured
with immutable launch and limit values. All execution-specific values are
call-local. A `CanonicalAcpEventSink` is never reused across executions.
Two independent calls must not share a session, process, sink, expected
sequence, execution ID, prompt, or result. The injected `AcpClientRuntime`
remains safe because its process, connection, task, and cleanup state is
local to each `run()` invocation.

Concurrent calls are permitted only if the bound `RunRuntime` and ACP
client contracts are independently safe; no adapter-level mutable state is
used to coordinate them. A given sink is still one-execution-only and rejects
foreign event identity.

## Cancellation and BaseException behavior

The asynchronous ACP core remains the owner of cancellation-resistant
teardown, including connection close, process-tree cleanup, and bounded final
owned-task draining. The sync adapter does not wrap `asyncio.run()` in a
broad `BaseException` handler. If `asyncio.CancelledError` escapes the ACP
runtime after its cleanup, it propagates. `KeyboardInterrupt` and
`SystemExit` also propagate unchanged. The adapter does not invent
`execution.failed` for these control-flow or process-level termination
signals; ordinary typed ACP infrastructure failures are the translated
failure class.

## Test strategy

Implementation follows strict RED -> GREEN TDD. Every unit begins with a
regression test that fails against the unimplemented adapter or sink for the
intended reason, then adds the smallest production change and reruns the
focused test. The matrix includes:

- launch profile immutability, absolute/relative/missing executable
  resolution, exact args, exact environment, and host-environment
  non-inheritance;
- every pre-side-effect rejection with assertions for no canonical event, no
  ACP invocation, no connect/spawn, and no workspace mutation;
- exact cwd, exact prompt, exact supplied execution ID, exact
  `execution.started` payload, effective-limit rejection before start, a
  structural non-subclass ACP fake, and fresh repeated calls;
- one started and one terminal execution event, source/correlation identity,
  ACP result-only completion facts, and no invented native lifecycle events;
- JSON-mode SDK serialization, canonical JSON validation, arbitrary-object
  rejection, bounded output, direct underlying persistence failure,
  ACP-wrapper `AcpEventSinkError`, and optimistic sequence conflict behavior;
- exact canonical constants and validation tests for permission title,
  option count, option ID, NUL facts, and a diagnostic exactly bounded to
  2,000 characters;
- first-transient session binding, foreign later-session rejection,
  completion binding with no updates, and completion/result mismatch;
- ordinary post-start dependency failure settlement, failed completion never
  returning a worker result, and terminal persistence precedence with both
  failures reachable in the exception chain;
- permission requested/resolved mapping, cancelled outcome preservation, and
  absence of waiting-user/resumed events;
- each expected ACP error class, cause preservation, bounded safe diagnostics,
  and terminal canonical failure precedence;
- exactly one async runtime invocation, active-loop rejection without an
  unawaited coroutine warning, no background loop thread, and sequential
  freshness;
- unchanged FixLoop ordering and infrastructure-failure settlement; and
- a real fake-agent process in a real linked worktree proving source/shadow
  isolation and completed-event-only-on-success.

The existing `tests/fixtures/acp_fake_agent.py` is extended for ACP
scenarios instead of introducing a second JSON-RPC implementation. Tests use
the official SDK and existing ACP fixture seams; they do not hand-write
protocol framing. Fake ACP client tests inject the client and process seams
so no unit test touches unrelated real process IDs.

## Future boundaries

3J3 adds CLI fallback as a separate Worker execution path and does not alter
the ACP adapter's launch or event rules. 3K adds RoutingPolicy and
executor/provider selection above the adapter. Neither concern belongs in
`AcpWorkerAttemptAdapter`, `CanonicalAcpEventSink`, or `acp_runtime`.

## Implementation hardening round 1

An independent review of the implemented (not merely designed) 3J2B code
found six defects. Each is now fixed; the corrected contracts below
supersede the corresponding passages earlier in this document.

**Canonical persistence-broken sink state.** `CanonicalAcpEventSink` gained
`self._persistence_error: Exception | None`, set inside `_append()` when the
actual `record_many()` call itself fails (never for ordinary
payload/provenance validation errors where no append was attempted), and
exposed read-only via `sink.persistence_error`. `_expected_seq` is never
advanced on that failure, and it is never refreshed from a fresh
`get_run()` read.

**No terminal append after known canonical conflict.** `sink.fail()` now
checks `self._persistence_error` first: if already set, it immediately
re-raises that stored exception instead of attempting another
`record_many()` with the now-stale expected sequence. The adapter mirrors
this: after any post-start failure, it checks `sink.persistence_error`
*before* deciding whether to call `sink.fail()` at all. If persistence is
already known broken, the adapter raises `ExecutorAdapterExecutionError`
directly from `sink.persistence_error` (never from a fabricated
second-attempt error), with the original ACP/completion failure preserved
in the exception's `__context__`. This applies uniformly to a streaming
`AcpEventSinkError`-wrapped canonical failure and to a `sink.complete()`
sequence conflict.

**Optional ACP permission title.** ACP's `ToolCallUpdate.title` is
optional/nullable, and the 3J2A client already normalizes an absent title
to `""`. The former `_permission_text()` required every permission field
non-empty, which made a conforming empty title a fatal sink failure. It is
replaced by `_canonical_text(value, *, field, allow_empty=False)`; every
call site keeps `allow_empty=False` except `title`, which now persists
`title=""` exactly, never a fabricated placeholder.

**Portable child-environment key validation.** `AcpWorkerLaunchProfile.env`
key validation now additionally rejects an empty key and a key containing
`"="` (both of which Python subprocess creation deterministically rejects)
at profile-construction time, before any adapter attempt or
`execution.started` exists. An empty environment *value* remains valid.

**Canonical session-id bound.** `session_id` is agent-controlled provenance
persisted in `execution.output`, `permission.requested`,
`permission.resolved`, and `execution.completed`. `_bind_session()` now
routes through the same `_canonical_text()` bound (non-empty, NUL-free, at
most `MAX_CANONICAL_ACP_TEXT_CHARS` = 2,000 characters, no truncation) used
for permission facts, everywhere a session identity is bound.

**Strict SDK/Pydantic update serialization.** `_serialize_update()`'s
raw-dict/JSON-primitive bypass (`if update is None or isinstance(update,
(dict, list, str, int, float, bool)): return update`) is removed entirely.
Every ACP session update, with no exception, must expose a callable
`model_dump` and is serialized via
`update.model_dump(mode="json", by_alias=True, exclude_none=True)`, then
validated through the repository's canonical JSON contract. A JSON-shaped
plain dict is rejected with `TypeError`, not persisted merely because it
happens to already be JSON-compatible. Older sink tests that had injected
raw dict updates now use the existing `_sdk_update()` SDK-model helper.

A seventh, related fix closes the same "leak an infrastructure exception
type the caller shouldn't see" gap one layer earlier: constructing
`CanonicalAcpEventSink` against a non-`RUNNING` Run raises a raw
`ValueError` from the sink; the adapter now catches that specific
`ValueError` around sink construction only (never around an actual
attempted append) and translates it to `ExecutorAdapterInputError`, since
no execution has begun and no side effect has occurred.

## Implementation hardening round 2

An independent review of the Round-1 diff accepted every Round-1 fix and
found one remaining security blocker in `AcpWorkerAttemptAdapter._failure_message()`.

**Sequential replacement was not overlap-safe.** The Round-1 implementation
still redacted secrets via sequential `str.replace()` calls (prompt, then
each env key, then each env value). When one sensitive literal is a
substring of another -- an env key that is a prefix of its own value (e.g.
key `"TOKEN"`, value `"TOKEN_SECRET"`), or a prompt that overlaps a longer
env value -- replacing the shorter/earlier literal first destroys the exact
substring the later replacement needs to match, leaving a fragment of the
secret in the canonical diagnostic. No reordering of the replacement calls
closes this for every possible overlap.

**Secret-bearing diagnostics now fail closed to one fixed safe message.**
`_failure_message()` no longer performs any partial textual substitution.
It first builds the complete set of non-empty sensitive literals for this
attempt: the exact prompt, every `launch.env` key, every `launch.env`
value, and every `launch.argv` member (including the resolved executable
path -- launch arguments are opaque at this layer and may later carry
provider/authentication material, so they are treated as sensitive
unconditionally). If the raw (NUL-stripped) exception message contains
*any* of these literals anywhere, the entire diagnostic is discarded and
replaced with one fixed constant,
`executor_runtime.acp_worker.SAFE_REDACTED_DIAGNOSTIC_MESSAGE` -- never a
partially-redacted reconstruction, and never a message that itself names or
hints at the detected secret. Only when no sensitive literal is present at
all is the raw diagnostic retained.

**The two concerns stay separate.** A diagnostic that is *sensitive* is
replaced wholesale, regardless of length. A diagnostic that is merely
*long but non-sensitive* still passes through unchanged from
`_failure_message()` and is still bounded to exactly
`MAX_CANONICAL_ACP_TEXT_CHARS` (2,000) characters by
`CanonicalAcpEventSink.fail()`, unmodified from Round 1. These are
independent tests and independent code paths: sensitivity detection never
truncates, and the 2,000-character bound never inspects for secrets.

No behavior from Implementation Hardening Round 1 changed:
`CanonicalAcpEventSink.persistence_error`, `_append()`, and `fail()` are
untouched; the adapter's persistence-precedence check (skip `sink.fail()`
when `sink.persistence_error is not None`) is untouched; the optional
permission title, portable environment-key validation, canonical
session-id bound, and strict SDK/Pydantic update serialization are all
unmodified.

Milestone 3J2B
DESIGN COMPLETE
DESIGN HARDENING ROUND 3 COMPLETE
IMPLEMENTATION PLAN COMPLETE
IMPLEMENTED
IMPLEMENTATION HARDENING ROUND 1 COMPLETE
IMPLEMENTATION HARDENING ROUND 2 COMPLETE
INDEPENDENT ACTUAL CODE REVIEW PENDING
