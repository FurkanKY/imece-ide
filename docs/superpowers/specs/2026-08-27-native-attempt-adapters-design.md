# Milestone 3J1 — Native Attempt Adapters (design)

Checkpoint: `521e29759a356ca9fd4e6ff0099d08fcb89b87e0` (tag `milestone-3i`).

## Purpose

3H introduced three small Protocol ports that `FixLoopRunner` depends on
instead of a second Agent harness: `WorkerAttemptRunner`,
`VerificationAttemptRunner`, `ReviewAttemptRunner` (`fix_runtime/ports.py`).
No concrete production implementation of these ports exists yet. 3J1 adds
exactly that: thin adapters in a new `executor_runtime/` package that bind
each port to the already-existing native runtime that already does the real
work (`AgentSession`, `VerificationRunner`, `ReviewerRunner`).

## Architectural rule: HOW, not WHEN/WHICH

Adapters answer "how does this existing attempt port invoke the existing
runtime?" — never "when should it run", "which executor/provider should be
selected", "should another attempt run", or "is the Run complete". Those
remain `FixLoopRunner`/`RunCompletionGate` orchestration responsibilities.

Consequently `executor_runtime` never imports `FixLoopRunner`,
`RunCompletionGate`, `PlannerRunner`, `ChangeProvider`, or `RoutingPolicy`
(the last does not exist yet). It also never imports `change_runtime` — diff
capture/stall detection stays owned by `FixLoopRunner`.

## Run-scoped adapters

The existing port methods (`WorkerAttemptRunner.run`,
`VerificationAttemptRunner.run`, `ReviewAttemptRunner.run`) do not carry
`run_id` — it's not part of the 3H contract and is not being changed here.
Concrete adapters that need to write canonical events are therefore
constructed with `runtime: RunRuntime` and `run_id: str` bound at
**construction time**, not passed per-call. Each adapter exposes a
read-only `run_id` property for diagnostics/tests.

Composition invariant (documented, not enforced by new code): the caller
composing a `FixLoopRunner` for a given Run must construct a fresh adapter
set bound to that same `run_id`. Passing a `run_id`-bound adapter into a
`FixLoopRunner` invocation for a *different* Run is invalid composition;
3J1 does not add a check for this — a future control-plane milestone is
where per-Run adapter composition actually gets constructed, and is the
right place to enforce it.

## Errors

`executor_runtime/errors.py` — a small, deliberately flat hierarchy:

- `ExecutorAdapterError` (base)
- `ExecutorAdapterInputError` — caller/config contract violation: wrong
  workspace type, wrong request/plan/request type, malformed
  execution_id/verification_id/review_id, sink construction against a Run
  that isn't RUNNING.
- `ExecutorAdapterExecutionError` — the underlying AgentSession/
  VerificationRunner/ReviewerRunner infrastructure failed, or returned
  evidence that violates the port's provenance contract (wrong id echoed
  back). `__cause__` is always preserved when wrapping.

Semantic outcomes are never errors: `VerificationStatus.FAIL/TIMEOUT/ERROR`
and `ReviewVerdict.NEEDS_FIX` are valid, normally-returned reports.

## NativeWorkerAttemptAdapter (`executor_runtime/native_worker.py`)

```
NativeWorkerAttemptAdapter(runtime, run_id, backend, *, context_engine=None, limits=None)
```

`limits` defaults to `AgentLimits(max_model_turns=20, max_tool_calls=50,
max_consecutive_tool_errors=5)` — the existing generic Worker harness
bounds, no retries.

### Safety invariant: GitWorktreeWorkspace only

The fix Worker's policy grants `edit`/`delete` ALLOW. `run()` therefore
requires `isinstance(workspace, GitWorktreeWorkspace)` — checked before
`backend.open_session`, before `execution.started`, before any mutation.
`LocalWorkspace` (or any other `Workspace`) is rejected with
`ExecutorAdapterInputError`. This is deliberately a concrete-type check, not
a capability/Protocol check: `GitWorktreeWorkspace` is the one isolated
shadow-worktree implementation that exists today (`workspace/worktree.py`);
weakening this to "anything implementing `Workspace`" would let the Worker
mutate the user's real checkout via `LocalWorkspace`. A future capability
based isolation abstraction (Docker/GitClone) is out of scope.

### Validation order in `run()`

1. `isinstance(request, FixWorkerRequest)` else `ExecutorAdapterInputError`.
2. `isinstance(workspace, GitWorktreeWorkspace)` else
   `ExecutorAdapterInputError`.
3. `WorkerAttemptResult(execution_id=execution_id)` constructed eagerly —
   this both validates the id against the existing stable-id contract
   (`fix_runtime.models._stable_id`, via the existing dataclass, no regex
   duplication) and produces the exact object returned on success.
4. Build a fresh `ToolRegistry`/`PolicyEvaluator`/`ToolExecutionContext`/
   `CanonicalAgentEventSink` (wrapping sink-construction `ValueError`, e.g.
   Run not RUNNING, as `ExecutorAdapterInputError`).
5. Build one fresh `AgentSession` with `execution_id=execution_id` and
   `event_sink=<that sink>`.
6. `session.start(request.rendered_input)` — **exactly** that string, never
   reconstructed from `request.task`/`request.trigger`/`request.plan`, never
   prefixed or truncated. System framing belongs in
   `AgentSession.instructions`, not in the input text.

### Tool surface — exactly seven tools, no process/shell

```
register_workspace_tools(registry)   # read_file, list_files, search_text, write_file, delete_path
register_repository_tools(registry, engine=context_engine)  # repo_map, search_code
```

`register_workspace_tools` already composes
`register_workspace_read_tools` internally (see
`tool_runtime/tools/workspace_files.py`), so this is the minimal call that
yields exactly the required seven specs. `run_process`
(`tool_runtime/tools/process.py`) is never registered — the Worker edits,
Verification executes trusted checks separately (see rationale below).

### Policy — ALLOW-only, fail-closed default, no ASK

```
read/list/search/edit/delete -> ALLOW; default -> DENY
```

No `ASK` rule exists in this policy. The Worker is non-interactive and runs
inside an isolated worktree, so a correctly configured Worker must never
pause for approval. If `session.start()` nonetheless returns an
`ApprovalPause` (this should be structurally impossible given the policy
above), the adapter raises `ExecutorAdapterExecutionError` and does **not**
call `session.resume(...)` — an approval pause here is a configuration bug,
not a legitimate interactive path, and 3J1 intentionally does not introduce
any `run.waiting_user` behavior for the automatic fix Worker.

### System instructions

`NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS` (module-level bounded constant)
tells the Worker: it is an implementation/fix Worker; `rendered_input`
already carries an explicit trust boundary (ORIGINAL USER TASK is
authoritative, GENERATED PLAN/FIX FEEDBACK are diagnostic data — mirrors
`fix_runtime/prompt.py`'s framing, not duplicated logic, just a consistent
instruction to the model); inspect the repo with the available tools before
editing; change only what's necessary; there is no shell/process tool, so it
must never claim to have run tests — deterministic Verification runs
separately afterward; return a concise plain-text completion summary that is
not itself a correctness verdict. No structured JSON output is required from
the Worker (unlike Planner/Reviewer) — `AgentOutcome.final_text` is not
parsed or interpreted by the adapter at all.

### Result / failure semantics

- Success (`AgentOutcome` returned, no exception): return the
  `WorkerAttemptResult` built in step 3 above. The adapter never inspects
  `final_text`, never reads the workspace diff, never runs Verification,
  never decides whether the fix is correct — that's `FixLoopRunner`'s job
  via the `ChangeProvider`/`VerificationAttemptRunner`/`ReviewAttemptRunner`
  ports.
- `AgentRuntimeError` from `session.start()`: wrapped as
  `ExecutorAdapterExecutionError(...)  from exc`. Never swallowed, never
  turned into a synthetic success. `AgentSession` + `CanonicalAgentEventSink`
  already own the normal `execution.failed` trajectory internally.
- `ApprovalPause`: `ExecutorAdapterExecutionError`, not resumed (see above).

### Canonical ownership

The adapter constructs exactly one `CanonicalAgentEventSink` per call and
hands it to a brand-new `AgentSession` as `event_sink`. It never calls
`runtime.record`/`runtime.record_many` itself — all `execution.*`/`turn.*`/
`model.*`/`tool.*` events are produced by the existing sink from the
existing `AgentSession` event stream, unmodified.

## NativeVerificationAttemptAdapter (`executor_runtime/native_verification.py`)

```
NativeVerificationAttemptAdapter(runtime, run_id, *, process_runner=None)
```

`process_runner` defaults to a fresh, stateless `ProcessRunner()`. The
constructor does not execute anything.

`run(workspace, plan, *, verification_id)`:

1. `isinstance(plan, VerificationPlan)` else `ExecutorAdapterInputError`
   (before any process execution).
2. Construct `CanonicalVerificationEventSink(runtime, run_id,
   verification_id=verification_id)` (wrapping construction `ValueError` —
   empty id or Run not RUNNING — as `ExecutorAdapterInputError`).
3. Construct a fresh `VerificationRunner(process_runner, event_sink=sink)`.
4. `runner.run(workspace, plan, verification_id=verification_id)`, wrapping
   any non-`ExecutorAdapterExecutionError` exception as
   `ExecutorAdapterExecutionError`.
5. Require `isinstance(report, VerificationReport)` and
   `report.verification_id == verification_id`, else
   `ExecutorAdapterExecutionError` (fail closed on a contract violation).
6. Return the report as-is.

`PASS`/`FAIL`/`TIMEOUT`/`ERROR` are all normal returns — the adapter never
raises merely because `report.status != PASS`, never retries, never calls
`RunCompletionGate`, never decides the next workflow step.
`CanonicalVerificationEventSink` remains the sole canonical bridge for the
attempt; the adapter never emits `execution.*`.

## NativeReviewAttemptAdapter (`executor_runtime/native_reviewer.py`)

```
NativeReviewAttemptAdapter(runtime, run_id, reviewer: ReviewerRunner)
```

The caller constructs the `ReviewerRunner` itself (with whatever
`ModelBackend`/`ContextEngine`/`AgentLimits` it wants) and hands it in —
semantic reviewer configuration stays owned by `review_runtime`, not
duplicated here.

`run(workspace, request, *, review_id)`:

1. `isinstance(request, ReviewRequest)` else `ExecutorAdapterInputError`.
2. Construct `CanonicalReviewEventSink(runtime, run_id, review_id=review_id)`
   (wrapping construction failure — `ReviewInputError` for a malformed id,
   or `ValueError` for Run not RUNNING — as `ExecutorAdapterInputError`).
3. `reviewer.run(workspace, request, recorder=sink, review_id=review_id)`,
   wrapping any non-`ExecutorAdapterExecutionError` exception (backend
   failure, `ReviewProtocolError` from a malformed model answer, an
   unexpected approval pause already turned into `ReviewExecutionError`
   internally, ...) as `ExecutorAdapterExecutionError`.
4. Require `isinstance(report, ReviewReport)` and
   `report.review_id == review_id`, else `ExecutorAdapterExecutionError`.
5. Return the report as-is.

`APPROVED`/`NEEDS_FIX` are both normal returns. No retry, no
`RunCompletionGate` call, no prompt/context construction inside
`executor_runtime` — that all stays inside `ReviewerRunner`. Canonical
`review.*` events keep `execution_id=None` (owned entirely by
`CanonicalReviewEventSink`, unmodified — see its module docstring on why
Reviewer activity must never look like `execution.*` activity to
`RunCompletionGate`).

## Public exports (`executor_runtime/__init__.py`)

```
ExecutorAdapterError, ExecutorAdapterInputError, ExecutorAdapterExecutionError
NativeWorkerAttemptAdapter, NativeVerificationAttemptAdapter, NativeReviewAttemptAdapter
```

No internal registry/policy helper is exported — mirrors
`review_runtime`/`planner_runtime` keeping `_reviewer_registry`/
`_planner_registry` private-by-convention (importable for tests via the
module, not re-exported).

## Explicitly out of scope for 3J1

No `RoutingPolicy`, no ACP/CLI executor, no generic `TaskExecutor`
abstraction, no full Planner → Worker → Verification → Reviewer → FixLoop
orchestration, no `fix_runtime/ports.py`/`fix_runtime/runner.py`
modification, no UI/legacy `adapters.py`/`providers.py` change, no new
dependency.
