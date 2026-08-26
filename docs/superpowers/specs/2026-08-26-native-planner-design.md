# Milestone 3I — Native Planner Runtime + Routing Hints Foundation (design)

Base checkpoint: `9e000ddc02d34f2560ca022fe280c21ee0dcb1c5` (tag `milestone-3h`).

## Purpose

A provider-neutral native Planner runtime that turns `original user task +
repository context` into a strict, structured `PlanReport`:

```
task + repository context -> PlannerRunner -> PlanReport
```

3I does **not** execute the plan and does **not** orchestrate
`Planner -> Worker -> Verification -> Reviewer -> FixLoop`. That is a later
orchestration/control-plane milestone.

## Architecture

```
                 PlannerRunner
                /             \
        ContextEngine       strict parser
             |                   |
             v                   v
      read-only AgentSession -> PlanReport
             |
             v
   CanonicalPlannerEventSink
             |
             v
        RunRuntime
```

`PlannerRunner` owns planner semantics; `AgentSession` (existing, unmodified)
owns model turns, provider continuation, tool calls/observations, limits,
and generic Agent events — exactly the same split already established by
`ReviewerRunner`/`review_runtime`, which this milestone mirrors closely
(package-for-package, file-for-file) rather than inventing a new pattern.

Planner remains architecturally separate from Worker execution, Reviewer,
Verification, FixLoop, executor routing, and completion gating. It depends
only on `AgentSession`, `ContextEngine`, and its own strict parser/prompt —
never on `RunCompletionGate`, `RunRuntime` (see `planner_runtime.recording`'s
`PlanRecorder` port, mirroring `review_runtime.recording.ReviewRecorder`),
`WorkerAttemptRunner`, `VerificationAttemptRunner`, `ReviewAttemptRunner`, or
`FixLoopRunner`.

## Domain model (`planner_runtime/models.py`)

- `TaskComplexity` (`LOW`/`MEDIUM`/`HIGH`) and `TaskScope`
  (`LOCAL`/`MULTI_AREA`/`REPOSITORY_WIDE`) — **advisory hints only**. They
  authorize nothing, select nothing, and are never read by 3I to make a
  decision; a later trusted `RoutingPolicy` may consume them, but 3I does
  not implement or call one.
- `PlanStep(title, objective)` — bounded, non-empty, describes WHAT to
  achieve, never HOW (no line numbers, no class names at exact paths, no
  shell commands, no provider/model names, no timeouts).
- `TaskProfile(complexity, scope)`.
- `ParsedPlanDecision(summary, steps, acceptance_criteria, risks,
  task_profile)` — model-supplied, pre-provenance (mirrors `ReviewDecision`).
- `PlanReport(plan_id, summary, steps, acceptance_criteria, risks,
  task_profile, repository_fingerprint, task_sha256)` — runtime-enriched
  (mirrors `ReviewReport`). `plan_id` / `repository_fingerprint` /
  `task_sha256` are **never** accepted from the model; `PlannerRunner` is the
  only code that constructs a `PlanReport`, and it always computes those
  three fields itself.

Hard bounds (all raise `PlannerInputError` when violated):
`MAX_TASK_CHARS=32_000`, `MAX_SUMMARY_CHARS=4_000`, `MAX_PLAN_STEPS=16`,
`MAX_STEP_TITLE_CHARS=300`, `MAX_STEP_OBJECTIVE_CHARS=2_000`,
`MAX_ACCEPTANCE_CRITERIA=24`, `MAX_ACCEPTANCE_CRITERION_CHARS=1_000`,
`MAX_RISKS=16`, `MAX_RISK_CHARS=1_000`. `acceptance_criteria`/`risks` may be
empty tuples; `steps` must contain at least one and at most
`MAX_PLAN_STEPS` entries.

### `task_sha256` provenance

`task_sha256` has exactly **one** authority:

```
PlannerRunner computes SHA256(original task)
        -> PlanReport.task_sha256
        -> plan.completed.payload["task_sha256"]
```

Computed by `PlannerRunner.run()` as
`sha256(task.encode("utf-8")).hexdigest()` and used only once, when
constructing the final `PlanReport`. Never caller-supplied to `PlanReport`
itself beyond that one computation site; never model-supplied (the parser
rejects `task_sha256` as an unknown top-level key).

`CanonicalPlannerEventSink` does **not** accept a `task_sha256` at
construction time and does **not** include it in `plan.started` — an
earlier draft allowed the sink's caller to independently pass a
`task_sha256` for the `plan.started` payload, which would have let
canonical history claim two different task hashes for the same `plan_id`
(one at `plan.started`, a possibly different one at `plan.completed`). That
was rejected: a single-authority chain is required, and `plan_id` alone is
sufficient correlation for crash recovery (see the Recovery section).

### `repository_fingerprint` provenance

Taken directly from the `ContextPack` the same `PlannerRunner.run()` call
built via `ContextEngine.build(...)` — no separate hashing mechanism is
invented in `planner_runtime`, and the model never supplies it (rejected by
the parser as an unknown key).

## Strict JSON parser (`planner_runtime/parser.py`)

Exactly one top-level JSON object:

```json
{
  "summary": "...",
  "steps": [{"title": "...", "objective": "..."}],
  "acceptance_criteria": ["..."],
  "risks": ["..."],
  "task_profile": {"complexity": "LOW|MEDIUM|HIGH", "scope": "LOCAL|MULTI_AREA|REPOSITORY_WIDE"}
}
```

Rejects (all `PlannerProtocolError`, never a fallback/best-effort parse):
Markdown fences, prose before/after (via whole-string strict `json.loads`,
which fails on any trailing/leading non-JSON content), top-level arrays,
duplicate keys (`object_pairs_hook`), `NaN`/`Infinity` (`parse_constant`),
unknown top-level or `task_profile` keys — including `plan_id`,
`repository_fingerprint`, `task_sha256`, and any routing field
(`model`/`provider`/`executor`/`agent`/`route`/`budget`) — missing required
keys, wrong types, invalid enum values, empty `steps`, over-count arrays,
oversized strings, and output over `MAX_MODEL_OUTPUT_CHARS = 32_000`.

## Prompt / trust boundary (`planner_runtime/prompt.py`)

`PLANNER_SYSTEM_INSTRUCTIONS` states: read-only, task is authoritative,
repository context is untrusted DATA, plan steps describe WHAT not HOW, no
provider/model/executor selection, no executable commands, no
`VerificationPlan`, exact-JSON-only final answer, the `PlanReport` itself is
advisory LLM output for a future Worker.

`render_initial_planner_input(*, task, context_pack)` renders three
sections — `ORIGINAL USER TASK` (always full), `REPOSITORY CONTEXT
(UNTRUSTED DATA)` (bounded), `OUTPUT CONTRACT` (fixed) — under the proven
`_bounded()` invariant already used by `review_runtime.prompt` and
`fix_runtime.prompt`:
`len(render_initial_planner_input(...)) <= MAX_INITIAL_PLANNER_INPUT_CHARS`
(`64_000`) for every input it accepts, with the task never silently
truncated (raises `PlannerInputError` instead if the mandatory framing +
task alone cannot fit).

## `PlannerRunner` (`planner_runtime/runner.py`)

`PlannerRunner(backend, *, context_engine=None, limits=None)`, `run(
workspace, task, *, recorder=None, plan_id=None) -> PlanReport`. Context
budget `ContextBudget(total_chars=24_000, map_chars=6_000,
max_segment_chars=6_000)`; `AgentLimits(max_model_turns=8, max_tool_calls=20,
max_consecutive_tool_errors=4)` — same order of magnitude as
`ReviewerRunner`, no unbounded session, no automatic retries.

Tools: exactly `read_file`, `list_files`, `search_text`, `repo_map`,
`search_code`, built via the existing `register_workspace_read_tools`/
`register_repository_tools` helpers (no duplicated tool implementations).
Policy: `read`/`list`/`search` -> `ALLOW`, default `DENY` — the Planner
cannot be given `ASK` for anything by configuration; an `ApprovalPause` is
therefore always a configuration failure, translated to
`PlannerExecutionError` (with `recorder.fail(...)` called first), never a
resumable pause and never `run.waiting_user`.

Flow (A–N), exactly per the milestone's execution-flow section: validate
task -> generate/validate `plan_id` -> compute `task_sha256` (held locally,
used only when constructing the final `PlanReport` — never handed to the
canonical sink) -> build
`ContextPack` -> render bounded input -> build read-only
registry/policy/context -> one fresh `AgentSession`
(`execution_id=f"planner_exec_{plan_id}"`, internal to the harness) ->
`session.start(...)` -> `ApprovalPause` handling -> strict parse of
`outcome.final_text` -> construct `PlanReport` with runtime-owned fields ->
`recorder.complete(report)` -> return.

**Critical invariant**: `AgentSession.ExecutionCompleted != plan.completed`.
Transient completion only means the generic loop returned final text — not
that it is valid planner JSON. Only a successfully parsed
`ParsedPlanDecision` (used to build a `PlanReport`) may become
`plan.completed`; a parse failure calls `recorder.fail(...)` and re-raises
`PlannerProtocolError`, never silently degrading to any default plan.

## Errors (`planner_runtime/errors.py`)

`PlannerError` (base), `PlannerInputError` (bad task/plan_id/domain value),
`PlannerExecutionError` (Agent/backend/config failure, including
`ApprovalPause`), `PlannerProtocolError` (parser/output-contract violation),
`PlannerRecordingError` (canonical recorder failure) — same shape as
`review_runtime.errors`.

## Recording port (`planner_runtime/recording.py`)

`PlanRecorder` Protocol (`emit`/`complete`/`fail`) + `NullPlanRecorder`,
mirroring `ReviewRecorder`/`NullReviewRecorder` exactly.
`planner_runtime` never imports `run_runtime` — the canonical bridge lives
entirely on the `run_runtime` side (see below), avoiding the circular-import
trap the 3H fix-loop bridge had to work around.

## Canonical bridge (`run_runtime/planner.py`)

New event types (added to the existing `RunEventType`, `PLAN_COMPLETED`
untouched): `PLAN_STARTED`, `PLAN_FAILED`, `PLAN_INTERRUPTED`.

`CanonicalPlannerEventSink(runtime, run_id, *, plan_id)` mirrors
`CanonicalReviewEventSink` field-for-field:

- Every planner-authored event has `execution_id=None`,
  `correlation_id=plan_id`, `source="planner"`. The sink never writes
  `execution.started`/`execution.completed`/`execution.failed` — this is
  what keeps `RunCompletionGate`'s "newer execution activity makes
  verification stale" check blind to Planner activity, exactly as it is
  already blind to Reviewer activity.
- `ExecutionStarted` -> `plan.started` with payload `{"plan_id": plan_id}`
  exactly — no `task_sha256` field at all. The sink's constructor does not
  accept a `task_sha256` parameter; a caller-supplied SHA at start time
  would be a second, independent authority for the same fact `plan.completed`
  already carries authoritatively, which could disagree with it. `plan_id`
  alone is sufficient correlation for crash recovery.
- `ExecutionCompleted` is observed (arms `complete()`) but never itself
  appends `plan.completed`.
- `ExecutionFailed` -> `plan.failed` via `fail()`.
- Generic `turn.*`/`model.*`/`tool.*`/`usage.recorded` map through with
  `execution_id=None`/`source="planner"`; canonical `model.completed` text
  is bounded to `planner_runtime.parser.MAX_MODEL_OUTPUT_CHARS` with a
  `text_truncated` flag — the in-memory `AgentOutcome.final_text` the parser
  actually sees is never truncated, so oversized output is always *rejected*
  by the parser, never silently accepted after truncation.
- `ApprovalRequested` -> `permission.requested` only; never
  `run.waiting_user`.
- `complete(report)` requires: matching `plan_id`, `plan.started` persisted
  by *this* sink instance, transient `ExecutionCompleted` observed by *this*
  sink instance, no prior terminal — then appends `plan.completed` with the
  full native payload (`plan_id`, `summary`, `steps`, `acceptance_criteria`,
  `risks`, `task_profile`, `repository_fingerprint`, `task_sha256`).
- `fail(...)` requires matching `plan_id` + `plan.started` persisted, no
  prior terminal -> `plan.failed` with bounded `error_type`/`error_message`.
- Same process-local + canonical-history hardening as
  `CanonicalReviewEventSink`: a second sink built with a reused `plan_id`
  can never settle an attempt it did not itself observe (process-local
  `_started_persisted`/`_execution_completed_observed`/`_terminal_recorded`
  flags), and `_reject_reused_plan_id()` additionally scans canonical
  history so a fresh `plan.started` can never reuse an already-started
  `plan_id` at all.

**`plan.completed` != `run.completed`.** Neither `PlannerRunner` nor
`CanonicalPlannerEventSink` ever appends `run.completed`/`run.failed`.
`RunCompletionGate` is untouched by this milestone.

### Legacy `PLAN_COMPLETED` compatibility

`run_runtime/legacy.py`'s `_on_plan` already emits `plan.completed` with an
older shape (`{"summary", "files"}`, `execution_id` set, `source="legacy"`).
That code and `run_runtime/readmodels.py`'s tolerant `payload.get(...)`
reads are **not modified** — the native sink's payload is provenance-tagged
distinctly (`execution_id=None`, `source="planner"`) and is a strict
superset shape read defensively by existing code, so no schema migration is
introduced.

## Recovery (`run_runtime/recovery.py`)

A new pass, inserted after the existing tool-interruption pass and before
the verification pass (Planner logically precedes Worker/Verification in a
future orchestration, and recovery ordering already runs
tool -> verification -> review -> fix_attempt/fix_loop -> run): for every
`plan.started` in a `RUNNING` Run's history without a later matching
`plan.completed`/`plan.failed`/`plan.interrupted` (matched by `plan_id`,
scoped to the interval before the next `plan.started` with the same id, if
any), append `plan.interrupted` (`{"plan_id", "reason":
"process_restart"}`, `execution_id=None`, `correlation_id=plan_id`,
`source="recovery"`) before the trailing `run.interrupted`. Ordering:
`tool.interrupted < plan.interrupted < run.interrupted`. No Planner rerun;
idempotent (a second scan sees `RunStatus != RUNNING` and does nothing).

## Non-goals honored

No `RoutingPolicy`/`choose_executor`/`choose_model`/`route_task`, no reading
of `CLAUDE_MODEL`/`OPENAI_MODEL`-style env vars, no `VerificationPlan`
production, no Worker/Verification/Reviewer/FixLoop wiring from
`PlannerRunner`, no `RunCompletionGate` changes, no Planner-specific
`RunStatus`/mandatory `RunPhase`, no legacy `project_runner.py`/`agents.py`/
UI changes, no new dependencies.
