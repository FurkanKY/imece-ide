# Milestone 3J1 — Native Attempt Adapters (implementation plan)

See `docs/superpowers/specs/2026-08-27-native-attempt-adapters-design.md`
for the approved architecture. This plan tracks the actual files touched
against the real repository state at `milestone-3i` (`521e2975`).

## New package: `executor_runtime/`

- `errors.py` — `ExecutorAdapterError`, `ExecutorAdapterInputError`,
  `ExecutorAdapterExecutionError`.
- `native_worker.py` — `NativeWorkerAttemptAdapter`,
  `NATIVE_FIX_WORKER_SYSTEM_INSTRUCTIONS`, private `_worker_policy`/
  `_worker_registry` helpers.
- `native_verification.py` — `NativeVerificationAttemptAdapter`.
- `native_reviewer.py` — `NativeReviewAttemptAdapter`.
- `__init__.py` — public re-exports only.

## Not modified (no blocker found)

`fix_runtime/ports.py`, `fix_runtime/runner.py`, `agent_runtime/*`,
`review_runtime/*`, `verification_runtime/*`, `process_runtime/*`,
`run_runtime/native_agent.py`, `run_runtime/verification.py`,
`run_runtime/reviewer.py`, `run_runtime/completion.py`, `workspace/*`,
`tool_runtime/*`, `planner_runtime/*`, `change_runtime/*`, legacy
`adapters.py`/`providers.py`, UI.

## Tests

New:
- `tests/test_native_worker_adapter.py` — constructor `run_id` property;
  non-`FixWorkerRequest`/invalid `execution_id`/`LocalWorkspace` all
  rejected before `backend.open_session`/`execution.started`/mutation;
  `GitWorktreeWorkspace` accepted; backend sees exactly the seven tool
  definitions (`read_file`, `list_files`, `search_text`, `write_file`,
  `delete_path`, `repo_map`, `search_code`) and no `run_process`; ALLOW
  policy never produces ASK for read/list/search/edit/delete; exact
  `request.rendered_input` reaches the first `AgentSession` `UserInput`
  byte-for-byte; system instructions do not alter `rendered_input`; supplied
  `execution_id` used exactly and returned; canonical
  `execution.started`/`execution.completed` exist for that exact id with no
  manually-duplicated lifecycle; fresh `AgentSession` per call;
  `AgentRuntimeError` -> `ExecutorAdapterExecutionError` with `__cause__`
  (and no `WorkerAttemptResult` returned); `ApprovalPause` ->
  `ExecutorAdapterExecutionError`, not resumed, no `run.waiting_user`;
  adapter never touches `ProcessRunner`; a real-mutation test using a real
  temporary Git repo + `GitWorktreeWorkspace` and a scripted `write_file`
  tool call proves the source repo worktree is untouched while the shadow
  worktree is mutated.
- `tests/test_native_verification_adapter.py` — `run_id` property;
  non-`VerificationPlan` rejected before process execution; exact
  `verification_id` forwarded to `CanonicalVerificationEventSink` and to
  the fake `ProcessRunner`'s `ProcessRequest`(s); `PASS`/`FAIL`/`TIMEOUT`/
  `ERROR` reports all return normally (no exception); a returned report
  with a mismatched `verification_id` fails closed; no `RunCompletionGate`
  import/call; no retry; canonical `verification.started`/
  `verification.completed` exist; adapter never emits `execution.*`.
- `tests/test_native_review_adapter.py` — `run_id` property;
  non-`ReviewRequest` rejected before `ReviewerRunner.run`; exact
  `review_id`/workspace/request forwarded; `CanonicalReviewEventSink` passed
  as `recorder`; `APPROVED`/`NEEDS_FIX` both return normally; a returned
  report with a mismatched `review_id` fails closed; a reviewer
  infrastructure/protocol exception is wrapped with `__cause__`; no retry;
  no `RunCompletionGate`; canonical review lifecycle keeps
  `execution_id=None`; the adapter builds no prompt/context itself — one
  end-to-end test uses a real `ReviewerRunner` + a scripted `ModelBackend`.
- `tests/test_native_attempt_adapters_integration.py` — a real `RunRuntime`
  + real `GitWorktreeWorkspace` + real `NativeWorkerAttemptAdapter`/
  `NativeVerificationAttemptAdapter`/`NativeReviewAttemptAdapter` supplied,
  unmodified, to a real `FixLoopRunner`; a scripted backend Worker turn
  edits a file, a fake `ProcessRunner`-backed verification plan returns
  PASS, a scripted reviewer backend returns APPROVED; asserts
  `FixLoopReport.status == COMPLETED` and the exact canonical id/ordering
  contract from the design doc (Worker execution id on
  `execution.started`/`execution.completed`, verification id on
  `verification.*`, `execution_id is None` on `review.*`, no adapter-
  produced `run.waiting_user`, no duplicated terminal lifecycle). Uses the
  real `GitWorktreeChangeProvider` (already covered independently by
  `tests/test_change_runtime.py`) unless it proves to obscure the adapter
  integration, in which case a minimal deterministic `ChangeProvider` test
  double is used instead — the assertions above do not change either way.

## Explicitly out of scope for this milestone

No `RoutingPolicy`, no ACP/CLI/subprocess executor, no generic
`TaskExecutor` abstraction, no orchestration of
Planner → Worker → Verification → Reviewer → FixLoop, no
`fix_runtime/ports.py`/`fix_runtime/runner.py` changes, no legacy
`project_runner.py`/`agents.py`/UI wiring, no new dependencies.
