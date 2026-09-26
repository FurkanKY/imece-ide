# CLI Worker Fallback Design

> **Deferred.** This milestone (3J3) is not currently being implemented. The
> ACP Worker already covers account-based CLI agents (Claude Code, Codex,
> Gemini CLI) for now. Revisit this design only if a needed CLI agent lacks
> ACP support.

## Status and purpose

This document specifies Milestone 3J3: the third concrete
`fix_runtime.ports.WorkerAttemptRunner` implementation, alongside the
already-complete Native Worker (`executor_runtime/native_worker.py`) and ACP
Worker (`executor_runtime/acp_worker.py`, checkpointed at tag
`milestone-3j2b`, commit `cbfe2f072d8ef09825c2ef0e3efad55d413d2d75`). Its only
responsibility is to answer **how** one fresh Fix Worker attempt executes
through an already-configured, non-interactive, headless command-line agent
binary. It does not decide **which** executor to select for a given Run —
that is exclusively 3K's `RoutingPolicy`.

```
orchestrator = who / when   (FixLoopRunner — unchanged)
executor     = how          (CliWorkerAttemptAdapter — this milestone)
workspace    = where        (GitWorktreeWorkspace — unchanged)
```

This document is design only. No production code, no test code, and no
milestone document other than this one is created or modified by this
milestone. Section headers below map directly onto the 22 design questions
this document is required to settle explicitly (see the end of each section
for a "Resolves design question N" marker).

## Scope

3J3 includes:

- `CliWorkerLaunchProfile` — a provider-neutral, immutable launch
  configuration (command, args, env, timeout).
- `resolve_cli_worker_launch(profile)` — one-time, pre-side-effect
  executable resolution.
- `CliWorkerAttemptAdapter` — the concrete `WorkerAttemptRunner`
  implementation.
- A narrow, additive `ProcessRequest.stdin_text` capability in
  `process_runtime`, plus the corresponding `ProcessRunner` stdin-writer
  thread.
- `run_runtime/cli.py: CanonicalCliEventSink` — a new, independent canonical
  adapter surface (not a reuse of `CanonicalAcpEventSink`).
- Overlap-safe, whole-text/whole-message secret redaction for CLI stdout,
  stderr, and failure diagnostics, duplicated in shape from the
  already-approved ACP policy.
- A real, provider-neutral fake CLI fixture and integration tests proving
  stdin delivery, workspace isolation, freshness, non-zero exit, and
  timeout behavior over a real subprocess.

3J3 excludes: `RoutingPolicy`, automatic ACP→CLI fallback decision logic,
provider preference/ordering, any Codex/Claude/Gemini/Qwen/OpenCode-specific
parsing or presets, CLI token/cost extraction, provider authentication
setup, UI configuration, a CLI discovery catalog, retries, resume/session
semantics, remote execution, interactive terminal/PTY behavior, `shell=True`,
and any modification to `FixLoopRunner`, `fix_runtime.ports`, the ACP Client
Core, or the ACP Worker adapter. 3K owns executor selection/routing above
this adapter; nothing in this design anticipates or hard-codes a routing
decision.

## Why the legacy `adapters.py` CLI wrappers are not reused

`adapters.py` (`call_claude`, `call_gemini_cli`, `call_codex_cli`,
`call_qwen_code`, `_run_cli`) belongs to the pre-`fix_runtime` legacy
`LLMResponse`/`PROVIDERS` architecture described in `docs/ARCHITECTURE.md`
Layer 1. Concretely, inspecting `adapters.py` directly shows it:

- calls `subprocess.run(argv, capture_output=True, text=True, timeout=600)`
  directly — no `ProcessRuntime` hardening (no bounded capture with a fixed
  byte ceiling, no process-tree cleanup on timeout, no workspace-relative
  cwd enforcement, no safe-environment allowlist);
- delivers the prompt by **string-formatting it into the CLI argument**
  (`_run_cli([cli, "-p", full_prompt, "-o", "json"])`,
  `_run_cli([cli, "exec", "--json", full_prompt])`) — exactly the argv
  transport this milestone's hard invariant (see next section) forbids;
- hard-codes provider-specific JSON parsing (`call_codex_cli` parses
  `item.type == "agent_message"`, `call_gemini_cli`-style parses
  `data.get("stats", {}).get("models")`) — exactly the provider-specific
  knowledge Section "Provider-neutral core" forbids;
- has no concept of `GitWorktreeWorkspace`, canonical `RunEvent` history, a
  stable `execution_id`, or a typed executor failure hierarchy — it predates
  all of those contracts entirely.

None of `adapters.py`'s code, shapes, or conventions are reused. 3J3 is
written from scratch against the current `WorkerAttemptRunner` contract, the
existing `process_runtime` primitives, and the existing canonical-event
conventions established by the Native and ACP adapters.

## Dependency direction

```
fix_runtime
    knows WorkerAttemptRunner (fix_runtime.ports) only

executor_runtime.cli_worker
    depends on: fix_runtime.models/ports, process_runtime, run_runtime.cli,
                run_runtime.service, workspace.worktree

run_runtime.cli
    depends on: process_runtime.models (ProcessResult facts only),
                run_runtime.events/models/service

process_runtime
    depends on: nothing above it (workspace only, as today).
    MUST NOT import executor_runtime, run_runtime, fix_runtime, or acp_runtime.

tool_runtime.tools.process
    unchanged: still constructs ProcessRequest with no stdin_text.
```

`process_runtime/models.py` and `process_runtime/runner.py` gain the new
`stdin_text` capability, but neither file gains a new import from any layer
above `process_runtime`. `run_runtime/cli.py` never imports
`executor_runtime` or `fix_runtime` (mirrors `run_runtime/acp.py` exactly:
it knows `ProcessResult`, not `FixWorkerRequest`). This preserves the same
layering the 3J2B design document established for ACP and required no
change to verify.

## Hard invariant: prompt transport is stdin, never argv

`FixWorkerRequest.rendered_input` (up to `fix_runtime.prompt.MAX_FIX_INPUT_CHARS`
= 48,000 characters, verified directly in `fix_runtime/prompt.py`) is
delivered to the CLI process **verbatim**, through the child's stdin, and
through no other channel. It is never prefixed, suffixed, reconstructed,
derived from `request.task`, placed in `argv`, placed in an environment
variable, or written to a temporary workspace file.

This is not a style preference; it is structurally forced by existing
`process_runtime` bounds inspected directly in `process_runtime/models.py`:
a single `ProcessRequest.argv` element is capped at `MAX_ARGUMENT_LENGTH` =
16,384 characters — under half of the worst-case rendered input — so argv
transport would silently truncate or hard-reject a legitimate large fix
attempt. Environment variables are unsuitable for the same reason plus
`MAX_ENV_VALUE_LENGTH` = 16,384, and additionally because environment
content is far more likely to be echoed into shell history, `/proc`,
crash reporters, or child-of-child inheritance. A temporary workspace file
would require its own create/cleanup lifecycle, a new place secrets could
persist on disk after the run (or leak into the workspace worktree the
Verification/Reviewer stages also inspect), and a new race between "the
child has finished reading" and "we delete the file" that stdin does not
have.

## `CliWorkerLaunchProfile` — exact contract

*(Resolves design question 1.)*

```python
@dataclass(frozen=True, slots=True)
class CliWorkerLaunchProfile:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_ms: int = DEFAULT_TIMEOUT_MS   # process_runtime.models.DEFAULT_TIMEOUT_MS (120_000)
```

Validated in `__post_init__`, mirroring `AcpWorkerLaunchProfile`'s
already-approved shape field-for-field, with one addition (`timeout_ms`):

- `command`: non-empty `str`, NUL-free.
- `args`: rejected outright if it is itself a `str`/`bytes` (the same
  `AcpWorkerLaunchProfile` guard against a caller accidentally passing one
  string instead of a sequence); otherwise copied into a `tuple[str, ...]`;
  every element must be a non-empty, NUL-free `str`.
- `env`: must be a `Mapping`; copied into an immutable `MappingProxyType`.
  Every key must be a non-empty, NUL-free `str` **and must not contain
  `"="`** (subprocess creation on POSIX deterministically rejects such
  names — the same fix applied to `AcpWorkerLaunchProfile.env` during 3J2B
  Hardening Round 1 is applied here from the start, not retrofitted). Every
  value must be a NUL-free `str`; an empty value is valid. `env` is the
  profile's override layer; see "Environment semantics" below for how it
  composes with `ProcessRunner`'s existing safe-inherited-environment
  allowlist — it is **not** treated as an exact/exclusive environment the
  way `AcpWorkerLaunchProfile.env` is for ACP.
- `timeout_ms`: must satisfy the exact same bound `ProcessRequest.timeout_ms`
  already enforces (`process_runtime.models`: a positive `int`, not `bool`,
  `<= MAX_TIMEOUT_MS` = 600,000). This is a deliberate, redundant
  pre-side-effect check: it lets an invalid profile fail at
  `CliWorkerLaunchProfile` construction time (configuration time, long
  before any attempt), rather than only at `ProcessRequest` construction
  time inside a specific attempt.

`CliWorkerLaunchProfile` does not carry `stdin_text` — the profile is
reusable, static, per-adapter-instance configuration; `stdin_text` is
per-attempt data (`request.rendered_input`) supplied fresh on every
`CliWorkerAttemptAdapter.run()` call. No mutable state is ever attached to
the profile itself.

`resolve_cli_worker_launch(profile: CliWorkerLaunchProfile) -> tuple[str, tuple[str, ...]]`
mirrors `resolve_acp_worker_launch` exactly: if `profile.command` is
absolute, it is validated in place (`os.path.isabs` + `os.path.isfile` +
`os.access(path, os.X_OK)`); if relative, `shutil.which(profile.command)`
is called **exactly once**, and the discovered path is validated the same
way. Any failure raises `ExecutorAdapterInputError`. The function returns
`(absolute_executable, profile.args)` — not a full `ProcessRequest`, because
`ProcessRequest` additionally needs the per-attempt `cwd` and `stdin_text`
that the launch profile does not own. This split keeps `resolve_cli_worker_launch`
independently unit-testable exactly like `resolve_acp_worker_launch` is
today. *(Resolves design question 8: executable resolution happens inside
`CliWorkerAttemptAdapter.run()`, before `sink.start()`/`execution.started`,
using the same one-shot `shutil.which` + absolute-path-validation shape
already approved for ACP.)*

## `ProcessRequest.stdin_text` — exact contract

*(Resolves design question 2.)*

```python
MAX_STDIN_TEXT_CHARS = 64_000  # process_runtime/models.py

@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    env: Mapping[str, str] = field(default_factory=dict)
    stdin_text: str | None = field(default=None, repr=False)
```

`64_000` is chosen because it must exceed `fix_runtime.prompt.MAX_FIX_INPUT_CHARS`
(48,000, confirmed by direct inspection) with headroom, while remaining a
concrete, explicit, single named constant (no formula tying the two modules
together — `process_runtime` must not import `fix_runtime`). In today's
actual `FixWorkerRequest` flow, `rendered_input` can never exceed 48,000
characters, so this bound is deliberately never expected to fire for the
CLI Worker's real traffic; it exists as `process_runtime`'s own independent
safety net, exactly like `MAX_ARGUMENT_LENGTH` is independent of any
particular caller.

Validation added to `ProcessRequest.__post_init__`, evaluated **before**
`ProcessRunner` ever spawns a process (dataclass construction always
precedes any call to `ProcessRunner.run()`):

- `None` is valid and preserves today's exact behavior (see "ProcessRunner
  stdin implementation" below) — this is the only value every existing
  caller (`tool_runtime.tools.process._request()`, every existing
  `ProcessRequest(...)` call site, every existing test) supplies, since
  none of them pass `stdin_text` at all.
- If not `None`: must be `str`; must not contain `"\x00"`; must satisfy
  `len(stdin_text) <= MAX_STDIN_TEXT_CHARS`. Any violation raises
  `ProcessInputError` — the same exception type every other `ProcessRequest`
  field violation already raises, so `CliWorkerAttemptAdapter` translates it
  as ordinary local-input rejection exactly like it does for
  `AcpInputError`/`ProcessInputError` today. **No truncation is ever
  performed** — an oversized `stdin_text` is a hard input rejection, not a
  silently-shortened prompt.
- `repr=False`, matching `AcpPromptRequest.prompt`'s existing precedent —
  the prompt body must never appear in an object repr, log line, or
  traceback formatting of the request itself.
- `stdin_text` is immutable exactly like every other `ProcessRequest`
  field: it is a plain `str` (strings are already immutable in Python), so
  no defensive copy is needed beyond the existing frozen-dataclass
  discipline.

## `ProcessRequest.permission_resource()` — fail-closed rule

*(Resolves design question 6.)*

The user/agent-facing `run_process` tool's schema (`tool_runtime/tools/process.py:
RUN_PROCESS_SCHEMA`) is **not** modified — it keeps exactly `argv`, `cwd`,
`timeout_ms`, `env`, with `additionalProperties: False`. Nothing in this
milestone adds a `stdin` field to that schema, and
`tool_runtime.tools.process._request()` never constructs a `ProcessRequest`
with `stdin_text` set — confirmed by direct inspection: it always calls
`ProcessRequest(argv=..., cwd=..., timeout_ms=..., env=...)` with no fifth
argument. `RunProcessExecutor`/`register_process_tool` therefore require no
change at all, and their existing tests continue to exercise the exact same
code path unmodified.

The fail-closed rule guards against `permission_resource()` itself becoming
a hidden secret leak or a silent misrepresentation if it is ever called on
a request that *does* carry `stdin_text` (defense in depth — not because
today's tool can produce one):

```python
def permission_resource(self) -> str:
    if self.stdin_text is not None:
        raise ProcessInputError(
            "ProcessRequest.permission_resource() refuses to serialize a "
            "request that carries stdin_text: a permission resource must "
            "fully represent what will execute, and silently omitting "
            "piped input would misrepresent it to the approval boundary."
        )
    ...  # unchanged existing json.dumps(...) body
```

This is deliberately fail-**closed**, not fail-silent: raising
`ProcessInputError` (not returning a resource string that quietly omits
`stdin_text`) means a future caller who mistakenly wires a stdin-bearing
`ProcessRequest` into any permission-gated tool path gets an immediate,
loud, typed failure instead of an approval prompt that misrepresents what
will actually run. No prompt content ever reaches a `PermissionRequest`
resource string, because the only production caller of
`permission_resource()` (`tool_runtime/tools/process.py:_permission`) is
never given a `stdin_text`-bearing request in the first place, and if that
ever changed, this guard fails the whole tool call rather than leaking.

## `ProcessRunner` stdin implementation

*(Resolves design questions 3, 4, 5.)*

`ProcessRunner.run()` keeps every existing capability unchanged: bounded
stdout/stderr capture via `BoundedCapture`, `request.timeout_ms`-bounded
`process.wait()`, existing `terminate_process_tree` cleanup on timeout,
`shell=False`, and workspace-relative cwd/executable resolution. The stdin
addition is purely conditional and additive:

```python
stdin = subprocess.PIPE if request.stdin_text is not None else subprocess.DEVNULL
```

When `request.stdin_text is None`, this is `subprocess.DEVNULL`, byte-for-byte
identical to today's hard-coded value — every existing `ProcessRunner`
caller and every existing `tests/test_process_runtime.py`/
`tests/test_process_tool.py` test continues down the exact same code path,
because none of them will ever set `stdin_text`.

When `request.stdin_text is not None`, the required shape is:

1. Spawn the process with `stdin=subprocess.PIPE` (stdout/stderr unchanged
   at `subprocess.PIPE`).
2. Start the existing stdout/stderr `BoundedCapture` drain threads exactly
   as today.
3. Start one additional, dedicated **stdin-writer thread**:

   ```python
   class _StdinWriteOutcome:
       __slots__ = ("error",)
       def __init__(self) -> None:
           self.error: BaseException | None = None

   def _write_stdin(stream, data: bytes, outcome: _StdinWriteOutcome) -> None:
       try:
           stream.write(data)
           stream.flush()
       except BaseException as exc:  # noqa: BLE001 - background thread; see below
           outcome.error = exc
       finally:
           try:
               stream.close()
           except Exception:
               pass
   ```

   The `except BaseException` here mirrors the *existing*,
   already-in-production pattern in `process_runtime/capture.py:
   BoundedCapture.consume` (`except BaseException as exc: self.error = exc`)
   verbatim, for the identical reason: this runs on a background thread,
   and CPython only ever delivers `KeyboardInterrupt` to the main thread, so
   this is not "catching and swallowing Ctrl-C" — there is no Ctrl-C to
   catch here. `SystemExit` raised from a background thread (which nothing
   in this code path does) would still only terminate that thread, not the
   process, so recording it as a stored outcome rather than letting it
   vanish silently is strictly safer.
4. `stdin_text` is encoded as UTF-8 exactly once
   (`request.stdin_text.encode("utf-8")`) before the writer thread starts —
   no re-encoding, no chunked re-derivation.
5. The writer thread performs exactly one `write()` call with the complete
   encoded payload (Python's `BufferedWriter.write` itself may perform
   multiple underlying OS writes for a large payload, but the *contract* is
   "write the exact bytes once," not "write in application-controlled
   chunks").
6. `stream.close()` runs in the writer thread's `finally` block on **every**
   outcome — success, a caught exception, or (implicitly) if the thread
   itself does not start (no dedicated thread means no dedicated behavior;
   see step 9 for the join-timeout case). Child stdin is always closed by
   this thread's own logic once its one write attempt is done, so the child
   sees EOF as soon as delivery finishes (or fails) — never left open
   indefinitely.
7. `process.wait(timeout=request.timeout_ms / 1000)` — unchanged from
   today. The writer thread runs **concurrently** with this wait; nothing
   about the wait call itself changes.
8. On `subprocess.TimeoutExpired`, the existing `terminate_process_tree`
   cleanup path is unchanged. If the writer thread was still blocked on
   `write()` (because the child never read enough to unblock the OS pipe
   buffer), killing the child causes that blocked write to unblock with
   `BrokenPipeError` shortly after, which the writer thread captures as
   `outcome.error` — it does not need any special handling from the
   timeout branch itself.
9. After the existing `try/except TimeoutExpired/finally` block (which
   already joins the stdout/stderr threads with `timeout=3`), the stdin
   writer thread (if one was started) is joined the same bounded way:
   `stdin_thread.join(timeout=3)`. The existing post-join liveness check
   (`if stdout_thread.is_alive() or stderr_thread.is_alive(): raise
   ProcessRuntimeError(...)`) is extended to also check the stdin thread.
   This keeps the "boundedly join all owned I/O threads" contract for all
   three threads uniformly, with no new unbounded wait introduced anywhere.
10. After the existing `stdout_capture.error`/`stderr_capture.error` checks,
    one more check is added: `if outcome.error is not None: raise
    ProcessRuntimeError("Process stdin delivery failed; the child may have "
    "closed stdin before the complete input was written.") from outcome.error`.
    This is evaluated **unconditionally** (whether or not the process timed
    out) — a stdin delivery failure is always meaningful evidence that the
    input contract was violated, so it takes precedence over building an
    otherwise-normal `ProcessResult`. Per the approved contract: **a
    configured CLI closing stdin before the complete prompt is delivered is
    an execution failure, not a successful Worker run** — this is exactly
    the mechanism that enforces that rule at the `process_runtime` layer.

No `communicate()` call is introduced anywhere — the manual
`Popen` + `BoundedCapture`-thread + (new) writer-thread pattern is kept
throughout, so `communicate()`'s own internal buffering never has a chance
to bypass `BoundedCapture`'s explicit bound. No new unbounded buffer is
created: the writer thread holds only the one pre-encoded `bytes` payload
(already bounded to `MAX_STDIN_TEXT_CHARS` characters by `ProcessRequest`),
never accumulates anything, and is discarded once the write attempt
finishes. The stdin payload is never included in any exception message
constructed inside `ProcessRunner`/`_write_stdin` — `ProcessRuntimeError`'s
message above is a fixed, static string; the *cause* chain carries the raw
`BrokenPipeError`/`OSError`, whose own message from the OS layer does not
contain the payload either (a broken-pipe error describes the I/O failure,
not the data that was being written).

**Known, accepted limitation** (documented here rather than silently
assumed away): if `stdin_text` is small enough to fit entirely within the
OS pipe's kernel buffer (commonly 64 KiB on Linux, comparable to
`MAX_STDIN_TEXT_CHARS`), `stream.write()` can return successfully purely
because the kernel buffered it, even if the child process never actually
reads any of it before exiting. In that narrow case, `outcome.error` stays
`None` and `ProcessRunner` proceeds to build a normal `ProcessResult`
reflecting whatever the (possibly prompt-blind) child actually did. This is
an inherent property of pipe-based stdin delivery, not something
`process_runtime` can fully close without out-of-band coordination from the
child itself (which a provider-neutral, non-interactive CLI contract
cannot assume). It does not create a correctness gap in the overall Fix
Loop: a CLI that ignored its input and produced an unrelated (or empty)
change is still caught downstream by Verification/Reviewer exactly as any
other incorrect Worker attempt would be — `exit_code == 0` from
`ProcessRunner` never means "the fix is correct" (see "Success contract"
below), only that the process itself terminated cleanly.

## Environment semantics

*(Resolves design question 7.)*

CLI and ACP intentionally differ here, by design, not by oversight. ACP's
`AcpLaunchSpec.env` is the *exact, complete* child environment (no host
merge at all — the 3J2A/3J2B invariant). A generic, already-installed,
already-authenticated headless CLI (the kind of tool this milestone
targets) is normally expected to run with an ordinary host shell-like
environment — it may need `PATH` to find its own dependencies, `HOME`/
`USERPROFILE` to find its own config/credentials store, `LANG`, `TEMP`,
etc. Forcing an ACP-style exact environment onto every CLI fallback would
make already-configured, already-authenticated CLIs fail in ways this
milestone has no way to diagnose or fix (CLI authentication setup is
explicitly out of scope).

Therefore `CliWorkerLaunchProfile.env` is **not** the exact child
environment. It is the same kind of override layer `run_process`'s own
`ProcessRequest.env` already is: `ProcessRunner._safe_environment()`
(unchanged) merges the profile's overrides on top of its existing curated
host-inherited allowlist (`_SAFE_ENV_KEYS` = `PATH, PATHEXT, SYSTEMROOT,
WINDIR, COMSPEC, TEMP, TMP, TMPDIR, HOME, USERPROFILE, USER, USERNAME, LANG,
VIRTUAL_ENV`, plus any `LC_*` variable — read directly from
`process_runtime/runner.py`). `CliWorkerLaunchProfile.env` flows into
`ProcessRequest.env` unchanged, so `ProcessRequest.__post_init__`'s existing
restriction (no override of `PATH`/`PATHEXT`, case-insensitively) applies
automatically — no new code is needed to preserve that restriction, since
the CLI adapter constructs a normal `ProcessRequest` and inherits all of
its existing guarantees. This is a deliberate reuse, not a duplication: the
CLI Worker's environment story is "be a slightly more locked-down
`run_process` call," while the ACP Worker's story is "own the entire
environment" — the two adapters are allowed, and expected, to differ here.

## Preflight order

*(Resolves design question 15, restated precisely for the concrete types
involved.)*

All of the following happen before any canonical event is written and
before any process is spawned. Every failure below raises
`ExecutorAdapterInputError` unless noted:

1. `isinstance(request, FixWorkerRequest)`.
2. `isinstance(workspace, GitWorktreeWorkspace)` (see "Workspace boundary").
3. `WorkerAttemptResult(execution_id=execution_id)` construction (validates
   the supplied ID against the existing stable-ID contract; catches
   `FixLoopInputError` and re-raises as `ExecutorAdapterInputError`, exactly
   like both existing adapters do).
4. `resolve_cli_worker_launch(self._launch_profile)` — absolute executable
   resolution (see above).
5. `workspace.root.is_dir()` — explicit pre-check even though
   `GitWorktreeWorkspace` instances are always backed by a real, live
   directory by construction; this mirrors ACP's explicit
   `os.path.isdir(cwd)` check and ensures a missing/disposed worktree is a
   *pre*-side-effect input rejection rather than a `ProcessSpawnError`
   surfacing after `execution.started` (`ProcessRunner`'s own `_cwd_path`
   check happens too, but only *after* the process side effect boundary,
   which is too late for this milestone's ordering contract).
6. `ProcessRequest` construction:
   `ProcessRequest(argv=(executable, *args), cwd=".", timeout_ms=profile.timeout_ms,
   env=profile.env, stdin_text=request.rendered_input)`. `cwd="."` is
   deliberate — it reuses `ProcessRunner`'s own existing, already-tested
   workspace-relative cwd resolution (`_cwd_path`: `relative_cwd == "."` →
   `workspace.root`) exactly the way `tool_runtime.tools.process._request()`
   already does for `run_process`, instead of reimplementing absolute-path
   validation the way the ACP adapter must (ACP has no `Workspace` concept
   at the `AcpPromptRequest` level; `ProcessRequest` already does). Any
   `ProcessInputError` here (including an oversized `stdin_text`, an
   invalid `timeout_ms`, or an invalid `env` entry) is caught and
   re-raised as `ExecutorAdapterInputError`.
7. (Bounds — timeout/env/stdin — are enforced as an inherent part of step 6;
   there is no separate manual bound-check in the adapter, matching the
   "single source of truth in the model's own `__post_init__`" pattern
   already used for `AcpPromptRequest`.)
8. Structural validation of the injected process runner: `callable(getattr(process_runner,
   "run", None))` (see "Structural injection").
9. `CanonicalCliEventSink(self._runtime, self._run_id, execution_id=execution_id)`
   construction — requires `RunStatus.RUNNING`; a `ValueError` here (mirrors
   `CanonicalAcpEventSink`/`CanonicalAgentEventSink` exactly) is caught and
   re-raised as `ExecutorAdapterInputError`, since no execution has begun
   and no side effect has occurred.
10. `sink.start(request.task)` — the first durable canonical write for this
    attempt (`execution.started`).
11. `self._process_runner.run(workspace, process_request)` — the one
    process side effect for this attempt.

No process spawns before step 11. No `execution.started` is ever recorded
for a deterministic local configuration failure caught in steps 1-9.

## Workspace boundary

*(Resolves the workspace requirement stated throughout Sections 7/19.)*

Exactly matches the Native and ACP Worker safety invariant:
`isinstance(workspace, GitWorktreeWorkspace)` is required; `LocalWorkspace`
and every other concrete `Workspace` are rejected with
`ExecutorAdapterInputError` before any canonical event. `ProcessRunner`
itself is workspace-type-agnostic (it only calls `workspace.root`), so this
gate lives entirely in `CliWorkerAttemptAdapter`, exactly where the
equivalent gate lives in `NativeWorkerAttemptAdapter`/`AcpWorkerAttemptAdapter`.
The CLI process's cwd is always the worktree root (`ProcessRequest(cwd=".")`,
resolved against the `GitWorktreeWorkspace` passed straight through to
`ProcessRunner.run(workspace, process_request)`); the actual mutation
boundary — the fact that the process's writes land in the shadow worktree
and never in the source checkout — is the pre-existing `GitWorktreeWorkspace`
guarantee itself, not a property this adapter re-derives. The real
integration test (see "Test strategy") proves this end to end with a real
subprocess mutating a real file, exactly the way
`tests/test_acp_worker_integration.py::test_real_acp_worker_mutates_shadow_worktree_not_source_repo`
already proves it for ACP.

## Structural injection

*(Resolves design question — matches the already-approved ACP shape.)*

```python
class _CliProcessRunner(Protocol):
    def run(self, workspace, request: ProcessRequest) -> ProcessResult: ...
```

`CliWorkerAttemptAdapter.__init__` requires
`callable(getattr(process_runner, "run", None))`, never
`isinstance(process_runner, ProcessRunner)` — a plain test double with a
compatible `run` method is a valid injected dependency, mirroring
`AcpWorkerAttemptAdapter`'s `_AcpClientRunner` Protocol exactly. All
per-attempt state (`ProcessRequest`, `CanonicalCliEventSink`, the
`ProcessResult`) is local to one `run()` call; the adapter itself stores
only immutable configuration (`_runtime`, `_run_id`, `_launch_profile`,
`_process_runner`) set once at construction and reused unchanged across
sequential calls.

## Canonical CLI event model

*(Resolves design questions 9, 10, 11, 14, 15 (atomicity), 16.)*

`run_runtime/cli.py` introduces `CanonicalCliEventSink`, a new,
independent module — **not** a subclass or reuse of
`CanonicalAcpEventSink`, and it never manufactures ACP or native semantic
events (no `model.*`, `turn.*`, `tool.*`, `usage.*`, `permission.*`,
`run.waiting_user`, `run.resumed`). Every event it writes carries:

```
source = "cli_worker"
execution_id = <the supplied execution_id>
correlation_id = <same execution_id>
```

Constructor: `CanonicalCliEventSink(runtime, run_id, *, execution_id)`,
requiring `RunStatus.RUNNING` and capturing `run.last_event_seq` as the
initial optimistic expected sequence — the exact same shape as
`CanonicalAcpEventSink`/`CanonicalAgentEventSink`, duplicated (not
imported) because it is a genuinely separate transport with a genuinely
separate settlement shape (see next paragraph). It exposes:

```python
def start(self, task: str) -> None: ...
def settle_success(self, result: ProcessResult, *, sanitized_stdout: str, sanitized_stderr: str) -> None: ...
def settle_failure(self, result: ProcessResult, *, sanitized_stdout: str, sanitized_stderr: str, error_type: str, message: str) -> None: ...
def fail_without_result(self, error: Exception, *, error_type: str, message: str) -> None: ...

@property
def persistence_error(self) -> Exception | None: ...
```

`start(task)` records exactly one durable `execution.started`:

```json
{"transport": "cli", "task": "<FixWorkerRequest.task>"}
```

No rendered prompt, no environment, no argv — identical exclusion list to
ACP's `execution.started`. `start()` is called outside the adapter's
post-start exception-translation block, an intentional parity choice with
the already-approved ACP adapter (see "Error translation" below).

### Atomic terminal settlement — the CLI-specific strengthening

Unlike ACP (where output streams arrive incrementally, mid-execution, over
an open connection), `ProcessRunner` only ever returns a complete
`ProcessResult` after the subprocess has already terminated. There is
therefore no streaming case to support, and no reason to accept
non-atomic terminal settlement: `settle_success`/`settle_failure` each
build a `list[RunEventSpec]` — zero, one, or two `execution.output` specs
(stdout if non-empty, stderr if non-empty, in that order) followed by
exactly one terminal spec (`execution.completed` or `execution.failed`) —
and append the **entire list in one `RunRuntime.record_many(...)` call**
using the sink's current optimistic expected sequence. Output and terminal
settlement are therefore atomic: either the whole batch (output events +
terminal event) commits, or none of it does. There is no possible
intermediate state where an `execution.output` event exists without its
corresponding terminal event, or vice versa, for a single attempt.

`fail_without_result` (the process-*exception* path — see "Process
exception contract") appends a single-spec batch containing only
`execution.failed`, since no `ProcessResult` exists to derive output events
from.

All four public methods funnel through one private choke point,
`_append_batch(specs)`, mirroring `CanonicalAcpEventSink._append`'s
already-approved shape exactly:

```python
def _append_batch(self, specs: list[RunEventSpec]) -> None:
    try:
        committed, _ = self._runtime.record_many(
            run_id=self._run_id, specs=tuple(specs),
            expected_last_event_seq=self._expected_seq,
        )
    except Exception as exc:
        self._persistence_error = exc
        raise
    self._expected_seq = committed[-1].seq
```

`_expected_seq` is **never** advanced on failure, **never** refreshed from
a fresh `get_run()` read, and there is **never** a retry. Each of
`settle_success`/`settle_failure`/`fail_without_result` begins with:

```python
if self._persistence_error is not None:
    raise self._persistence_error
```

before attempting to build or append anything — mirroring the exact 3J2B
Hardening Round 1 fix to `CanonicalAcpEventSink.fail()`. In the CLI sink's
actual call shape, each attempt calls at most one of these three methods
exactly once, so this guard is structurally close to unreachable in normal
operation — it is included anyway, as written defense-in-depth consistent
with the ACP precedent, not because a currently-reachable code path
depends on it. Canonical integrity/storage failure always has precedence
over a CLI/process-level failure: if a terminal batch append itself fails,
the adapter never attempts a second append with the same (now-stale)
expected sequence — see "Error translation and precedence" below.

### `execution.output` payload

```json
{
  "transport": "cli",
  "stream": "stdout",
  "text": "<sanitized bounded text>",
  "truncated": false,
  "bytes": 123
}
```

(`stream` is `"stdout"` or `"stderr"`; one event per non-empty stream, per
attempt, at most two per batch.) `truncated`/`bytes` are copied verbatim
from `ProcessResult.stdout_truncated`/`stdout_bytes` (or the `stderr_*`
equivalents) — real, already-bounded facts, never re-derived. `text` is
already bounded transitively by `process_runtime.capture.CAPTURE_LIMIT`
(64 KiB) — `ProcessResult.stdout`/`stderr` can never exceed that, so no
*additional* truncation is applied by the CLI sink; the value persisted is
either the original captured text or the fixed redaction marker (see
"Secret-safe output" below), never a further-shortened version of either.
Generic CLI output is never parsed: no `model.*`/`turn.*`/`tool.*`/
`usage.*`/token/cost/agent-message/provider-identity inference is performed
even when stdout happens to look like JSON — it is opaque process output,
persisted as one bounded text fact and nothing else.

### `execution.completed` payload (resolves design question 10)

```json
{
  "transport": "cli",
  "exit_code": 0,
  "timed_out": false,
  "duration_ms": 842,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_bytes": 57,
  "stderr_bytes": 0
}
```

Every field is copied verbatim from the real `ProcessResult` that produced
it. No prompt, argv, env, "final answer," model identity, token count, or
cost field is ever included.

### `execution.failed` payload — non-zero/timeout (resolves design question 11)

```json
{
  "transport": "cli",
  "error_type": "CliNonZeroExit",
  "message": "CLI execution exited with code 3.",
  "exit_code": 3,
  "timed_out": false,
  "duration_ms": 640,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_bytes": 40,
  "stderr_bytes": 88
}
```

`error_type` is one of exactly two synthetic canonical labels chosen by the
adapter, never a Python exception class name here (there is no Python
exception in this branch — a non-zero exit or a timeout is a normal,
settled `ProcessResult`, not a raised exception):

- `"CliTimeout"` when `result.timed_out is True` (checked first — a timed
  out process's exit code is usually meaningless/platform-dependent, so
  `timed_out` takes precedence in both the label and the message);
- `"CliNonZeroExit"` when `result.timed_out is False and result.exit_code != 0`.

`message` is always a short, **synthesized** string built only from
already-safe numeric facts (`f"CLI execution timed out after {timeout_ms}ms."`
or `f"CLI execution exited with code {exit_code}."`) — it is never built
from `result.stdout`/`result.stderr` content, so it can never leak a
secret from process output, and it needs no redaction pass of its own
(that policy is reserved for the *exception*-path diagnostic, see below,
where a real `str(exc)` exists and could contain anything). Full stdout and
stderr (sanitized) are separately available via the `execution.output`
events already appended in the same atomic batch — this is exactly why
Section "Non-zero/timeout contract" says "No raw stderr duplication inside
the failure diagnostic": the diagnostic's job is a one-line synthetic
summary, not a copy of output already recorded elsewhere.

### `execution.failed` payload — process exception (resolves design question 11, exception branch)

```json
{
  "transport": "cli",
  "error_type": "ProcessSpawnError",
  "message": "<redacted-if-sensitive str(exc), NUL-stripped>"
}
```

No `exit_code`/`timed_out`/`duration_ms`/output facts here — no
`ProcessResult` exists in this branch (the process never spawned, or
`ProcessRunner` itself raised before returning one). `error_type` is
`type(exc).__name__` for the real `ProcessSpawnError`/`ProcessCleanupError`/
`ProcessRuntimeError` (or, in principle, any other ordinary `Exception` the
injected process runner raises). `message` goes through the redaction
policy below, since `str(exc)` is real, unconstrained text that could
legitimately contain fragments of the command line, an OS error message
that happened to embed part of an argument, etc.

## Secret-safe output and failure diagnostics

*(Resolves design questions 12, 13.)*

CLI stdout/stderr can echo back anything the child process chooses to
print — including, in the worst case, its own invocation, an environment
value it was configured with, or the prompt it was given. Canonical
history is append-only, so this milestone applies the exact **overlap-safe,
whole-text, fail-closed** policy 3J2B Hardening Round 2 established for
ACP, duplicated in shape here (not imported — `run_runtime.cli` does not
depend on `executor_runtime.acp_worker`, and re-opening the already-closed
3J2B `_failure_message` to extract a shared helper is explicitly out of
scope: "Do NOT reopen ACP behavior without a demonstrated bug").

For one CLI Worker attempt, the complete set of non-empty sensitive
literals is:

```
request.rendered_input                         (the exact prompt)
every CliWorkerLaunchProfile.env key
every CliWorkerLaunchProfile.env value
every resolved launch argv member               (executable + every arg)
```

(Identical composition to ACP's set, with `launch.argv`/`launch.env`
replaced by the CLI launch profile's resolved equivalents.)

**Output redaction** (per stream, independently): computed by the adapter
— never by the sink, which has no knowledge of the prompt/profile — as

```python
def _sanitize(text: str, *, sensitive_literals: list[str]) -> str:
    if not text:
        return text
    if any(literal in text for literal in sensitive_literals):
        return SAFE_REDACTED_CLI_OUTPUT_MESSAGE
    return text
```

applied once to `result.stdout` and once to `result.stderr`, independently
— if only stdout contains a sensitive literal, only stdout's `text` field
becomes the fixed marker; stderr, if clean, is persisted normally (and vice
versa). `SAFE_REDACTED_CLI_OUTPUT_MESSAGE` is one fixed constant, e.g.:

```python
SAFE_REDACTED_CLI_OUTPUT_MESSAGE = (
    "CLI output redacted because it contained sensitive input or launch data."
)
```

**Failure-diagnostic redaction** (exception path only — the non-zero/timeout
path's `message` is synthetic and never needs this, as established above):
identical fail-closed, whole-message policy to ACP's already-approved
`_failure_message`:

```python
def _failure_message(error: Exception, *, sensitive_literals: list[str]) -> str:
    raw_message = str(error).replace("\x00", "")
    if any(literal in raw_message for literal in sensitive_literals):
        return SAFE_REDACTED_CLI_DIAGNOSTIC_MESSAGE
    return raw_message
```

No sequential `str.replace()` of individual secrets is ever performed —
the exact overlap bug fixed in 3J2B Hardening Round 2 (a shorter literal
that is a prefix of a longer one, replaced first, can leave a fragment of
the longer secret behind) is avoided from the start by never attempting
partial substitution at all: it is whole-message-safe or whole-message-
replaced, with no partial state in between. Neither redaction helper is
shared with `executor_runtime.acp_worker`; each module owns its own copy of
the same narrow policy, matching the instruction that this is a deliberate,
approved duplication rather than a shared-helper refactor.

Raw, unredacted sensitive text is never persisted in any other canonical
field, never logged elsewhere by this milestone, and never copied into a
second location "just in case" — the sink persists exactly the sanitized
text/message it is given, once, in the one atomic terminal batch.

## Success contract

*(Resolves design question 20.)*

A CLI Worker attempt succeeds — and only succeeds — when all of the
following hold:

```
ProcessRunner.run(...) returned a ProcessResult (no exception)
AND result.timed_out is False
AND result.exit_code == 0
AND the atomic output+execution.completed batch was durably committed
```

Only then does `CliWorkerAttemptAdapter.run()` return
`WorkerAttemptResult(execution_id=execution_id)` — the same, unmodified
`execution_id` the caller supplied, exactly like both existing adapters.
`exit_code == 0` means only "the CLI process itself terminated normally
without a detected timeout or stdin-delivery failure." It is not, and is
never treated as, evidence that the requested fix is correct — that
judgement remains exclusively Verification's and the Reviewer's, unchanged,
via the same `FixLoopRunner._require_execution_completed` /
Verification-then-Reviewer sequencing every other Worker path already goes
through.

## Non-zero / timeout contract

*(Resolves design question 17, 18.)*

If `ProcessRunner` returns a settled `ProcessResult` with `exit_code != 0`
or `timed_out is True` (checked in that priority order — timeout first),
the attempt is a failure: the adapter calls
`sink.settle_failure(result, sanitized_stdout=..., sanitized_stderr=...,
error_type=..., message=...)` (one atomic batch: sanitized output events +
`execution.failed`, exactly as specified above), and then — once that
durable append itself succeeds — raises `ExecutorAdapterExecutionError`
(never returning a `WorkerAttemptResult`). If the atomic batch append
itself fails, the sink's `persistence_error` guard takes over exactly as
described in "Canonical persistence failure precedence" below; the adapter
never attempts a second, stale-sequence append in that case either.

## Process exception contract

*(Resolves design question 19.)*

If, after `execution.started` has already been durably recorded, the
injected process runner raises an ordinary `Exception` (in production,
`ProcessSpawnError`, `ProcessCleanupError`, or `ProcessRuntimeError` from
`process_runtime.errors`; in a test double, potentially any `Exception`)
instead of returning a `ProcessResult`, there is no `ProcessResult` to
settle and therefore no output events to append. The adapter attempts
exactly one best-effort `sink.fail_without_result(exc, error_type=...,
message=...)` — **unless** `sink.persistence_error is not None` already
(from an earlier failure this same attempt, structurally only possible if
`sink.start()` itself already set it — see "Canonical persistence failure
precedence"), in which case no append is attempted at all and the stored
persistence error is surfaced directly. If the one `fail_without_result`
attempt itself fails, that failure — not the original process exception —
is what gets surfaced (see precedence below), with the original process
exception preserved in the exception context/cause chain.

`asyncio.CancelledError` is not relevant here at all: `CliWorkerAttemptAdapter`
is fully synchronous end to end (`ProcessRunner.run()` is itself a plain
synchronous call — there is no event loop, no `asyncio.run()`, and
therefore no sync-to-async bridge to design, unlike ACP). `KeyboardInterrupt`
and `SystemExit` are never caught by any `except Exception` clause in this
adapter (they are not `Exception` subclasses) and are never converted into
an ordinary Worker failure or into `execution.failed` — they simply
propagate, exactly as the approved ACP contract requires for its own
control-flow signals.

## Canonical persistence failure precedence

*(Resolves design questions 14, 16 — restated as the unified precedence
rule the adapter implements.)*

```python
try:
    result = self._process_runner.run(workspace, process_request)
except Exception as exc:
    if sink.persistence_error is not None:
        raise ExecutorAdapterExecutionError(
            "CLI Worker canonical persistence is unavailable or "
            "sequence-conflicted; not attempting execution.failed."
        ) from sink.persistence_error
    try:
        sink.fail_without_result(exc, error_type=type(exc).__name__,
                                  message=self._failure_message(exc, ...))
    except Exception as terminal_failure:
        raise ExecutorAdapterExecutionError(
            "CLI Worker execution and terminal failure recording both failed."
        ) from terminal_failure
    raise ExecutorAdapterExecutionError("CLI Worker execution failed.") from exc
```

The same `if sink.persistence_error is not None: raise ... from
sink.persistence_error` guard is applied identically around the
`settle_success`/`settle_failure` call sites. Canonical integrity/storage
failure always outranks the CLI/process-level failure that triggered the
settlement attempt: once persistence is known broken, the adapter never
performs a second append with the sink's now-stale expected sequence, never
refreshes that sequence from a fresh read, and never retries. This is the
same rule 3J2B Hardening Round 1 proved for ACP, implemented independently
for the CLI sink's own (simpler, atomic-batch) settlement shape.

## Error translation and precedence — full table

| Situation | Canonical action | Raised adapter error |
| --- | --- | --- |
| Local input/workspace/profile/ID/executable/cwd/`ProcessRequest` (incl. stdin bound) validation | no event | `ExecutorAdapterInputError` |
| Non-`RUNNING` Run at sink construction | no event | `ExecutorAdapterInputError` |
| Process runner raises ordinary `Exception` after `execution.started`, canonical store healthy | one best-effort `execution.failed` (no output events) | `ExecutorAdapterExecutionError`, cause = original exception |
| `ProcessResult` with `exit_code == 0`, `timed_out is False`, durable atomic batch committed | atomic `execution.output`(s) + `execution.completed` | `WorkerAttemptResult` |
| `ProcessResult` with `exit_code != 0` or `timed_out is True` | atomic `execution.output`(s) + `execution.failed` | `ExecutorAdapterExecutionError` |
| Any terminal/failure append itself fails, and persistence was previously healthy | that append failure becomes the surfaced cause | `ExecutorAdapterExecutionError`, cause = the append failure, original failure retained in `__context__` |
| Sink already knows persistence is broken (`persistence_error is not None`) at any settlement attempt | no further append | `ExecutorAdapterExecutionError`, cause = `sink.persistence_error` |
| `asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` from any dependency | not applicable — not `Exception` subclasses | propagates unchanged |

`sink.start(request.task)` is called outside this translation block — the
same structural choice already made (and approved) for
`AcpWorkerAttemptAdapter`. A `start()` failure (for example, a
`RunRuntime` sequence conflict discovered at the very first canonical
write of the attempt) therefore propagates as its raw underlying exception
type from `CliWorkerAttemptAdapter.run()`. This is not silently swallowed:
`FixLoopRunner._run_worker`'s existing, unmodified
`except Exception as exc: raise FixLoopExecutionError(f"Worker port failed: {exc}") from exc`
still converts it at the orchestration boundary. This is a deliberate
parity decision with the already-approved 3J2B pattern, not a new
CLI-specific gap, and is called out explicitly here rather than left
implicit.

## Freshness

*(Resolves design question — freshness/no-reuse requirement.)*

One `CliWorkerAttemptAdapter.run()` call produces exactly one fresh
`ProcessRunner`-spawned child process. There is no session concept for CLI
at all (unlike ACP), so there is nothing to "resume": every attempt
constructs a fresh `ProcessRequest` and calls
`self._process_runner.run(workspace, process_request)` exactly once. No
retry is ever performed by this adapter — a `FixLoopRunner` second attempt
(a new `attempt_index`, a new `worker_execution_id`) is what produces a
second, independent process. `CanonicalCliEventSink` is one-execution-only,
exactly like `CanonicalAcpEventSink`/`CanonicalAgentEventSink`: a fresh
instance is constructed on every `run()` call, never reused across
attempts.

## Test strategy

*(Resolves design question 21.)*

Unit-level (`tests/test_cli_worker.py`), following the exact TDD discipline
already used for `tests/test_acp_worker.py`:

- `CliWorkerLaunchProfile`: absolute/relative/missing command resolution
  (mirrors `resolve_acp_worker_launch`'s existing test matrix exactly);
  args/env immutability and caller-mutation isolation; empty env remains
  empty; empty env key rejected; env key containing `"="` rejected; NUL in
  command/args/env rejected; invalid/oversized `timeout_ms` rejected.
- `ProcessRequest.stdin_text`: `None` preserves existing behavior
  (parametrized against the *existing* `test_process_runtime.py` success/
  timeout/large-output cases, run once with `stdin_text=None` explicitly to
  prove no regression); non-`str` rejected; NUL rejected; exactly-at-bound
  accepted; one-over-bound rejected with `ProcessInputError`, before any
  process spawns (assert a spy/counting process runner double was never
  invoked); `permission_resource()` raises `ProcessInputError` when
  `stdin_text is not None`, and is unaffected (byte-identical output) when
  it is `None`.
- `ProcessRunner` stdin delivery (real subprocess, not a double): a small
  Python `-c` script that reads all of stdin and echoes it to stdout proves
  exact byte-for-byte delivery for a payload near `MAX_STDIN_TEXT_CHARS`;
  a script that never reads stdin and sleeps proves the timeout path still
  cleans up the process tree with a pending/blocked writer thread; a script
  that closes stdin immediately (`sys.stdin.close()`) without reading
  proves `ProcessRuntimeError` is raised with a `BrokenPipeError`-family
  cause, and that no unrelated `ProcessResult` is returned in that case.
- `CanonicalCliEventSink`: requires `RUNNING`; `start()` records exactly one
  `execution.started` with the exact `{"transport": "cli", "task": ...}`
  payload; `settle_success` commits output(s)+`execution.completed` in one
  batch (assert `record_many` called exactly once for the whole batch, via
  a counting wrapper around `RunRuntime.record_many` exactly like the ACP
  suite's `flaky_record_many` pattern); `settle_failure` the same for
  output(s)+`execution.failed`; zero-length stdout/stderr never produces an
  output event; a stream containing a sensitive literal is replaced with
  `SAFE_REDACTED_CLI_OUTPUT_MESSAGE` while an unrelated clean stream in the
  same result is persisted normally (proves per-stream, not all-or-nothing,
  redaction); `persistence_error` is set by a failing `record_many` and a
  subsequent settlement call re-raises it without a second `record_many`
  call (mirrors
  `test_streaming_persistence_failure_does_not_attempt_terminal_append`'s
  call-counting technique exactly); `fail_without_result` never includes
  `exit_code`/`duration_ms`/output facts.
- `CliWorkerAttemptAdapter`: every pre-side-effect rejection (non-
  `FixWorkerRequest`, `LocalWorkspace`, arbitrary `Workspace`, invalid
  `execution_id`, unresolved executable, disposed/missing worktree root,
  oversized `stdin_text` via an oversized `rendered_input` fixture crafted
  at the `FixWorkerRequest` boundary is not reachable since `fix_runtime`
  already bounds it below `MAX_STDIN_TEXT_CHARS` — instead this case is
  proven directly against `ProcessRequest`/`ProcessRunner`, and separately
  proven for the adapter via a structural fake `FixWorkerRequest`-shaped
  object is *not* used — `FixWorkerRequest`'s own bound makes this
  genuinely unreachable through the real port, and the design explicitly
  records that as intentional headroom, not a gap) each assert: zero
  process-runner invocations, zero canonical events beyond the pre-existing
  `RUN_STARTED`. Exact `cwd="."`/exact `stdin_text=request.rendered_input`/
  exact resolved absolute `argv[0]` passed to the injected process runner.
  Structural fake process-runner double accepted without
  `isinstance(process_runner, ProcessRunner)`. Fresh process runner
  invocation per call (two sequential `run()` calls → two distinct
  `ProcessRequest`/`ProcessResult` pairs, two distinct `execution.started`
  events). Success path returns the exact supplied `execution_id` and
  records only real `ProcessResult` facts in `execution.completed`.
  Non-zero exit and timeout each produce the atomic output+`execution.failed`
  batch and raise `ExecutorAdapterExecutionError`, never a
  `WorkerAttemptResult`. A raised `ProcessSpawnError`/`ProcessCleanupError`/
  `ProcessRuntimeError` from the injected runner produces exactly one
  best-effort `execution.failed` with no output events. The three redaction
  integration tests (env-key-is-prefix-of-value, prompt-overlapping-env-
  value, sensitive-launch-argument), reproduced against the CLI adapter's
  own `_failure_message`/`_sanitize` the same way 3J2B Hardening Round 2
  proved them for ACP. `KeyboardInterrupt`/`SystemExit` from the injected
  runner propagate unchanged. Persistence-conflict precedence
  (`test_completion_sequence_conflict_skips_execution_failed_append`-style,
  reproduced for the CLI atomic-batch shape) proves no second, stale-sequence
  append and the correct cause/context chain.

Real-process integration (`tests/test_cli_worker_integration.py`, using a
new `tests/fixtures/cli_fake_agent.py` — a small, dependency-free,
provider-neutral Python script, **not** using the ACP SDK or any
JSON-RPC framing, mode-selected by `sys.argv[1]`, mirroring the existing
`acp_fake_agent.py` mode-selection convention without sharing any of its
ACP-specific implementation):

- `echo` mode: reads all of stdin to EOF, writes the exact received text
  byte-for-byte to a file under cwd (so the test can assert exact delivery
  independent of stdout buffering), prints a short fixed line to stdout,
  exits 0. Proves: exact `rendered_input` arrives via stdin; prompt is
  absent from the observed argv (the test inspects the launch profile's
  own static `args`, which never contain the prompt, and separately
  confirms the fake agent's own `sys.argv` — captured by the fixture into
  the same received-text file — contains no fragment of the prompt).
- `mutate` mode: reads stdin (discarded), writes a fixed, known file
  (`cli_worker_mutated.txt`) with fixed content under cwd, exits 0. Used
  together with a real `GitWorktreeWorkspace.create()` over a real git
  source checkout (mirroring
  `tests/test_acp_worker_integration.py::_git_source`/`_real_adapter`
  exactly) to prove: shadow worktree mutated, source checkout unchanged,
  `execution.completed` present.
- `fail` mode: reads stdin, writes a fixed line to stderr, exits a fixed
  non-zero code. Proves the non-zero contract end to end, including that
  `execution.failed` (not `execution.completed`) is the only terminal
  event and that stderr is persisted (sanitized) as an `execution.output`
  event in the same batch.
- `hang` mode: reads stdin fully (so the "stdin delivered, then the CLI
  hangs" case — not the "stdin was never delivered" case — is what is
  under test) then sleeps far longer than the test's configured
  `timeout_ms`. Proves: `ProcessRunner`'s existing timeout +
  `terminate_process_tree` path is reached through the CLI adapter, the
  fake process is actually gone afterward (checked via `psutil`, the same
  pattern `tests/test_acp_fake_agent_integration.py` already uses), and
  `execution.failed` with `error_type == "CliTimeout"` is recorded.

No fake JSON-RPC of any kind is implemented anywhere in this fixture or
these tests. No real Codex/Claude/Gemini/Qwen installation is required by
any test in this milestone.

## Scope boundary with 3K

*(Resolves design question 22.)*

`CliWorkerLaunchProfile` is deliberately configuration-only: it has no
notion of "this is the fallback for provider X," no ordered list of
candidate commands, no environment-driven auto-discovery of an installed
CLI, and no coupling to `AcpWorkerLaunchProfile`/`AcpWorkerAttemptAdapter`
beyond both independently implementing the same `WorkerAttemptRunner`
port. Selecting *which* of `NativeWorkerAttemptAdapter`/
`AcpWorkerAttemptAdapter`/`CliWorkerAttemptAdapter` a given `FixLoopRunner`
composition uses for a given Run is entirely a 3K concern (`RoutingPolicy`)
and is not referenced, anticipated, or partially implemented anywhere in
this design. No code in this milestone imports or is imported by anything
under a hypothetical `routing_runtime`/`RoutingPolicy` namespace, and none
of the three adapters know about each other's existence.

## Self-review

Performed against the checklist this milestone was asked to self-review
for:

- **Contradictions**: none found. The one place two requirements could
  appear to conflict — "reuse ProcessRunner's safe environment" (CLI) vs.
  "exact environment" (ACP) — is explicitly resolved as an intentional
  difference between the two adapters, not a contradiction within either
  one.
- **Missing failure paths**: the four-way split (input rejection / process
  exception / non-zero-or-timeout / persistence conflict) covers every
  return/raise site of `ProcessRunner.run()` (a normal `ProcessResult`, or
  one of the three `process_runtime.errors` types, or — newly — a stdin
  delivery failure surfaced as `ProcessRuntimeError`, already covered by
  "process exception" since it is the same exception type).
- **Secret leakage**: three distinct redaction surfaces identified
  (stdout, stderr, exception-diagnostic message) and each is covered by
  the same overlap-safe, whole-text, fail-closed policy; the synthetic
  non-zero/timeout `message` is proven incapable of leaking secrets by
  construction (never built from output text) rather than by a redaction
  pass. `permission_resource()` fails closed rather than silently omitting
  `stdin_text`.
- **Data-loss/recovery issues**: none — this milestone adds no new
  persistent storage of its own; canonical events are the same
  `RunRuntime`/SQLite store every other adapter already writes through, and
  the atomic-batch design makes partial/torn writes for one attempt
  impossible (batch commits or it doesn't).
- **Unbounded resources**: `stdin_text` is bounded (`MAX_STDIN_TEXT_CHARS`);
  the stdin-writer thread holds exactly one pre-encoded, already-bounded
  payload and is discarded after one write attempt; stdout/stderr remain
  bounded by the existing `BoundedCapture`/`CAPTURE_LIMIT`; no new
  in-memory accumulation of any kind is introduced.
- **Process-tree leaks**: the stdin-writer thread never blocks
  `ProcessRunner`'s existing timeout/cleanup path (it runs concurrently,
  is joined with the same bounded `timeout=3` the output threads already
  use, and a still-blocked write unblocks with `BrokenPipeError` once
  `terminate_process_tree` kills the child) — no new process-tree leak
  surface is introduced.
- **Stale optimistic-sequence retry**: explicitly forbidden and structurally
  prevented by the `persistence_error` guard on every settlement method,
  duplicated from the proven 3J2B Hardening Round 1 fix.
- **Prompt mutation**: `request.rendered_input` flows into
  `ProcessRequest(stdin_text=request.rendered_input)` with no
  intermediate transformation, concatenation, or reconstruction anywhere
  in the adapter.
- **Dependency inversion**: verified against the dependency-direction table
  above; `process_runtime` gains no new imports from any higher layer;
  `run_runtime.cli` gains no import of `executor_runtime`/`fix_runtime`.
- **Accidental 3K scope**: verified in "Scope boundary with 3K" above — no
  selection/ordering/routing logic anywhere in this design.
- **Accidental `adapters.py` coupling**: verified in "Why the legacy
  `adapters.py` CLI wrappers are not reused" above — zero shared code,
  shapes, or conventions; the argv-prompt pattern that module uses is the
  exact pattern this milestone's hard invariant forbids.

No open question from the assignment was left unresolved; none required a
placeholder or a "TBD."

## Expected file boundaries (for a future implementation plan)

Create: `executor_runtime/cli_worker.py`, `run_runtime/cli.py`,
`tests/test_cli_worker.py`, `tests/test_cli_worker_integration.py`,
`tests/fixtures/cli_fake_agent.py`.

Modify narrowly: `executor_runtime/__init__.py` (export the three new
public names), `run_runtime/__init__.py` (export `CanonicalCliEventSink`),
`process_runtime/models.py` (`stdin_text` field, `MAX_STDIN_TEXT_CHARS`,
`permission_resource()` guard), `process_runtime/runner.py` (stdin-writer
thread).

Not modified: `process_runtime/__init__.py` — `MAX_STDIN_TEXT_CHARS` is
imported directly from `process_runtime.models`, the same precedent
`CAPTURE_LIMIT` already establishes (`tests/test_process_runtime.py`
imports it from `process_runtime.capture`, not from the package
`__init__`); there is no existing convention of re-exporting these
bound constants from the package root, so this milestone does not start
one. `tests/test_process_runtime.py`/`tests/test_process_tool.py` gain new
test functions (additive) but no existing test in either file is changed.
`tests/fixtures/acp_fake_agent.py` is not touched. `fix_runtime/*`,
`executor_runtime/native_worker.py`, `executor_runtime/acp_worker.py`,
`run_runtime/acp.py`, `run_runtime/native_agent.py`, and every ACP/native
test file are untouched — no existing contract defect was discovered in
any of them while researching this design, so there is nothing to stop and
report.

Milestone 3J3
DESIGN COMPLETE
IMPLEMENTATION PLAN NOT STARTED
IMPLEMENTATION NOT STARTED
INDEPENDENT DESIGN REVIEW PENDING
