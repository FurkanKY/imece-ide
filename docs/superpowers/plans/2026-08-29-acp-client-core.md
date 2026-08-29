# ACP Client Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Status note:** this plan documents Milestone 3J2A as actually built and
> hardened through hardening round 4. All checkboxes below are
> checked because the work is complete at the time of writing; the plan is
> kept in this format per repository convention, not as a forward-looking
> TODO list.
>
> ```
> Milestone 3J2A
> IMPLEMENTED
> HARDENING ROUND 4 COMPLETE
> INDEPENDENT ACTUAL CODE REVIEW APPROVED
> ```
>
> This is **not** closed, checkpointed, committed, tagged, or
> pushed.

**Goal:** Give IMECE a provider-neutral, asynchronous ACP Client Core
(`acp_runtime/`) that can launch one local-stdio ACP agent subprocess using
the official `agent-client-protocol` SDK, run exactly one prompt through it
with an **exact** caller-specified child environment, observe bounded
streaming updates, always deny permission requests, and deterministically
tear the process tree down — with no connection to `FixLoopRunner` or any
canonical `run_runtime` event yet.

**Architecture:** `AcpClientRuntime.run()` validates input, then
(`acp_runtime/stdio.py`) spawns the agent subprocess directly via
`asyncio.create_subprocess_exec` with the caller's `AcpLaunchSpec.env` as
the literal, unmerged child environment, and hands its stdin/stdout streams
to the official SDK's `acp.connect_to_agent(...)` so 100% of the JSON-RPC/
NDJSON protocol implementation stays SDK-owned. A private `_ImeceAcpClient`
implements only `session_update`/`request_permission`; a `_FatalSignal`
races the outstanding `prompt()` call against limit/sink/protocol failures
discovered inside those callbacks. One `finally` block owns best-effort
session close, connection teardown, and deterministic
`process_runtime.cleanup.terminate_process_tree` cleanup on every exit path.

**Tech Stack:** Python 3.12, `asyncio`, `agent-client-protocol` (`acp`)
0.12.1, `psutil` (via the existing `process_runtime` package), `pytest`.

---

## Task 1: Dependency + models/errors

**Files:**
- Modify: `requirements.txt`
- Create: `acp_runtime/__init__.py`
- Create: `acp_runtime/errors.py`
- Create: `acp_runtime/models.py`
- Test: `tests/test_acp_models.py`

- [x] **Step 1: Add the dependency**

```
agent-client-protocol>=0.12.1,<0.13
```
appended to `requirements.txt`. Installed and inspected (`pip show
agent-client-protocol` → `0.12.1`) before writing any code that depends on
its API shape, per the checkpoint's "freshly inspect the installed SDK"
rule.

- [x] **Step 2: Write the failing model/error tests**

`tests/test_acp_models.py` covers (see file for full matrix, 34 tests
total after hardening round 1): empty/NUL/relative `argv`, invalid/NUL
`env` keys or values, `env` excluded from `repr()`, caller-side and
through-`spec.env` immutability (`MappingProxyType`), relative/empty/
whitespace `cwd`/`prompt`, non-int/bool/zero/negative `AcpClientLimits`
fields, `AcpRunResult` field presence and frozen-ness, and
platform-conditional absolute-path semantics (`os.name == "nt"` gated).

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'acp_runtime'`

- [x] **Step 3: Implement errors.py**

```python
class AcpRuntimeError(Exception):
    """Base class for acp_runtime failures."""

class AcpInputError(AcpRuntimeError): ...
class AcpSpawnError(AcpRuntimeError): ...
class AcpProtocolError(AcpRuntimeError): ...
class AcpAuthenticationRequiredError(AcpRuntimeError): ...
class AcpLimitError(AcpRuntimeError): ...
class AcpTimeoutError(AcpRuntimeError): ...
class AcpEventSinkError(AcpRuntimeError): ...
class AcpCleanupError(AcpRuntimeError): ...
```
(full docstrings in `acp_runtime/errors.py`.)

- [x] **Step 4: Implement models.py**

`AcpLaunchSpec` (frozen, `slots=True`): `argv: tuple[str, ...]` validated
via `os.path.isabs(argv[0])` (current-platform semantics, not a hand-rolled
parser); `env: Mapping[str, str]` validated into a `MappingProxyType` over a
defensively-copied `dict` (genuinely immutable — mutation through `spec.env`
raises `TypeError`, not just defensive-copy-but-mutable). `AcpPromptRequest`
(frozen, `slots=True`): `cwd` validated the same way with `os.path.isabs`;
`prompt` non-empty/non-whitespace, excluded from `repr()`.
`AcpClientLimits`/`AcpRunResult` as specified in the design doc. Full source
in `acp_runtime/models.py`.

- [x] **Step 5: `acp_runtime/__init__.py` re-exports**

Re-exports the error hierarchy and the four model types only (no
`acp_runtime.stdio` re-export — see Task 9).

- [x] **Step 6: Run tests to verify pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py -q`
Result: `29 passed, 5 skipped` (skips are the `os.name == "nt"` Windows-only
cases, correctly inert on this Linux host).

---

## Task 2: Extract shared process-tree cleanup

**Files:**
- Create: `process_runtime/cleanup.py`
- Modify: `process_runtime/runner.py`
- Modify: `process_runtime/__init__.py`
- Test: `tests/test_acp_process_cleanup.py`

- [x] **Step 1: Write the failing cleanup regression tests**

`tests/test_acp_process_cleanup.py`: already-dead PID is harmless; root
process is terminated; a spawned descendant is also terminated; a survivor
(via a monkeypatched `psutil.wait_procs` that reports no dead processes)
raises `ProcessCleanupError`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_process_cleanup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'process_runtime.cleanup'`

- [x] **Step 2: Move `_terminate_tree` verbatim**

`process_runtime/cleanup.py` contains `terminate_process_tree(pid: int) -> None`,
byte-for-byte the same psutil tree-walk/terminate/kill/verify algorithm that
was previously the private `_terminate_tree` in `process_runtime/runner.py`
— no behavioral change.

- [x] **Step 3: Update the call site**

`process_runtime/runner.py` now does
`from process_runtime.cleanup import terminate_process_tree` and calls
`terminate_process_tree(process.pid)` where it used to call the removed
private helper. `process_runtime/__init__.py` additionally exports
`terminate_process_tree`.

- [x] **Step 4: Run tests to verify pass, and prove no ProcessRunner regression**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_process_cleanup.py tests/test_process_runtime.py tests/test_process_tool.py -q`
Result: `137 passed, 5 skipped` (combined with Task 1's suite; `ProcessRunner`
tests unmodified and green).

---

## Task 3: Transient events + fail-closed Client handlers

**Files:**
- Create: `acp_runtime/events.py`
- Modify: `acp_runtime/client.py` (created in this task; extended in later
  tasks)
- Test: `tests/test_acp_client.py` (permission-fencing subset)

- [x] **Step 1: Implement `acp_runtime/events.py`**

```python
@dataclass(frozen=True, slots=True)
class AcpSessionUpdateObserved:
    session_id: str
    update: Any
    serialized_chars: int

@dataclass(frozen=True, slots=True)
class AcpPermissionRequested:
    session_id: str
    tool_call_id: str
    title: str
    option_ids: Sequence[str]

@dataclass(frozen=True, slots=True)
class AcpPermissionResolved:
    session_id: str
    tool_call_id: str
    outcome: str

class AcpEventSink(Protocol):
    def emit(self, event: AcpRuntimeEvent) -> None: ...

class NullAcpEventSink:
    def emit(self, event: AcpRuntimeEvent) -> None:
        return None
```

- [x] **Step 2: Implement `_ImeceAcpClient` fail-closed handlers**

`session_update`/`request_permission` in `acp_runtime/client.py`, fenced on
`self.session_id` (a `None`-bound or foreign `session_id` fatal-signals
`AcpProtocolError` and never emits events/increments counters — hardened in
Task 9 to apply identically to `request_permission`, not just
`session_update`). `request_permission` always returns
`RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))`.

- [x] **Step 3: Run the permission/session-fencing tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k permission -q`
Result: 13 permission-related tests pass (fail-closed resolution, no
`options[0]` selection, `_meta` ignored, event ordering, count tracking,
foreign-session fencing, sink-failure ordering — see Task 9).

---

## Task 4: `AcpClientRuntime` success path

**Files:**
- Modify: `acp_runtime/client.py`
- Test: `tests/test_acp_client.py` (success-path subset)

- [x] **Step 1: Write the failing success-path tests**

`tests/test_acp_client.py::test_fresh_run_means_fresh_spawn`,
`test_protocol_version_used`, `test_client_capabilities_none_passed_through`,
`test_new_session_uses_exact_absolute_cwd`, `test_mcp_servers_empty_list`,
`test_exact_prompt_passed_as_one_text_block`,
`test_session_id_and_stop_reason_propagated` — driven through an injected
`_connect` constructor seam (`_make_connect`/`_FakeConnection`), not a real
subprocess.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "success or protocol_version or capabilities or new_session or prompt" -v`
Expected: FAIL (`AcpClientRuntime` not yet implemented)

- [x] **Step 2: Implement `AcpClientRuntime.run` success path**

Input validation (type checks, `event_sink.emit` callable, prompt length,
`cwd` existence) → `self._connect(client, launch.argv, launch.env, request.cwd)`
→ `conn.initialize(protocol_version=acp.PROTOCOL_VERSION, client_capabilities=None)`
→ `conn.new_session(cwd=request.cwd, mcp_servers=[])` →
`conn.prompt(session_id, [acp.text_block(request.prompt)])` → build
`AcpRunResult`.

- [x] **Step 3: Run tests to verify pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -q`
Result: passes as part of the full 68-test file (see Task 9 for final
count after hardening).

---

## Task 5: Bounded updates + event-sink failure abort

**Files:**
- Modify: `acp_runtime/client.py`
- Test: `tests/test_acp_client.py` (update-bounds subset)

- [x] **Step 1: Write the failing bounds tests**

`test_update_event_emitted`, `test_update_count_tracked`,
`test_per_update_size_limit_aborts`, `test_update_count_limit_aborts`,
`test_total_update_chars_limit_aborts`, `test_limit_breach_sends_cancel`,
`test_foreign_session_update_aborts`, `test_event_sink_exception_aborts`,
`test_no_transcript_accumulation`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "update or limit or transcript" -v`
Expected: FAIL (bounds/fatal-signal logic not yet implemented)

- [x] **Step 2: Implement `_FatalSignal` + bounded `session_update`**

```python
class _FatalSignal:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.error: AcpRuntimeError | None = None

    def trigger(self, error: AcpRuntimeError) -> None:
        if self.error is None:
            self.error = error
        self.event.set()
```
`session_update` measures `update.model_dump_json()`, checks per-update size
→ count → total-size in order, fatal-signals `AcpLimitError` on breach, and
emits `AcpSessionUpdateObserved` (via `_emit`, which fatal-signals
`AcpEventSinkError` on sink failure) otherwise. `_run_prompt` races
`conn.prompt(...)` against `fatal.event.wait()` via
`asyncio.wait(..., return_when=FIRST_COMPLETED)`, cancels + settles the
prompt on fatal, and raises the stored error.

- [x] **Step 3: Run tests to verify pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "update or limit or transcript" -q`
Result: all pass.

---

## Task 6: Auth/protocol failures + timeout/cancel

**Files:**
- Modify: `acp_runtime/client.py`
- Test: `tests/test_acp_client.py` (auth/timeout subset)

- [x] **Step 1: Write the failing auth/timeout tests**

`test_auth_required_maps_to_specific_error`,
`test_other_request_error_maps_to_protocol_error`,
`test_no_auth_retry_no_authenticate_call`,
`test_prompt_timeout_sends_cancel_and_raises`, `test_cancel_wait_is_bounded`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "auth or timeout or cancel" -v`
Expected: FAIL (mapping/timeout logic not yet implemented)

- [x] **Step 2: Implement `_map_request_error` + timeout branch**

```python
def _map_request_error(exc: "acp.RequestError") -> AcpRuntimeError:
    if exc.code == -32000:  # acp.RequestError.auth_required()'s own code
        return AcpAuthenticationRequiredError(str(exc))
    return AcpProtocolError(str(exc))
```
`_run_prompt`: if `prompt_task not in done` after the bounded
`asyncio.wait(...)`, cancel + settle, raise `AcpTimeoutError`. No retry
anywhere; no `authenticate()` call anywhere in `acp_runtime`.

- [x] **Step 3: Run tests to verify pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "auth or timeout or cancel" -q`
Result: all pass.

---

## Task 7: Session close + deterministic cleanup

**Files:**
- Modify: `acp_runtime/client.py`
- Test: `tests/test_acp_client.py` (session-close/cleanup subset)

- [x] **Step 1: Write the failing close/cleanup tests**

`test_close_not_called_when_capability_absent`,
`test_close_attempted_when_capability_present`,
`test_close_failure_does_not_falsify_successful_result`,
`test_cleanup_called_on_success`, `test_cleanup_called_on_protocol_failure`,
`test_cleanup_survivor_raises_acp_cleanup_error`,
`test_cleanup_survivor_after_primary_failure_chains_cause`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "close or cleanup" -v`
Expected: FAIL (close/cleanup wiring not yet implemented)

- [x] **Step 2: Implement capability-gated close + hard cleanup**

`session_close_supported = session_capabilities is not None and session_capabilities.close is not None`
(never guessed from method presence). Best-effort
`asyncio.wait_for(conn.close_session(session_id), timeout=...)`. Hard
cleanup via `await asyncio.to_thread(terminate_process_tree, process.pid)`;
survivor → `AcpCleanupError` chained via
`raise AcpCleanupError(...) from (primary_exc or cleanup_exc)`.

- [x] **Step 3: Run tests to verify pass**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k "close or cleanup" -q`
Result: all pass.

---

## Task 8: Real fake-agent subprocess integration

**Files:**
- Create: `tests/fixtures/acp_fake_agent.py`
- Test: `tests/test_acp_fake_agent_integration.py`

- [x] **Step 1: Build the fake agent on the official SDK**

`tests/fixtures/acp_fake_agent.py` implements `initialize`/`new_session`/
`close_session`/`cancel`/`prompt` on top of `acp.Agent`/`acp.run_agent`, mode
selected by `sys.argv[1]`: `echo`, `permission` (issues one real
`session/request_permission` with `allow_once`/`allow_always` options),
`hang` (never resolves `prompt`), `child_process` (spawns one long-lived
child, writes its PID to `$ACP_FAKE_AGENT_CHILD_PID_FILE`), `many_updates`
(`$ACP_FAKE_AGENT_UPDATE_COUNT` chunks), `env_probe` (reports one env var,
named by `$ACP_FAKE_AGENT_PROBE_VAR` default `LOGNAME`, to
`$ACP_FAKE_AGENT_SENTINEL_FILE` — added in Task 9's hardening). No
hand-written JSON-RPC anywhere in the fixture.

- [x] **Step 2: Write and run the integration tests**

`tests/test_acp_fake_agent_integration.py`: echo end-to-end;
permission-mode fail-closed proof over the real protocol; hang-mode timeout
+ root-PID-gone proof (`psutil`); child_process-mode descendant-cleanup
proof; a capability-advertisement spy (via the `_connect` seam, updated in
Task 9 to wrap `spawn_acp_agent_connection` instead of the removed
`spawn_agent_process`); a many-updates no-transcript-accumulation proof.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_fake_agent_integration.py -q`
Result: `8 passed` (6 original + 2 exact-env tests added in Task 9).

---

## Task 9: Hardening round 1

An independent review against the approved design and the installed SDK
found nine defects before approval. This task fixes all of them without
broadening into 3J2B scope.

**Files:**
- Create: `acp_runtime/stdio.py`
- Modify: `acp_runtime/client.py`
- Modify: `acp_runtime/models.py`
- Modify: `tests/test_acp_client.py` (rewritten `_connect` seam + ~25 new tests)
- Modify: `tests/test_acp_models.py` (immutability + platform-path tests)
- Modify: `tests/test_acp_fake_agent_integration.py` (capability-spy rewrite
  + 2 new exact-env tests)
- Modify: `tests/fixtures/acp_fake_agent.py` (`env_probe` mode)
- Modify: `docs/superpowers/specs/2026-08-29-acp-client-core-design.md`

- [x] **Step 1: Exact child environment (BLOCKER)**

`acp.spawn_agent_process` unconditionally merges a curated host-env subset
on top of the caller's `env` — violating `AcpLaunchSpec.env == COMPLETE
child environment`. Fixed by creating `acp_runtime/stdio.py`:

```python
async def spawn_acp_agent_connection(
    client: Any, argv: tuple[str, ...], env: Mapping[str, str], cwd: str,
) -> tuple[Any, "asyncio.subprocess.Process"]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=dict(env), cwd=cwd,
    )
    assert process.stdin is not None and process.stdout is not None
    conn = acp.connect_to_agent(client, process.stdin, process.stdout)
    return conn, process
```
`AcpClientRuntime.__init__` now takes `_connect` (default
`spawn_acp_agent_connection`) instead of `_spawn_agent_process`. Not
exported from `acp_runtime/__init__.py`.

Test: `tests/test_acp_fake_agent_integration.py::test_exact_child_environment_host_var_not_inherited_unless_supplied`
and `::test_exact_child_environment_supplied_value_reaches_child_exactly` —
real subprocess + real SDK connection, would FAIL against the old
`spawn_agent_process`-based implementation.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_fake_agent_integration.py -k exact_child_environment -q`
Result: `2 passed`.

- [x] **Step 2: Immutable env (BLOCKER)**

`acp_runtime/models.py: _validate_env` now returns
`MappingProxyType(normalized)` instead of a plain `dict`.

Test: `tests/test_acp_models.py::test_env_is_read_only_mapping_proxy`,
`::test_mutation_through_spec_env_raises`,
`::test_dict_of_spec_env_still_works_for_subprocess_launch`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py -k env -q`
Result: `9 passed`.

- [x] **Step 3: Platform-correct absolute paths**

Replaced the custom `startswith("/")`/`value[1] == ":"` heuristic with
`os.path.isabs(...)` in both `AcpLaunchSpec.argv[0]` and
`AcpPromptRequest.cwd` validation.

Test: `tests/test_acp_models.py::test_windows_drive_relative_argv0_rejected`,
`::test_windows_drive_rooted_argv0_accepted`,
`::test_windows_unc_argv0_accepted`,
`::test_posix_windows_style_path_not_treated_as_absolute`,
`::test_windows_drive_relative_cwd_rejected`,
`::test_windows_drive_rooted_cwd_accepted` (Windows cases
`@pytest.mark.skipif(os.name != "nt", ...)`, correctly skipped on this
Linux host; the POSIX case runs unconditionally here).

- [x] **Step 4: Foreign permission session fence (BLOCKER)**

`request_permission` now fences on `self.session_id` identically to
`session_update`: a `None`-bound or foreign `session_id` fatal-signals
`AcpProtocolError` and returns the cancelled response without emitting any
permission event or incrementing `permission_request_count`.

Test: `tests/test_acp_client.py::test_foreign_session_permission_request_aborts_without_events`.

- [x] **Step 5: Permission event-sink failure ordering**

`request_permission` emits `AcpPermissionRequested` first; if that `_emit`
call fails, `permission_request_count` is **not** incremented and
`AcpPermissionResolved` is **never attempted**.

Test: `tests/test_acp_client.py::test_permission_sink_failure_on_first_event_prevents_second`
(asserts exactly one sink call happened),
`::test_permission_count_not_incremented_when_request_event_sink_fails`.

- [x] **Step 6: Synchronous event-sink contract**

```python
if inspect.iscoroutinefunction(emit):
    raise AcpInputError("AcpClientRuntime.run event_sink.emit must be synchronous, not a coroutine function.")
```
checked before any process spawn.

Test: `tests/test_acp_client.py::test_async_emit_event_sink_rejected_before_spawn`.

- [x] **Step 7: Fatal-signal race (BLOCKER)**

`_run_prompt` now checks `fatal.error is not None` directly after
`asyncio.wait()` returns (the authoritative, synchronously-written state),
instead of relying on `fatal_task in done` membership, which could lag one
scheduler tick behind a simultaneously-successful `prompt_task`.

Test: `tests/test_acp_client.py::test_simultaneous_prompt_success_and_fatal_state_always_returns_fatal` —
deterministic construction (fatal triggered synchronously inside the same
tick the fake prompt returns success), not a timing-sensitive sleep.

- [x] **Step 8: Preserve wrapped causes**

`_emit` and the update-serialization-failure path now do
`error.__cause__ = exc` explicitly before calling `fatal.trigger(error)`
(since these errors are stored and raised later, not raised immediately, so
`raise ... from exc` isn't available at the point of discovery).

Test: `tests/test_acp_client.py::test_permission_sink_failure_preserves_cause`,
`::test_event_sink_exception_preserves_cause`.

- [x] **Step 9: All post-spawn paths clean the process tree; generic
  failure mapping; cancellation; session-close-on-abort; cleanup
  precedence; connection teardown**

Restructured `AcpClientRuntime.run` around one `try/finally`: the `finally`
unconditionally runs best-effort `close_session` (if a session exists and
the capability was advertised) → bounded `conn.close()` (mapped to
`AcpProtocolError` only if no earlier primary failure exists) → hard
`terminate_process_tree`. A new `_call_agent` helper wraps `initialize`/
`new_session` so unexpected (non-`RequestError`) exceptions also map to
`AcpProtocolError` with `__cause__` preserved, while `asyncio.CancelledError`
is explicitly re-raised, never converted. Because none of the `finally`
block's `except` clauses catch `CancelledError`, caller-side cancellation
still runs the full teardown sequence before propagating unmodified (unless
cleanup itself then fails, in which case `AcpCleanupError` legitimately
takes precedence per the existing precedence rule).

Test: `tests/test_acp_client.py::test_unexpected_initialize_exception_maps_to_protocol_error_and_cleans_up`,
`::test_unexpected_new_session_exception_maps_to_protocol_error_and_cleans_up`,
`::test_unexpected_prompt_exception_maps_to_protocol_error_and_cleans_up`,
`::test_os_spawn_error_maps_to_acp_spawn_error`,
`::test_spawn_failure_does_not_attempt_cleanup`,
`::test_caller_cancellation_still_cleans_up_process_tree`,
`::test_timeout_after_session_creation_attempts_capability_gated_close`,
`::test_fatal_update_after_session_creation_attempts_capability_gated_close`,
`::test_no_close_attempt_when_no_session_created`,
`::test_connection_close_always_attempted_on_success`,
`::test_connection_close_failure_still_runs_hard_cleanup`,
`::test_connection_close_failure_maps_to_protocol_error_when_no_primary_failure`,
`::test_connection_close_failure_does_not_mask_earlier_primary_failure`,
`::test_cleanup_survivor_alone_preserves_process_cleanup_error_cause`.

- [x] **Step 10: Run full focused + regression + broad suites**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py tests/test_acp_process_cleanup.py tests/test_acp_client.py tests/test_acp_fake_agent_integration.py tests/test_process_runtime.py tests/test_process_tool.py -q`
Result: `137 passed, 5 skipped`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests --ignore=tests/test_keys.py -q`
Result: `1230 passed, 9 skipped` (established `test_keys.py` exclusion plus 5
new Windows-only skips; no new exclusion invented).

---

## Task 10: Hardening round 2 (three independently-found blockers)

An independent full-diff review found three blockers in the hardening
round 1 implementation, all in `AcpClientRuntime`'s teardown lifecycle.

**Files:**
- Create: `tests/fixtures/acp_fake_agent.py` (modified: `child_process` mode
  grandchild now redirects `stdin`/`stdout`/`stderr` to `DEVNULL`)
- Modify: `process_runtime/cleanup.py`
- Modify: `acp_runtime/stdio.py`
- Modify: `acp_runtime/client.py`
- Test: `tests/test_acp_process_cleanup.py`, `tests/test_acp_client.py`,
  `tests/test_acp_fake_agent_integration.py`

- [x] **Step 1: Write the failing snapshot regression tests (blocker 1)**

`tests/test_acp_process_cleanup.py::test_capture_process_tree_on_dead_pid_returns_empty_snapshot`,
`::test_capture_process_tree_includes_root_and_descendant`,
`::test_terminate_process_tree_with_no_snapshot_preserves_old_behavior`,
`::test_snapshot_catches_descendant_reparented_after_root_exit` — the last
one reproduces the exact blocker-1 race at the `process_runtime.cleanup`
level: capture a snapshot while a child process is still alive, let its
parent exit (orphaning it), prove a fresh rescan from the dead parent pid
finds nothing, then prove the pre-capture snapshot still reaches and kills
the orphan.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_process_cleanup.py -k "snapshot or capture_process_tree" -v`
Expected: FAIL with `ImportError: cannot import name 'ProcessTreeSnapshot'`

- [x] **Step 2: Implement `ProcessTreeSnapshot`/`capture_process_tree`, extend `terminate_process_tree`**

```python
@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshot:
    root: "psutil.Process | None"
    descendants: tuple["psutil.Process", ...]

def capture_process_tree(pid: int) -> ProcessTreeSnapshot: ...

def terminate_process_tree(pid: int, *, snapshot: ProcessTreeSnapshot | None = None) -> None:
    # unions snapshot.root/descendants with a fresh rescan from pid;
    # de-duplicated by (pid, create_time) identity; no-snapshot behavior
    # is byte-identical to the pre-round-2 algorithm.
    ...
```

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_process_cleanup.py tests/test_process_runtime.py tests/test_process_tool.py -q`
Result: `8 + 28 = 36` combined, all passed (no `ProcessRunner` regression).

- [x] **Step 3: Write the failing bounded-cancel regression tests (blocker 2)**

`tests/test_acp_client.py::test_hanging_cancel_send_does_not_block_teardown`
(a fake `conn.cancel()` that never returns) and
`::test_prompt_requiring_outer_close_does_not_deadlock_teardown` (a fake
prompt coroutine that survives local task cancellation and can only finish
once a fake `conn.close()` sets a release event).

Run (against the pre-round-2 `client.py`, wrapped in a hard 20s bash
timeout since a true hang cannot be waited out): both tests hang and are
killed by the timeout (exit 143) rather than completing — confirmed genuine
deadlocks, not assertion failures.

- [x] **Step 4: Bound `_cancel_and_settle`; move task ownership to a
  run-local `owned_tasks` set drained after outer teardown**

`_cancel_and_settle` bounds both the `conn.cancel(...)` send and the
voluntary settle-wait by `cancel_grace_ms` independently, and never awaits
`prompt_task` again itself after that. `prompt_task`/`fatal_task` are added
to a run-local `owned_tasks: set[asyncio.Task]` the moment they're created;
`AcpClientRuntime.run`'s teardown `finally` drains that set in one final
bounded step (`_drain_owned_tasks`, bounded by `cancel_grace_ms`) **after**
connection close and hard process cleanup. A survivor even after that final
drain raises `AcpCleanupError` rather than being silently dropped.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -q`
Result: `70 passed` (both new tests pass in ~1.5s combined, no hang).

- [x] **Step 5: Write the failing post-spawn-ownership regression test (blocker 3)**

`tests/test_acp_fake_agent_integration.py::test_connection_construction_failure_rolls_back_real_process`
— monkeypatches only `acp.connect_to_agent` to raise a sentinel exception,
using a real `create_subprocess_exec`-spawned process (tagged with a unique
argv marker so the test can scan `psutil.process_iter` for it afterward).

Run: FAIL — the raw sentinel exception propagates uncaught out of
`AcpClientRuntime.run` (not even mapped to a typed error), and Python logs
`Exception ignored in: BaseSubprocessTransport.__del__ ... RuntimeError:
Event loop is closed` — direct evidence the subprocess transport was
garbage-collected without ever being properly torn down (the process
leaked).

- [x] **Step 6: `spawn_acp_agent_connection` owns rollback until handoff succeeds**

```python
process = await asyncio.create_subprocess_exec(...)
try:
    if process.stdin is None or process.stdout is None:
        raise AcpProtocolError(...)
    conn = acp.connect_to_agent(client, process.stdin, process.stdout)
except asyncio.CancelledError:
    # roll back, then re-raise unchanged (unless rollback itself fails)
    ...
except Exception as exc:
    # roll back, then raise AcpProtocolError (cause preserved), or
    # AcpCleanupError if rollback itself fails
    ...
return conn, process
```

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_fake_agent_integration.py::test_connection_construction_failure_rolls_back_real_process -v`
Result: `1 passed`.

- [x] **Step 7: Write the failing real-process blocker-1 integration test,
  discover and fix a related latent gap, then get to green**

`tests/test_acp_fake_agent_integration.py::test_snapshot_before_close_catches_descendant_orphaned_by_root_exit`
uses the `child_process` fake-agent mode and forces the real agent root
process to fully exit (via the real production close path plus an explicit
`process.wait()`) *before* hard cleanup runs. Getting this test to green
surfaced a real latent gap: `acp.connect_to_agent`'s `Connection.close()`
never actually closes `process.stdin` (inspected directly in the installed
SDK — see design doc "Connection teardown now closes stdin"), so no
gracefully-behaved agent could ever exit on its own; every run always ended
in a hard kill. Fixed by adding `acp_runtime/stdio.py: close_acp_agent_connection(conn, process)`
(SDK `conn.close()` + explicit stdin EOF/close), used by `AcpClientRuntime.run`'s
teardown instead of `conn.close()` directly. Getting the test's own
`process.wait()` to resolve then surfaced a second, test-fixture-only bug:
`tests/fixtures/acp_fake_agent.py`'s `child_process` mode spawned its
grandchild without redirecting `stdin`/`stdout`/`stderr`, so the grandchild
inherited a duplicate write-end of the agent's own stdout pipe — keeping
that pipe open forever from the OS's perspective and preventing asyncio's
own `Process.wait()` from ever resolving (traced directly into
`asyncio.base_subprocess.BaseSubprocessTransport._try_finish`, which
requires all pipe transports to report `disconnected` before resolving
`_exit_waiters`). Fixed by redirecting the grandchild's stdio to `DEVNULL`
in the fixture (standard practice for a detached background process
regardless).

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_fake_agent_integration.py -v`
Result: `10 passed` (all real-subprocess integration tests, including both
new hardening-round-2 tests).

- [x] **Step 8: Full regression + broad + static verification**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py tests/test_acp_process_cleanup.py tests/test_acp_client.py tests/test_acp_fake_agent_integration.py tests/test_process_runtime.py tests/test_process_tool.py -q`
Result: `145 passed, 5 skipped`.

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests --ignore=tests/test_keys.py -q`
Result: `1238 passed, 9 skipped` (same established exclusion; no new one
invented).

Run: `python3 -m py_compile acp_runtime/*.py process_runtime/*.py tests/test_acp_models.py tests/test_acp_process_cleanup.py tests/test_acp_client.py tests/test_acp_fake_agent_integration.py tests/fixtures/acp_fake_agent.py`
Result: clean.

Run: `git diff --check HEAD` (scoped to this milestone's files — a
pre-existing, not-mine `imece_3j2a_h1_full_diff.txt` review artifact in the
repo root is excluded)
Result: clean.

---

## No Placeholders / Self-Review

- **Spec coverage**: every numbered item in the hardening-round-1 request
  (sections 1–19) maps to a step in Task 9, and every blocker in the
  hardening-round-2 request maps to a step in Task 10, each with the exact
  test(s) that prove it.
- **Type consistency**: `AcpClientRuntime.__init__(self, *, _connect=None, _terminate_process_tree=None, _capture_process_tree=None)`
  is the one constructor signature used consistently from Task 10 onward
  (the earlier `_spawn_agent_process` seam name from Tasks 4–8 was fully
  replaced in Task 9, not left dangling — `grep -rn "_spawn_agent_process" acp_runtime/ tests/`
  returns nothing).
- **No dangling references**: `acp.spawn_agent_process` no longer appears
  anywhere in `acp_runtime/client.py` (`tests/test_acp_client.py::test_client_module_never_calls_spawn_agent_process`
  asserts this by source inspection). `terminate_process_tree`'s no-snapshot
  call sites in `process_runtime/runner.py` were not touched by Task 10 (its
  signature change is purely additive via a keyword-only default).
