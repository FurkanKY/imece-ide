# Milestone 3H — Bounded Fix Loop (implementation plan)

See `docs/superpowers/specs/2026-08-25-bounded-fix-loop-design.md` for the
approved architecture. This plan tracks the actual files touched against
the real repository state at `milestone-3g` (`d1dedd8e`).

## New package: `change_runtime/`

- `errors.py` — `ChangeRuntimeError`, `ChangeInputError`, `ChangeCaptureError`.
- `models.py` — `WorkspaceChangeSet` (`diff_sha256` is `init=False`).
- `provider.py` — `ChangeProvider` Protocol.
- `git.py` — `GitWorktreeChangeProvider` (see design doc for the full safety
  list: `--no-optional-locks`, `--no-ext-diff`, `--no-textconv`, `--relative`,
  no index mutation, no symlink dereference, deterministic untracked-file
  rendering).
- `__init__.py` — public re-exports.

## New package: `fix_runtime/`

- `errors.py` — `FixLoopRuntimeError`, `FixLoopInputError`,
  `FixLoopExecutionError`, `FixLoopRecordingError`.
- `models.py` — `FixTriggerKind`, `FixLoopStatus`, `FixTrigger`,
  `FixWorkerRequest`, `FixLoopRequest`, `FixAttemptResult`, `FixLoopReport`,
  id generators/validators.
- `ports.py` — `WorkerAttemptResult`, `WorkerAttemptRunner`,
  `VerificationAttemptRunner`, `ReviewAttemptRunner` (Protocols).
- `prompt.py` — `render_fix_worker_input`, `MAX_FIX_INPUT_CHARS`.
- `runner.py` — `FixLoopRunner`.
- `__init__.py` — public re-exports.

## New: `run_runtime/fix_loop.py`

`CanonicalFixLoopRecorder` — deliberately self-contained (imports only from
`run_runtime.*`, never from `fix_runtime`, to avoid a circular package
import: `fix_runtime.runner` needs `run_runtime.completion`/`service`/
`fix_loop`, so `run_runtime.fix_loop` cannot import back from
`fix_runtime`). Its own tiny bounded-ID validator is duplicated locally
rather than imported (same pattern already used independently by
`review_runtime`/`verification_runtime`). `FixLoopRecordingError` is defined
in `run_runtime/errors.py` (this module raises it) — `fix_runtime/errors.py`
keeps its own same-named class for the `fix_runtime`-side hierarchy; they
are intentionally not the same class since the packages must not depend on
each other in that direction.

## Modified: `run_runtime/`

- `events.py` — add `FIX_LOOP_STARTED`, `FIX_ATTEMPT_STARTED`,
  `FIX_ATTEMPT_COMPLETED`, `FIX_ATTEMPT_INTERRUPTED`, `FIX_LOOP_COMPLETED`,
  `FIX_LOOP_EXHAUSTED`, `FIX_LOOP_FAILED`, `FIX_LOOP_INTERRUPTED`.
- `errors.py` — add `FixLoopRecordingError`.
- `completion.py` — add `complete_reviewed(...)` and `fail_fix_loop(...)`;
  `complete_verified()` unchanged.
- `recovery.py` — add the fix-attempt/fix-loop interruption pass, ordered
  after the existing tool/verification/review passes and before the final
  `run.interrupted` write.
- `__init__.py` — export `CanonicalFixLoopRecorder`.

## Tests

New:
- `tests/test_change_runtime.py` — full `GitWorktreeChangeProvider` matrix
  (clean/modified/staged/deleted/untracked/ignored/nested-root/newline/
  binary/symlink/index-safety/idempotence/revert-to-empty/no-shell-True).
- `tests/test_fix_models.py` — `FixTrigger`/`FixWorkerRequest`/
  `FixLoopRequest`/`FixLoopReport` invariants, ID generators.
- `tests/test_fix_prompt.py` — budget invariant + trust-boundary rendering
  (injection strings in verification stdout/stderr, reviewer summary/
  finding, generated plan all render after the `FIX FEEDBACK` header, task
  never contains them).
- `tests/test_fix_loop_bridge.py` — `CanonicalFixLoopRecorder`: start/
  attempt ordering/terminal-uniqueness/`execution_id=None`/reacquired-
  sequence-tolerates-interleaved-events/`EventSequenceError` surfaces.
- `tests/test_fix_loop_runner.py` — `FixLoopRunner` algorithm against fake
  Worker/Verification/Reviewer/ChangeProvider ports: stall, verification-FAIL
  recovery, review-NEEDS_FIX recovery, budget exhaustion (both trigger
  kinds), TIMEOUT/ERROR routing, post-review workspace-mutation staleness,
  `max_fix_attempts=1` off-by-one, stale initial trigger rejection, and
  infrastructure-failure translation (wrong execution_id, missing
  `execution.completed`, port exceptions, review provenance mismatch,
  `ChangeProvider` failure).

Extended:
- `tests/test_run_completion.py` — full `complete_reviewed`/`fail_fix_loop`
  matrix (happy paths, every rejection case from the design doc's 14-point
  list, malformed SHA, optimistic-race), plus the milestone's two CRITICAL
  integration tests (stale verification/review rejected after a fix attempt
  produces new evidence; workspace-SHA mismatch after Reviewer approval
  rejected both at the gate and inside `FixLoopRunner` itself).
- `tests/test_run_recovery.py` — unfinished fix attempt/loop interruption,
  ordering against nested tool/verification interruption, completed/
  exhausted/failed loops left alone, idempotent rerun.

## Explicitly out of scope for this milestone

No concrete native Worker/Verification/Reviewer adapters (ports + test
adapters only — see design doc's non-goal section), no initial-
implementation orchestration engine, no `RunCompletionGate` change to
require the fix loop for every Run, no UI, no legacy `project_runner.py`
wiring, no new dependencies.
