# Milestone 3G — Native Semantic Reviewer (implementation plan)

See `docs/superpowers/specs/2026-08-25-native-semantic-reviewer-design.md`
for the approved architecture. This plan tracks the actual files touched
against the real repository state at `milestone-3f` (`1069a55b`).

## New package: `review_runtime/`

- `errors.py` — `ReviewRuntimeError`, `ReviewInputError`,
  `ReviewProtocolError`, `ReviewExecutionError`, `ReviewRecordingError`.
- `models.py` — `ReviewVerdict`, `ReviewSeverity`, `ReviewFinding`,
  `ReviewDecision`, `ReviewReport`, `ReviewRequest`, `new_review_id`,
  `validate_review_id`.
- `parser.py` — `parse_review_decision(text) -> ReviewDecision`, strict JSON
  contract (`MAX_MODEL_OUTPUT_CHARS`).
- `prompt.py` — `REVIEWER_SYSTEM_INSTRUCTIONS`,
  `render_initial_review_input(...)`.
- `recording.py` — `ReviewRecorder` protocol, `NullReviewRecorder`.
- `runner.py` — `ReviewerRunner`.
- `__init__.py` — public re-exports.

## Modified: `tool_runtime/`

- `tool_runtime/tools/workspace_files.py` — extract
  `register_workspace_read_tools(registry)` (read_file/list_files/
  search_text only); `register_workspace_tools` now calls it and adds
  write_file/delete_path. Schemas/order unchanged (verified against
  `tests/test_workspace_tools.py`).
- `tool_runtime/tools/__init__.py` — export
  `register_workspace_read_tools`.

## Modified: `run_runtime/`

- `events.py` — add `REVIEW_STARTED`, `REVIEW_FAILED`,
  `REVIEW_INTERRUPTED` to `RunEventType` (existing `REVIEW_COMPLETED`
  string unchanged).
- `reviewer.py` (new) — `CanonicalReviewEventSink` (canonical bridge; see
  design doc for the full event mapping table and the
  `ExecutionCompleted`-is-not-`review.completed` rule).
- `readmodels.py` — `_review_receipt(events)` helper; `build_receipt` and
  `build_history_item` both use it instead of a bare
  `_latest(events, REVIEW_COMPLETED)` lookup.
- `recovery.py` — `recover_running_runs` gains a review-attempt pass
  (mirrors the existing verification-attempt pass) that appends
  `review.interrupted` for any unfinished `review.started` before the
  enclosing `run.interrupted`.
- `__init__.py` — export `CanonicalReviewEventSink`.
- `completion.py` — **not modified** (regression-tested only: Reviewer
  canonical events must not affect `RunCompletionGate.complete_verified`).

## Tests

New:
- `tests/test_review_models.py` — `ReviewFinding`/`ReviewDecision`/
  `ReviewReport`/`ReviewRequest` invariants, bounds, `diff_sha256`.
- `tests/test_review_parser.py` — strict JSON contract matrix (duplicate
  keys, NaN/Infinity, unknown keys, markdown fences, prose, domain-invalid
  combinations).
- `tests/test_reviewer_runner.py` — `ReviewerRunner` against a scripted
  `ModelBackend`: direct APPROVED/NEEDS_FIX, tool-assisted review, malformed
  output, backend failure, read-only tool surface, fail-closed policy,
  provenance (`diff_sha256`, `repository_fingerprint`), prompt-injection
  trust-boundary rendering.
- `tests/test_review_bridge.py` — `CanonicalReviewEventSink`: event mapping,
  `execution_id=None` on every reviewer event, `ExecutionCompleted` does not
  persist `review.completed`, `ExecutionFailed` -> `review.failed`, review_id
  reuse rejected, duplicate terminal rejected, `RunCompletionGate` regression
  (reviewer activity does not make verification stale).

Extended:
- `tests/test_run_readmodels.py` — latest-attempt review verdict semantics
  (legacy-only, running, failed, interrupted, completed, stale-attempt
  non-leak), `build_history_item` parity.
- `tests/test_run_recovery.py` — unfinished review interrupted, unfinished
  reviewer tool interrupted, completed review left alone, idempotent rerun.
- `tests/test_run_completion.py` — full worker -> verification -> reviewer
  canonical sequence still completes via `complete_verified`.
- `tests/test_workspace_tools.py` — `register_workspace_read_tools` exposes
  exactly the three read-only tools; `register_workspace_tools` behavior is
  unchanged.

## Explicitly out of scope for this milestone

No Fix Loop, no `RunCompletionGate` change to require `APPROVED`, no
provider-native structured outputs, no `AgentSession`/`ModelBackend`/
`VerificationRuntime`/`ProcessRuntime`/frontend changes, no new
dependencies.
