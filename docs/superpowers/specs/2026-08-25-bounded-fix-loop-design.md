# Milestone 3H — Bounded Fix Loop (design)

Status: APPROVED. Implemented on top of `milestone-3g` (commit `d1dedd8e`).

## Purpose

3H introduces the first bounded evaluator/optimizer feedback loop:

```
Worker -> Verification -> Reviewer
                              |-- APPROVED    -> reviewed completion
                              '-- NEEDS_FIX   -> bounded fresh Worker fix attempt
```

Deterministic Verification always takes priority over semantic Review:

- Verification `FAIL` -> fixable feedback (another Worker attempt, budget permitting).
- Verification `PASS` -> Reviewer runs.
- Verification `TIMEOUT` / `ERROR` -> **not** automatically fixable in 3H; the
  loop fails immediately, no further Worker attempt, Reviewer never runs.
- Reviewer `APPROVED` -> candidate completion path.
- Reviewer `NEEDS_FIX` -> fixable feedback (budget permitting).
- Reviewer infrastructure/protocol failure -> loop infrastructure failure,
  **never** treated as `NEEDS_FIX`.

Default `max_fix_attempts = 2`, bounded to `[1, 5]`. The **initial**
Worker/Verification/Reviewer pass (the one that produced the `FixTrigger`)
is **not** counted against this budget — the budget only bounds
`FixLoopRunner`'s own attempts.

## Non-goal: initial implementation orchestration

3H does **not** orchestrate the first Worker/Verification/Reviewer pass.
`FixLoopRunner` starts only after a fixable trigger already exists:
`VerificationReport.status == FAIL`, or `PASS` + `ReviewReport.verdict ==
NEEDS_FIX`. The future full Orchestration Engine will own "initial Worker ->
initial Verification -> initial Reviewer -> optional FixLoopRunner"; that
engine is explicitly **not** built in 3H.

## Architectural boundary

`FixLoopRunner` is orchestration ("who/when"), never a second Agent harness
("how"). It depends only on three small ports plus canonical Run
bookkeeping:

```
                    FixLoopRunner
                   /      |       \
          Worker port  Verification port  Reviewer port
                  \
                   ChangeProvider
```

It never touches `ModelBackend`, `AgentSession` internals, `ToolRegistry`
construction, tool-calling loops, `ProcessRunner` internals, the Reviewer's
parser/prompt/context logic, or Git workspace internals directly — those
remain owned by the concrete adapters behind each port (not built in 3H;
3H ships the ports and orchestration only, with test adapters standing in
for the not-yet-built concrete Worker/Verification/Reviewer adapters).

## New package: `change_runtime/` — a port separate from Workspace

`Workspace` stays a filesystem/lifecycle abstraction; `Workspace.diff()` was
deliberately **not** added. Instead:

- `ChangeProvider.capture(workspace) -> WorkspaceChangeSet` — a Protocol.
- `WorkspaceChangeSet(diff, changed_paths)` — immutable; `diff_sha256` is
  **not** an init parameter (`field(init=False)`), so it can never be
  caller-supplied — it is always `SHA256(exact diff UTF-8 bytes)`.
- `GitWorktreeChangeProvider` — the concrete provider for
  `GitWorktreeWorkspace`. Baseline is always
  `workspace.snapshot.snapshot_commit`; the current side is the workspace's
  current working tree. **Every capture is cumulative** (snapshot -> current
  working tree), never a delta against only the previous capture — proven by
  `tests/test_change_runtime.py::test_change_then_revert_to_snapshot_returns_empty_change_set`.

### Change-capture safety

Every `git` invocation in `change_runtime/git.py` uses argv-only
`subprocess.run` (`shell=False`), and:

- `--no-optional-locks` on every call: git's normal `diff`/`status`
  machinery opportunistically rewrites the on-disk index with a refreshed
  stat cache ("racy git") even for read-only commands; this flag suppresses
  that, so the index file is never touched.
- `--no-color --no-ext-diff --no-textconv` plus `-c core.pager=cat` on every
  diff invocation: disables user config (external diff tools, textconv
  filters, pager) that could otherwise execute arbitrary commands or make
  output non-deterministic.
- `GIT_TERMINAL_PROMPT=0`: never blocks on a credential prompt.
- No `git add`/`write-tree`/`commit-tree`/`update-index` anywhere in this
  module — it never stages or commits.
- Untracked files are rendered by this module's **own** deterministic
  renderer (never `git add -N` + diff, never `git diff --no-index`), so no
  extra git subprocess ever touches the index for them.
- Symlinks are represented via `os.readlink()` only — their target is never
  opened, so content outside the workspace can never leak into a review.
- `--relative` is required on the tracked-file `git diff` invocations:
  without it, git reports paths relative to the *repository* root even when
  invoked from a subdirectory, which would leak sibling-directory paths for
  a nested `project_relative_root`. `git ls-files` already defaults to
  cwd-relative paths.

Untracked files get a deterministic Git-style block built directly by this
module: UTF-8 text files get a real unified-diff-style "new file" hunk
(final-newline differences change the exact text and therefore the SHA,
matching git's own `\ No newline at end of file` convention); binary or
non-UTF-8 files get deterministic metadata (`path`, a binary marker, byte
size, and `SHA-256` of the raw bytes) instead of raw bytes ever entering the
textual diff; symlinks get their `readlink()` target recorded as text.

## Change size bound

`ReviewRequest.diff` is already capped at 200,000 characters
(`review_runtime.models`). 3H does **not** truncate a captured diff to fit —
if the cumulative diff exceeds that bound, constructing `ReviewRequest`
raises `ReviewInputError`, which `FixLoopRunner` wraps as a typed
`FixLoopExecutionError` (an infrastructure failure, not a silent
truncation). The diff passed into `ReviewRequest` is always byte-identical
to `WorkspaceChangeSet.diff`, so `ReviewRequest.diff_sha256 ==
WorkspaceChangeSet.diff_sha256` by construction.

## `fix_runtime/` domain model

- `FixTriggerKind`: `VERIFICATION_FAIL` | `REVIEW_NEEDS_FIX`.
- `FixLoopStatus`: `COMPLETED` | `EXHAUSTED` | `FAILED`.
- `FixTrigger` — the two valid shapes are enforced in `__post_init__`:
  `VERIFICATION_FAIL` requires `status == FAIL` and **no** review report;
  `REVIEW_NEEDS_FIX` requires `status == PASS`, a `ReviewReport` with
  `verdict == NEEDS_FIX`, and matching `verification_id`/`verification_status
  == "pass"` — `TIMEOUT`/`ERROR` can never construct a valid trigger.
- `FixWorkerRequest` / `FixLoopRequest` — bounded task/plan text,
  `max_fix_attempts` real-int-in-`[1,5]` (bool explicitly rejected).
  `FixLoopRequest` also carries the `VerificationPlan` to re-run.
- `FixAttemptResult` / `FixLoopReport` — immutable outcomes.
- IDs: `fix_<uuid>` / `fixatt_<uuid>` / `exec_fix_<uuid>`, always freshly
  generated per loop/attempt/execution — never derived from `attempt_index`
  alone.

## Ports

- `WorkerAttemptRunner.run(workspace, FixWorkerRequest, *, execution_id) ->
  WorkerAttemptResult` — one fresh execution per call; the concrete adapter
  must use the supplied `execution_id` and owns recording that execution's
  own canonical `execution.*` lifecycle. No concrete native adapter is built
  in 3H (test adapters only, per the milestone's explicit scope limit).
- `VerificationAttemptRunner.run(workspace, VerificationPlan, *,
  verification_id) -> VerificationReport` — the returned report must use the
  requested id; the adapter owns its own canonical `verification.*` events
  (thin wrapper over the existing `VerificationRunner`).
- `ReviewAttemptRunner.run(workspace, ReviewRequest, *, review_id) ->
  ReviewReport` — thin wrapper over the existing `ReviewerRunner`; 3H does
  not duplicate its parser/prompt/context/read-only-tool-policy logic.

## Trust boundary for fix feedback (`fix_runtime/prompt.py`)

`render_fix_worker_input` renders: `ORIGINAL USER TASK` (trusted
requirement, always present in full), `ATTEMPT INFO` (small fixed runtime
metadata), `GENERATED PLAN` and `FIX FEEDBACK` (both explicitly untrusted —
deterministic verification stdout/stderr and Reviewer summary/findings can
contain adversarial text like "ignore the task" or "delete all files" and
must never be treated as instructions). The implementation diff is
deliberately **not** included by default — the Worker has workspace tools
and should inspect the current workspace itself. The renderer proves the
same mathematical budget invariant established for the Reviewer's prompt in
3G-hardening: `len(rendered) <= MAX_FIX_INPUT_CHARS` for every accepted
input, task always kept in full, only `GENERATED PLAN`/`FIX FEEDBACK` bounded.

## Canonical fix-loop events (`run_runtime/fix_loop.py`)

New `RunEventType` values: `fix_loop.started`, `fix_attempt.started`,
`fix_attempt.completed`, `fix_attempt.interrupted`, `fix_loop.completed`,
`fix_loop.exhausted`, `fix_loop.failed`, `fix_loop.interrupted`.
`CanonicalFixLoopRecorder` owns **only** these events — it never writes
`execution.*`/`verification.*`/`review.*` (those stay owned by the
Worker/Verification/Reviewer adapters' own sinks, exactly as in 3G). Every
fix-loop-authored `RunEvent` has `execution_id=None`, `correlation_id ==
fix_loop_id`, `source="fix_loop"`; `worker_execution_id` lives only inside
`fix_attempt.*` payloads, never as the top-level `RunEvent.execution_id`.

**Sequence ownership**: unlike a sink that's used exactly once per attempt,
`CanonicalFixLoopRecorder` surrounds *other* canonical producers
(`fix_attempt.started` -> Worker writes `execution.*` -> `fix_attempt.
completed` -> Verification writes `verification.*` -> Reviewer writes
`review.*`). It therefore does **not** hold one `expected_last_event_seq`
across the whole loop — every method re-reads the RUNNING Run's current
`last_event_seq` immediately before its own append. A genuine concurrent
race still surfaces `EventSequenceError` unmodified (never swallowed or
retried).

The recorder validates: `fix_loop.started` is required before any attempt;
attempt indices are strictly monotonic (`1, 2, ...`); no duplicate active
attempt; no `fix_attempt.completed` before its own `fix_attempt.started`; at
most one terminal loop outcome; no fix event after that terminal;
`fix_loop_id` reuse across a fresh recorder instance is rejected (same
process-local-plus-canonical-reuse-guard discipline established for
`CanonicalReviewEventSink` in 3G hardening).

## `FixLoopRunner` algorithm

1. Capture the current cumulative `WorkspaceChangeSet` (before anything is
   committed). A `REVIEW_NEEDS_FIX` trigger must reference **exactly** that
   current diff SHA — checked and rejected with `FixLoopInputError` before
   `fix_loop.started` is ever committed, so the loop never acts on known-
   stale reviewer feedback.
2. Commit `fix_loop.started`.
3. For `attempt_index` in `1..max_fix_attempts`:
   - Capture `before`. Generate a fresh `fix_attempt_id` and
     `execution_id`. Commit `fix_attempt.started` **before** calling the
     Worker port (never after — the Worker's side effect must never precede
     its own canonical start record).
   - Call the Worker port exactly once; require the returned
     `execution_id` matches the requested one; require canonical evidence
     that this exact `execution_id` ended in `execution.completed` (not
     merely `execution.started`, and not `execution.failed`).
   - Capture `after`; commit `fix_attempt.completed` with `changed =
     (after.diff_sha256 != before.diff_sha256)`.
   - If unchanged: `fix_loop.exhausted(reason="stalled")` ->
     `RunCompletionGate.fail_fix_loop` -> return `EXHAUSTED`. No
     Verification, no Reviewer, no second Worker attempt.
   - Run Verification once with a fresh `verification_id`; require the
     returned id matches.
     - `ERROR` -> `fix_loop.failed(reason="verification_error")` -> gate ->
       `FAILED`. No Reviewer, no further Worker attempt.
     - `TIMEOUT` -> same, `reason="verification_timeout"`.
     - `FAIL` -> if this was the last attempt: `fix_loop.exhausted(reason=
       "budget_exhausted")` -> gate -> `EXHAUSTED`. Otherwise the next
       trigger becomes `VERIFICATION_FAIL(report)` and the loop continues —
       Reviewer never runs after a `FAIL`.
     - `PASS` -> re-capture the **cumulative** diff (not a per-attempt
       delta), build `ReviewRequest`, run Reviewer once with a fresh
       `review_id`; validate `review_id`/`verification_id`/
       `verification_status`/`diff_sha256` all match what was requested —
       any mismatch is an infrastructure failure, never `NEEDS_FIX`.
       - `NEEDS_FIX` -> if last attempt: exhaust (`budget_exhausted`).
         Otherwise the next trigger becomes `REVIEW_NEEDS_FIX(verification,
         review)` and the loop continues.
       - `APPROVED` -> re-capture once more and require
         `final_changes.diff_sha256 == review_report.diff_sha256` (the
         Reviewer is read-only, but the workspace could still have mutated
         between the diff it reviewed and its return). Mismatch ->
         `fix_loop.failed(reason="workspace_changed_after_review")` -> gate
         -> `FAILED`, never `run.completed`. Match -> `fix_loop.completed`
         -> `RunCompletionGate.complete_reviewed(...)` -> return
         `COMPLETED`. `FixLoopRunner` itself never appends `run.completed`.
4. The for-loop always returns from inside one of the branches above; a
   defensive `FixLoopExecutionError` guards the (unreachable) fall-through.

Infrastructure failures (Worker/Verification/Reviewer port exceptions,
provenance mismatches, missing canonical execution evidence, `ChangeProvider`
failures) are caught, translated to `FixLoopExecutionError` with `__cause__`
preserved, best-effort recorded as `fix_loop.failed(reason=
"infrastructure_error", ...)` and routed through `fail_fix_loop` if that
recording succeeds — but `EventSequenceError` from the recorder itself is
**never** swallowed; it propagates raw instead of inventing more terminal
evidence.

## `RunCompletionGate` additions (`run_runtime/completion.py`)

`complete_verified()` is unchanged and remains backward compatible.
`complete_reviewed(run_id, *, verification_id, review_id,
current_diff_sha256)` validates 14 conditions against one stable canonical
prefix (mirroring `complete_verified`'s pattern): requested verification is
the latest attempt with a `PASS` terminal, prior execution settled
successfully with no newer `execution.*` activity since verification
started (the 3G invariant — Reviewer/fix-loop events never count as
execution activity — is unchanged, since these checks only ever scan
`execution.started/completed/failed`), requested review is the latest
attempt, started *after* the successful verification terminal, terminated
`APPROVED`, and its payload's `verification_id`/`verification_status`/
`diff_sha256` all match. Only then does it append `run.completed` with
`reason="reviewed"`.

`fail_fix_loop(run_id, *, fix_loop_id)` may fail a RUNNING Run only when the
*latest* fix-loop attempt's terminal is `fix_loop.exhausted` or
`fix_loop.failed` — `fix_loop.completed`, `fix_loop.interrupted`, and a
non-terminal loop are all rejected, and newer `execution.*` activity after
the fix-loop terminal makes the evidence stale. `ExecutionFailed !=
RunFailed`, `VerificationFAIL != RunFailed`, `ReviewerNEEDS_FIX !=
RunFailed`, and now `FixLoopTerminal != RunFailed` — until this explicit
gate method owns the write.

## Crash recovery (`run_runtime/recovery.py`)

No automatic resume, no Worker rerun. Nested unfinished domain work
(tool/verification/review) is settled first using the existing 3G rules.
Then: every `fix_attempt.started` without a `fix_attempt.completed`/
`fix_attempt.interrupted` gets `fix_attempt.interrupted` (payload includes
`fix_loop_id`, `fix_attempt_id`, `attempt_index`, `worker_execution_id`,
`reason="process_restart"`, `outcome_unknown=true`). Then, if `fix_loop.
started` has no terminal, `fix_loop.interrupted` is appended. Then the
existing `run.interrupted` follows. Ordering is strictly: nested
tool/verification/review interruption < `fix_attempt.interrupted` <
`fix_loop.interrupted` < `run.interrupted`. Recovery remains idempotent (a
second scan finds nothing left to interrupt).

## Explicitly deferred (not in 3H)

Planner, adaptive routing, external ACP/Claude-CLI/Codex-CLI executors, a
generic executor marketplace, DAG/fan-out/parallel agents, multi-worker
voting, flaky-test retry, dynamic verification plans, automatic fix-loop
resume, leases/heartbeats, distributed execution ownership, UI/frontend
wiring, legacy `project_runner.py` migration, containers, embeddings, LSP,
MCP, ATIF, a scheduler. Also deferred: the concrete native
`WorkerAttemptRunner`/`VerificationAttemptRunner`/`ReviewAttemptRunner`
adapters themselves (3H ships the ports and orchestration; wiring them to
the real `AgentSession`/`VerificationRunner`/`ReviewerRunner` belongs to the
milestone that also builds the full initial-implementation Orchestration
Engine, per the non-goal in section 3).
