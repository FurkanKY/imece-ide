# Milestone 3I — Native Planner Runtime (implementation plan)

See `docs/superpowers/specs/2026-08-26-native-planner-design.md` for the
approved architecture. This plan tracks the actual files touched against
the real repository state at `milestone-3h` (`9e000ddc`).

## New package: `planner_runtime/`

- `errors.py` — `PlannerError`, `PlannerInputError`, `PlannerExecutionError`,
  `PlannerProtocolError`, `PlannerRecordingError`.
- `models.py` — `TaskComplexity`, `TaskScope`, `PlanStep`, `TaskProfile`,
  `ParsedPlanDecision`, `PlanReport`, `new_plan_id`, `validate_plan_id`, all
  domain bound constants.
- `parser.py` — `parse_plan_decision`, `MAX_MODEL_OUTPUT_CHARS`.
- `prompt.py` — `PLANNER_SYSTEM_INSTRUCTIONS`, `render_initial_planner_input`,
  `MAX_INITIAL_PLANNER_INPUT_CHARS`.
- `recording.py` — `PlanRecorder` Protocol, `NullPlanRecorder`.
- `runner.py` — `PlannerRunner`.
- `__init__.py` — public re-exports.

## New: `run_runtime/planner.py`

`CanonicalPlannerEventSink` — mirrors `run_runtime/reviewer.py`'s
`CanonicalReviewEventSink` field-for-field (not
`CanonicalAgentEventSink`/`CanonicalFixLoopRecorder`'s shape). Imports from
`planner_runtime.errors`/`models`/`parser` only — no circular import risk,
since `planner_runtime` never imports `run_runtime` (unlike the 3H
`fix_runtime`/`run_runtime.fix_loop` case, which needed a self-contained
recorder specifically to avoid one).

## Modified: `run_runtime/`

- `events.py` — add `PLAN_STARTED`, `PLAN_FAILED`, `PLAN_INTERRUPTED` next
  to the existing `PLAN_COMPLETED` (untouched, not renamed/removed).
- `recovery.py` — add the plan-attempt interruption pass, ordered after the
  existing tool-interruption pass and before the verification pass.
- `__init__.py` — export `CanonicalPlannerEventSink`.

Not modified (no blocker found): `run_runtime/completion.py`,
`run_runtime/legacy.py`, `run_runtime/readmodels.py`, `agent_runtime/*`,
`context_runtime/*`, `tool_runtime/*`, `review_runtime/*`,
`verification_runtime/*`, `fix_runtime/*`, `change_runtime/*`,
`workspace/*`, `project_runner.py`, `agents.py`, `webhost/`, `frontend/`.

## Tests

New:
- `tests/test_planner_models.py` — `PlanStep`/`TaskProfile`/
  `ParsedPlanDecision`/`PlanReport` bounds, frozen/immutable behavior, ID
  generation/validation, `task_sha256` shape.
- `tests/test_planner_parser.py` — full strict-JSON rejection matrix
  (fences, prose, duplicate keys, NaN/Infinity, top-level array, unknown/
  missing/wrong-type keys, invalid enums, empty/over-count/oversized
  fields, oversized output, model-forged runtime fields, routing fields).
- `tests/test_planner_prompt.py` — budget invariant (mirrors
  `test_review_prompt.py`'s proof technique) + trust-boundary rendering
  (malicious repository context confined to the `UNTRUSTED DATA` section,
  task always intact, output contract explicit, system-instruction
  read-only/no-routing/no-Markdown assertions).
- `tests/test_planner_runner.py` — `PlannerRunner` algorithm against a
  scripted `ModelBackend` (mirrors `test_reviewer_runner.py`): valid plans,
  plan_id supplied/generated, `task_sha256`/`repository_fingerprint`/
  `task_profile` provenance, tool-assisted planning, read-only tool
  surface/fail-closed policy, malformed-output/backend-failure/refusal
  typed errors, task/plan_id validation before any Agent side effect, fresh
  transient execution id, no retries, trust-boundary rendering reaches
  `AgentSession` verbatim, recorder wiring (`NullPlanRecorder` default,
  `emit`/`fail` call proof).
- `tests/test_planner_bridge.py` — `CanonicalPlannerEventSink` direct
  event-mapping, plan_id validation/reuse rejection, terminal-uniqueness-is-
  canonical hardening (mirrors the 3G/3H hardening lessons), oversized
  `model.completed` bounding with an end-to-end proof that the actual
  parser still sees and rejects the untruncated output, transient lifecycle
  ordering guards, `plan.completed`/`plan.failed` never terminating the Run,
  Planner activity never counted as execution activity by
  `RunCompletionGate`, no canonical `execution.*` ever emitted. Also proves
  the single `task_sha256` authority: `plan.started` payload is exactly
  `{"plan_id": ...}` (no `task_sha256` key at all), the sink constructor
  raises `TypeError` if a caller tries to pass `task_sha256=...`, and an
  end-to-end run proves `hashlib.sha256(task) == PlanReport.task_sha256 ==
  plan.completed.payload["task_sha256"]`.

Extended:
- `tests/test_run_recovery.py` — unfinished `plan.started` ->
  `plan.interrupted`; completed/failed/already-interrupted plans left
  alone; two plan attempts (only the unfinished one settled); tool+plan
  ordering; idempotent repeated scan; no Planner rerun.

## Explicitly out of scope for this milestone

No orchestration of Planner -> Worker -> Verification -> Reviewer ->
FixLoop, no concrete `RoutingPolicy` consuming `TaskProfile`, no
`VerificationPlan` production from plan output, no `RunCompletionGate`
changes, no legacy `project_runner.py`/`agents.py`/UI wiring, no new
dependencies.
