# ACP Worker Attempt Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Connect the existing synchronous WorkerAttemptRunner port to one fresh ACP
client execution while preserving the exact prompt, exact child environment,
Git worktree isolation, canonical Run history, FixLoop ordering, and typed ACP
failure semantics defined in the companion design.

## Architecture

~~~text
FixLoopRunner
    -> WorkerAttemptRunner.run(...)
        -> AcpWorkerAttemptAdapter
            -> CanonicalAcpEventSink
            -> AcpClientRuntime.run(...)
                -> official ACP SDK over local stdio
~~~

executor_runtime.acp_worker owns launch resolution, local validation, the
sync/async bridge, and adapter error translation. run_runtime.acp owns the
one-execution canonical sink. acp_runtime remains unaware of canonical Run
and FixLoop types. FixLoopRunner, fix_runtime.ports, routing, and executor
selection are not modified.

## Tech Stack

- Python 3.12;
- the existing acp_runtime ACP SDK bridge and AcpClientRuntime;
- RunRuntime, RunEventSpec, strict canonical JSON validation, and optimistic
  event sequencing;
- GitWorktreeWorkspace;
- pytest with plugin autoload disabled for verification;
- the existing official-SDK fake-agent fixture for real stdio integration.

## Spec

The companion design document is the normative interface and behavior source:

docs/superpowers/specs/2026-08-29-acp-worker-attempt-adapter-design.md

The implementation must preserve these interfaces:

~~~python
# fix_runtime.ports
WorkerAttemptRunner.run(workspace, request, *, execution_id) -> WorkerAttemptResult

# executor_runtime.acp_worker
AcpWorkerLaunchProfile(command, args=(), env={})
AcpWorkerAttemptAdapter(runtime, run_id, launch_profile, acp_client, *, limits=None)

# run_runtime.acp
CanonicalAcpEventSink(runtime, run_id, *, execution_id)
sink.start(task: str)
sink.emit(acp_event)
sink.complete(acp_result)
sink.fail(acp_error)
~~~

The exact environment is the profile mapping, the exact prompt is
FixWorkerRequest.rendered_input, the cwd is str(GitWorktreeWorkspace.root),
and every canonical ACP event has source="acp_worker", the supplied
execution_id, and correlation_id=execution_id.

## Global Constraints

- Strict RED -> GREEN TDD for every task.
- No implementation starts until its named regression is observed failing for
  the intended reason.
- No os.environ merge into AcpLaunchSpec.env.
- No provider-specific launch or authentication behavior.
- No ACP Client Core redesign and no dependency from acp_runtime to
  run_runtime, executor_runtime, or fix_runtime.
- No modification to FixLoopRunner, WorkerAttemptRunner, or routing unless a
  test demonstrates an unavoidable existing contract defect; if that occurs,
  stop and report it before editing.
- No native model, turn, tool, or usage events inferred from ACP updates.
- No broad exception or BaseException translation that changes
  CancelledError, KeyboardInterrupt, or SystemExit behavior.
- No mutable per-run state on the adapter; the canonical sink is
  one-execution only.
- No hand-written JSON-RPC framing in tests; extend the current fake-agent
  fixture and use the official SDK.
- No commit, tag, push, or intermediate checkpoint is part of this plan.

## Task 1: Launch profile, resolution, and invariants

**Files**

- Create executor_runtime/acp_worker.py with the launch-profile model and
  isolated resolution helper only.
- Modify executor_runtime/__init__.py only to export the new public types if
  the current export convention requires it.
- Create tests/test_acp_worker.py launch-profile tests.

**Interfaces consumed**

- acp_runtime.models.AcpLaunchSpec;
- executor_runtime.errors.ExecutorAdapterInputError;
- host-side shutil.which only for relative command discovery.

**Interfaces produced**

- frozen AcpWorkerLaunchProfile with tuple args and immutable copied env;
- resolve_acp_worker_launch(profile) -> AcpLaunchSpec.

**RED test**

Add and run:

- test_absolute_command_is_accepted_and_validated;
- test_relative_command_resolves_once_with_shutil_which;
- test_missing_command_raises_input_error;
- test_profile_preserves_args_in_order_and_exactly;
- test_profile_freezes_caller_args_and_env_mutation;
- test_profile_does_not_merge_host_environment;
- test_invalid_profile_values_are_input_errors.

Command:

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'profile or launch or command' -q
~~~

Expected RED is an import failure because the new module and types do not yet
exist. Do not add adapter behavior to make this first failure pass.

**Minimal implementation**

Validate and defensively copy command, args, and env. For an absolute command,
validate the exact executable path without PATH search. For a relative command,
call shutil.which exactly once, require an absolute executable result, and
build AcpLaunchSpec(argv=(resolved, *args), env=env). Keep the returned
environment equal to the profile mapping, including the empty mapping case.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'profile or launch or command' -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_models.py tests/test_acp_client.py -k 'env or argv or exact_child_environment' -q
~~~

## Task 2: Canonical ACP execution lifecycle sink

**Files**

- Create run_runtime/acp.py.
- Extend tests/test_acp_worker.py with sink lifecycle tests.
- Add tests/test_run_service.py coverage only if a current RunRuntime seam is
  needed to demonstrate the optimistic append contract.

**Interfaces consumed**

- RunRuntime.get_run, RunRuntime.record_many, RunEventSpec, and RunEventType;
- RunStatus.RUNNING;
- AcpRunResult, AcpEventSink, and the ACP transient event union;
- run_runtime.jsonutil canonical JSON validation.

**Interfaces produced**

- CanonicalAcpEventSink(runtime, run_id, *, execution_id);
- explicit start, emit, complete, and fail lifecycle operations;
- source="acp_worker" and execution/correlation identity on every spec.

**RED test**

Add and run:

- test_sink_requires_running_run;
- test_sink_start_records_exactly_one_execution_started;
- test_execution_started_payload_is_exact_transport_and_task;
- test_sink_rejects_duplicate_start_and_terminal_events;
- test_sink_uses_supplied_execution_id_on_every_event;
- test_first_transient_event_binds_session_id;
- test_foreign_later_session_id_is_rejected;
- test_completion_binds_session_when_no_updates_occurred;
- test_completion_rejects_result_session_mismatch;
- test_sink_never_refreshes_after_event_sequence_conflict.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_sink_requires_running_run \
  tests/test_acp_worker.py::test_sink_start_records_exactly_one_execution_started \
  tests/test_acp_worker.py::test_execution_started_payload_is_exact_transport_and_task \
  tests/test_acp_worker.py::test_sink_rejects_duplicate_start_and_terminal_events \
  tests/test_acp_worker.py::test_sink_uses_supplied_execution_id_on_every_event \
  tests/test_acp_worker.py::test_first_transient_event_binds_session_id \
  tests/test_acp_worker.py::test_foreign_later_session_id_is_rejected \
  tests/test_acp_worker.py::test_completion_binds_session_when_no_updates_occurred \
  tests/test_acp_worker.py::test_completion_rejects_result_session_mismatch \
  tests/test_acp_worker.py::test_sink_never_refreshes_after_event_sequence_conflict \
  -q
~~~

Expected RED is the missing run_runtime.acp module or sink symbol.

**Minimal implementation**

Capture the current sequence only after verifying RUNNING. Make start(task:
str) record exactly {"transport": "acp", "task": task}; the adapter passes
request.task and no prompt or launch facts. Start with bound_session_id=None,
bind the first transient session ID, require every later transient ID to
match, bind completion to result.session_id when no update bound it, and
reject a completion mismatch. Record start and terminal events through
record_many or record with the captured expected sequence, advancing only
after success. Do not generate IDs, refresh after EventSequenceError, or
retry a failed append. sink.fail() never invents a session ID.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_sink_requires_running_run \
  tests/test_acp_worker.py::test_sink_start_records_exactly_one_execution_started \
  tests/test_acp_worker.py::test_execution_started_payload_is_exact_transport_and_task \
  tests/test_acp_worker.py::test_sink_rejects_duplicate_start_and_terminal_events \
  tests/test_acp_worker.py::test_sink_uses_supplied_execution_id_on_every_event \
  tests/test_acp_worker.py::test_first_transient_event_binds_session_id \
  tests/test_acp_worker.py::test_foreign_later_session_id_is_rejected \
  tests/test_acp_worker.py::test_completion_binds_session_when_no_updates_occurred \
  tests/test_acp_worker.py::test_completion_rejects_result_session_mismatch \
  tests/test_acp_worker.py::test_sink_never_refreshes_after_event_sequence_conflict \
  -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_native_agent_bridge.py tests/test_run_service.py -q
~~~

## Task 3: ACP update and permission mapping

**Files**

- Extend run_runtime/acp.py transient mapping and serialization helpers.
- Extend tests/test_acp_worker.py mapping and serialization tests.
- Extend tests/fixtures/acp_fake_agent.py only for a scenario the existing
  fixture cannot express.

**Interfaces consumed**

- AcpSessionUpdateObserved, AcpPermissionRequested, and
  AcpPermissionResolved;
- official SDK/Pydantic JSON-mode serialization;
- strict RunEventSpec payload validation.

**Interfaces produced**

- execution.output payload with transport, session, canonical update, and
  serialized character count;
- permission.requested and permission.resolved payloads with bounded ACP
  facts.

**RED test**

Add and run:

- test_session_update_maps_to_execution_output_with_json_data;
- test_nonserializable_update_is_rejected_without_stringification;
- test_update_persistence_failure_surfaces_underlying_canonical_error;
- test_acp_client_wraps_canonical_sink_failure_as_acp_event_sink_error;
- test_permission_requested_maps_without_waiting_user;
- test_permission_resolved_preserves_cancelled_outcome;
- test_permission_title_over_limit_is_rejected_without_truncation;
- test_permission_option_count_over_limit_is_rejected;
- test_permission_option_id_over_limit_is_rejected_without_truncation;
- test_permission_nul_fact_is_rejected;
- test_acp_updates_do_not_emit_native_lifecycle_events.

The first persistence test calls CanonicalAcpEventSink.emit() directly and
asserts the underlying canonical serialization or RunRuntime error. The
second exercises the sink through AcpClientRuntime's existing callback seam
and asserts AcpEventSinkError; Task 6 then proves the adapter translates that
typed ACP error after attempting execution.failed.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'session_update or serializable_update or update_persistence or acp_client_wraps or permission or native_lifecycle' -q
~~~

Expected RED is the absent mapping implementation or missing canonical event
assertions.

**Minimal implementation**

Use the official model serialization surface, then validate the result as a
strict JSON-compatible dict/list/value. Reject arbitrary objects rather than
stringifying them. Map only execution.output, permission.requested, and
permission.resolved; never add native model, turn, tool, usage, or
waiting-user events. Define MAX_CANONICAL_ACP_TEXT_CHARS=2_000 and
MAX_CANONICAL_ACP_PERMISSION_OPTIONS=128. Validate every permission
session_id, tool_call_id, title, option_id, and outcome as a NUL-free string
within the text bound, and reject an oversized option list. Never truncate
provenance-bearing facts. The sink raises the underlying serialization or
RunRuntime error directly; it does not construct AcpEventSinkError. The
existing AcpClientRuntime callback layer wraps that direct sink failure as
AcpEventSinkError, which is tested separately from the direct sink case.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'session_update or serializable_update or update_persistence or acp_client_wraps or permission or native_lifecycle' -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py tests/test_acp_fake_agent_integration.py -k 'update or permission or transcript' -q
~~~

## Task 4: Adapter validation, workspace boundary, and exact prompt

**Files**

- Extend executor_runtime/acp_worker.py with AcpWorkerAttemptAdapter
  construction and pre-side-effect validation.
- Extend tests/test_acp_worker.py with adapter contract tests.

**Interfaces consumed**

- FixWorkerRequest, WorkerAttemptResult, and stable-ID validation;
- GitWorktreeWorkspace, LocalWorkspace, and Workspace;
- AcpPromptRequest;
- AcpClientLimits and the adapter-local structural ACP client runner seam;
- RunRuntime and CanonicalAcpEventSink.

**Interfaces produced**

- AcpWorkerAttemptAdapter.run() with the exact port signature;
- run_id property;
- effective immutable AcpClientLimits, using a supplied instance or a fresh
  default instance;
- injected structural ACP runner stored as self._acp_client;
- call-local launch spec, prompt request, sink, and result state.

**RED test**

Add and run:

- test_non_fix_request_rejected_before_any_side_effect;
- test_local_workspace_rejected_before_any_side_effect;
- test_arbitrary_workspace_rejected_before_any_side_effect;
- test_invalid_execution_id_rejected_before_any_side_effect;
- test_unresolved_executable_rejected_before_any_side_effect;
- test_invalid_cwd_or_prompt_rejected_before_any_side_effect;
- test_prompt_over_effective_acp_limit_is_rejected_before_execution_started;
- test_prompt_request_uses_exact_worktree_root_and_rendered_input;
- test_injected_structural_fake_acp_client_is_supported;
- test_adapter_passes_exact_effective_limits_instance_to_acp_client;
- test_adapter_reuses_one_default_limits_instance_for_validation_and_acp_call;
- test_supplied_execution_id_is_returned_exactly;
- test_run_id_property_matches_constructor_value.

Each rejected case uses a fake ACP client that fails if called, runtime event
inspection, and a mutation sentinel. The prompt-limit case supplies an
AcpClientLimits whose max_prompt_chars is smaller than the otherwise valid
rendered input and asserts ExecutorAdapterInputError, no execution.started,
no ACP run/connect call, and no workspace mutation.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_non_fix_request_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_local_workspace_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_arbitrary_workspace_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_invalid_execution_id_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_unresolved_executable_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_invalid_cwd_or_prompt_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_prompt_over_effective_acp_limit_is_rejected_before_execution_started \
  tests/test_acp_worker.py::test_prompt_request_uses_exact_worktree_root_and_rendered_input \
  tests/test_acp_worker.py::test_injected_structural_fake_acp_client_is_supported \
  tests/test_acp_worker.py::test_adapter_passes_exact_effective_limits_instance_to_acp_client \
  tests/test_acp_worker.py::test_adapter_reuses_one_default_limits_instance_for_validation_and_acp_call \
  tests/test_acp_worker.py::test_supplied_execution_id_is_returned_exactly \
  tests/test_acp_worker.py::test_run_id_property_matches_constructor_value \
  -q
~~~

Expected RED is the missing adapter or missing validation behavior.

**Minimal implementation**

Store the one effective limits value in self._limits: use the supplied
AcpClientLimits instance, or construct one fresh AcpClientLimits() when limits
is None; translate an invalid limits value to ExecutorAdapterInputError as an
adapter input/configuration failure. Use self._limits for both the
pre-side-effect max_prompt_chars check and the ACP call. The structural fake
captures the object and test_adapter_passes_exact_effective_limits_instance_to_acp_client
asserts identity with the explicitly configured instance; the named
test_adapter_reuses_one_default_limits_instance_for_validation_and_acp_call
case captures the object passed to the fake, proves that the same
adapter-stored default controls pre-start max_prompt_chars validation and the
ACP invocation, and proves that run() constructs no second AcpClientLimits
object. This uses a narrow fake/constructor seam and does not expose
self._limits as public API.
Validate all call-local inputs before sink construction/start or coroutine
creation. Require isinstance(workspace, GitWorktreeWorkspace), construct
WorkerAttemptResult to validate the ID, resolve the profile, validate the
absolute existing cwd, and construct AcpPromptRequest with the exact rendered
input. Then reject len(prompt_request.prompt) greater than the effective
max_prompt_chars with ExecutorAdapterInputError. Accept any injected object
whose run attribute is callable; do not require an AcpClientRuntime instance.
Do not touch the workspace beyond reading root.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_non_fix_request_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_local_workspace_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_arbitrary_workspace_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_invalid_execution_id_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_unresolved_executable_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_invalid_cwd_or_prompt_rejected_before_any_side_effect \
  tests/test_acp_worker.py::test_prompt_over_effective_acp_limit_is_rejected_before_execution_started \
  tests/test_acp_worker.py::test_prompt_request_uses_exact_worktree_root_and_rendered_input \
  tests/test_acp_worker.py::test_injected_structural_fake_acp_client_is_supported \
  tests/test_acp_worker.py::test_adapter_passes_exact_effective_limits_instance_to_acp_client \
  tests/test_acp_worker.py::test_adapter_reuses_one_default_limits_instance_for_validation_and_acp_call \
  tests/test_acp_worker.py::test_supplied_execution_id_is_returned_exactly \
  tests/test_acp_worker.py::test_run_id_property_matches_constructor_value \
  -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_native_worker_adapter.py -k 'workspace or execution_id or prompt or canonical' -q
~~~

## Task 5: Synchronous bridge and active-loop semantics

**Files**

- Extend executor_runtime/acp_worker.py with the active-loop check and
  asyncio.run bridge.
- Extend tests/test_acp_worker.py bridge tests.

**Interfaces consumed**

- asynchronous AcpClientRuntime.run;
- the adapter-local structural ACP client runner seam;
- the constructor-stored self._limits value;
- synchronous WorkerAttemptRunner and canonical sink;
- ExecutorAdapterExecutionError.

**Interfaces produced**

- one fresh event loop per synchronous attempt;
- exactly one injected ACP runtime invocation per adapter call;
- deterministic active-loop rejection before coroutine construction.

**RED test**

Add and run:

- test_sync_run_invokes_acp_runtime_once;
- test_second_adapter_call_invokes_a_fresh_acp_runtime_run;
- test_running_event_loop_is_rejected_before_start_or_connect;
- test_running_event_loop_does_not_create_unawaited_coroutine_warning;
- test_no_background_loop_thread_remains_after_run.

The active-loop test runs the synchronous adapter from an async test body,
captures warnings, and uses a fake client whose run method records calls.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'sync_run or second_adapter_call or running_event_loop or background_loop' -q
~~~

Expected RED is the absent bridge or raw asyncio.run runtime error.

**Minimal implementation**

Call asyncio.get_running_loop() before creating the ACP coroutine. Raise the
typed execution error if a loop is active. Otherwise call exactly:

~~~python
asyncio.run(
    self._acp_client.run(
        launch_spec,
        prompt_request,
        limits=self._limits,
        event_sink=sink,
    )
)
~~~

Use the one constructor-stored self._limits object; do not construct another
default during run and do not pass an undefined local limits value. Do not
retain a loop, task, or other per-call values on the adapter.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k 'sync_run or second_adapter_call or running_event_loop or background_loop' -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py -k 'fresh_run or cleanup or cancellation' -q
~~~

## Task 6: ACP success, failure translation, and terminal precedence

**Files**

- Complete executor_runtime/acp_worker.py error translation and lifecycle
  settlement.
- Complete run_runtime/acp.py result and failure payload construction.
- Extend tests/test_acp_worker.py failure and lifecycle tests.

**Interfaces consumed**

- all existing typed AcpRuntimeError subclasses;
- AcpRunResult;
- ExecutorAdapterExecutionError;
- RunRuntime optimistic append errors.

**Interfaces produced**

- exactly one durable execution.completed on success;
- one attempted durable execution.failed for ordinary post-start ACP failures;
- bounded diagnostics and cause-preserving adapter errors;
- canonical persistence failure precedence.

**RED test**

Add and run:

- test_success_records_only_real_acp_result_facts_and_returns_worker_result;
- parameterized test_each_expected_acp_error_records_failure_and_translates,
  covering AcpSpawnError, AcpProtocolError,
  AcpAuthenticationRequiredError, AcpTimeoutError, AcpLimitError,
  AcpEventSinkError, and AcpCleanupError;
- test_translated_error_preserves_original_acp_error_as_cause;
- test_failure_payload_is_bounded_and_excludes_prompt_and_environment;
- test_failure_diagnostic_is_exactly_bounded_to_2000_chars;
- test_ordinary_post_start_dependency_failure_gets_execution_failed;
- test_sink_complete_failure_never_returns_worker_result;
- test_terminal_failure_persistence_error_wins_without_retry;
- test_cancellation_keyboard_interrupt_and_system_exit_are_not_translated.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_success_records_only_real_acp_result_facts_and_returns_worker_result \
  tests/test_acp_worker.py::test_each_expected_acp_error_records_failure_and_translates \
  tests/test_acp_worker.py::test_translated_error_preserves_original_acp_error_as_cause \
  tests/test_acp_worker.py::test_failure_payload_is_bounded_and_excludes_prompt_and_environment \
  tests/test_acp_worker.py::test_failure_diagnostic_is_exactly_bounded_to_2000_chars \
  tests/test_acp_worker.py::test_ordinary_post_start_dependency_failure_gets_execution_failed \
  tests/test_acp_worker.py::test_sink_complete_failure_never_returns_worker_result \
  tests/test_acp_worker.py::test_terminal_failure_persistence_error_wins_without_retry \
  tests/test_acp_worker.py::test_cancellation_keyboard_interrupt_and_system_exit_are_not_translated \
  -q
~~~

Expected RED is the missing result, lifecycle, or error behavior.

**Minimal implementation**

Start the sink once with request.task, await one ACP run, complete it with
only AcpRunResult facts, and return the supplied result ID. After durable
execution.started, catch ordinary Exception-based failures from ACP runtime
execution, result validation, completion-payload construction, or
sink.complete() before terminal persistence succeeds. Unless the sink is
already terminal or canonical persistence is already proven unavailable, make
exactly one best-effort sink.fail() attempt. If it succeeds, raise
ExecutorAdapterExecutionError from the original failure. If it fails, raise
ExecutorAdapterExecutionError from the terminal canonical failure and retain
the original failure in the exception context/cause chain. Do not refresh or
retry EventSequenceError, and do not catch CancelledError, KeyboardInterrupt,
or SystemExit.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_worker.py::test_success_records_only_real_acp_result_facts_and_returns_worker_result \
  tests/test_acp_worker.py::test_each_expected_acp_error_records_failure_and_translates \
  tests/test_acp_worker.py::test_translated_error_preserves_original_acp_error_as_cause \
  tests/test_acp_worker.py::test_failure_payload_is_bounded_and_excludes_prompt_and_environment \
  tests/test_acp_worker.py::test_failure_diagnostic_is_exactly_bounded_to_2000_chars \
  tests/test_acp_worker.py::test_ordinary_post_start_dependency_failure_gets_execution_failed \
  tests/test_acp_worker.py::test_sink_complete_failure_never_returns_worker_result \
  tests/test_acp_worker.py::test_terminal_failure_persistence_error_wins_without_retry \
  tests/test_acp_worker.py::test_cancellation_keyboard_interrupt_and_system_exit_are_not_translated \
  -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_client.py tests/test_acp_fake_agent_integration.py -k 'auth or protocol or timeout or limit or cleanup or sink' -q
~~~

## Task 7: FixLoop integration without orchestration changes

**Files**

- Extend tests/test_acp_worker.py only for adapter-to-port contract checks.
- Create or extend tests/test_acp_worker_integration.py for a real
  FixLoopRunner composition using injected verification and reviewer seams.
- Do not modify fix_runtime/runner.py or fix_runtime/ports.py.

**Interfaces consumed**

- current FixLoopRunner constructor and worker result validation;
- CanonicalFixLoopRecorder and RunCompletionGate;
- WorkerAttemptRunner and WorkerAttemptResult.

**Interfaces produced**

- evidence that the ACP adapter fits the existing orchestration boundary;
- no new FixLoop-specific ACP branch.

**RED test**

Add and run:

- test_fixloop_order_is_fix_attempt_started_then_acp_lifecycle_then_completed;
- test_fixloop_accepts_acp_worker_attempt_result_without_modification;
- test_acp_failure_reaches_existing_fixloop_infrastructure_settlement.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker_integration.py -k 'fixloop or ordering or infrastructure' -q
~~~

Expected RED is missing ACP adapter integration.

**Minimal implementation**

Compose the adapter through the existing worker= constructor argument. Use the
provided execution ID and let FixLoopRunner perform its existing completion
evidence check and failure settlement. If an existing contract defect is
exposed, stop and report it before editing the FixLoop modules.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker_integration.py -k 'fixloop or ordering or infrastructure' -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_fix_loop_runner.py tests/test_native_attempt_adapters_integration.py -q
~~~

## Task 8: Real fake-agent and Git worktree isolation

**Files**

- Extend tests/fixtures/acp_fake_agent.py using the existing official ACP
  server fixture and SDK.
- Extend tests/test_acp_worker_integration.py.
- Modify executor_runtime/__init__.py only if public exports are needed.

**Interfaces consumed**

- GitWorktreeWorkspace.create and dispose;
- AcpWorkerLaunchProfile with the current Python fake-agent command;
- official acp.run_agent fixture behavior;
- AcpClientRuntime real stdio integration.

**Interfaces produced**

- one real adapter trajectory over a real linked worktree;
- proof of source/shadow isolation and completion evidence.

**RED test**

Add and run:

- test_real_acp_worker_mutates_shadow_worktree_not_source_repo;
- test_real_acp_success_has_execution_completed;
- test_real_acp_failure_has_execution_failed_and_no_completed_event.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker_integration.py -k 'real_acp or shadow_worktree or source_repo or execution_completed or execution_failed' -q
~~~

Expected RED is the absent adapter integration or fixture mode.

**Minimal implementation**

Add the smallest fixture mode that edits a known relative file through ACP,
using the existing official SDK server path. Build and dispose a real
GitWorktreeWorkspace, pass its root as ACP cwd, and inspect both source and
shadow files after the run. Ensure subprocess and worktree cleanup happen in
fixture finalization.

**GREEN command**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker_integration.py -k 'real_acp or shadow_worktree or source_repo or execution_completed or execution_failed' -q
~~~

**Adjacent regressions**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_fake_agent_integration.py tests/test_workspace_worktree.py -q
~~~

## Task 9: Regression matrix, full verification, and status synchronization

**Files**

- executor_runtime/acp_worker.py;
- run_runtime/acp.py;
- tests/test_acp_worker.py;
- tests/test_acp_worker_integration.py;
- tests/fixtures/acp_fake_agent.py if Task 8 extended it;
- the companion design and plan documents, only for verified implementation
  status synchronization after milestone work.

**Interfaces consumed**

All interfaces from Tasks 1–8 and every current focused ACP, FixLoop,
workspace, and run-runtime regression suite.

**Interfaces produced**

- complete 3J2B regression evidence;
- synchronized status wording after implementation review;
- no new orchestration or provider-selection behavior.

**RED test**

Before final implementation cleanup, run the complete new test modules and
record the remaining intended failures by test name. A test that fails outside
the intended 3J2B surface is a stop-and-diagnose condition.

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py tests/test_acp_worker_integration.py -q
~~~

**Minimal implementation**

Close only the remaining red cases, then run the focused suite:

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_models.py \
  tests/test_acp_process_cleanup.py \
  tests/test_acp_client.py \
  tests/test_acp_fake_agent_integration.py \
  tests/test_process_runtime.py \
  tests/test_process_tool.py \
  tests/test_acp_worker.py \
  tests/test_acp_worker_integration.py -q
~~~

Then run the broad suite with tests/test_keys.py excluded, compile all changed
Python modules, and run scoped diff hygiene. Synchronize status prose after
implementation verification succeeds, while retaining INDEPENDENT ACTUAL CODE
REVIEW PENDING until the separate review milestone. (Independent design
review was already completed and approved before implementation began; only
the actual-code review of the implemented modules remains outstanding.)

**GREEN commands**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_models.py \
  tests/test_acp_process_cleanup.py \
  tests/test_acp_client.py \
  tests/test_acp_fake_agent_integration.py \
  tests/test_process_runtime.py \
  tests/test_process_tool.py \
  tests/test_acp_worker.py \
  tests/test_acp_worker_integration.py -q

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests --ignore=tests/test_keys.py -q

python3 -m py_compile \
  acp_runtime/*.py executor_runtime/*.py process_runtime/*.py run_runtime/*.py \
  fix_runtime/*.py tests/test_acp_worker.py \
  tests/test_acp_worker_integration.py tests/fixtures/acp_fake_agent.py
~~~

**Adjacent regression tests**

~~~bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_native_worker_adapter.py \
  tests/test_fix_loop_runner.py \
  tests/test_run_service.py \
  tests/test_run_completion.py \
  tests/test_workspace_worktree.py -q
~~~

## Coverage map and self-review checklist

| Requirement group | Primary task(s) |
| --- | --- |
| Launch profile, exact env, resolution | 1 |
| Pre-side-effect validation, workspace, prompt | 4, 5 |
| Canonical lifecycle, source, identity, sequencing | 2, 6 |
| ACP output serialization and no invented native events | 3 |
| Permission requested/resolved semantics | 3 |
| ACP success facts and failure precedence | 6 |
| Sync/async bridge, active loop, freshness, BaseException | 5, 6 |
| FixLoop ordering and infrastructure settlement | 7 |
| Real fake-agent and worktree isolation | 8 |
| Full regression and status synchronization | 9 |

After implementation, verify that names and signatures above match the
earlier tasks, that no task creates a dependency into acp_runtime, that no
task synthesizes model, turn, tool, or usage events, and that every profile
test asserts the absence of a host-environment merge. The implementation
modules and test modules named by this plan are now present and covered by
Task 9 verification.

## Task 10: Implementation hardening round 1

An independent review of the implemented code (not the design) found six
defects, all in `run_runtime/acp.py` and `executor_runtime/acp_worker.py`.
See the companion design document's "Implementation hardening round 1"
section for the full narrative; this task records only the file/test
inventory.

**Files**

- Modify `run_runtime/acp.py`: `persistence_error` state and property,
  `_append()` sets it on failure, `fail()` short-circuits when already set,
  `_permission_text` renamed to `_canonical_text(value, *, field,
  allow_empty=False)`, `title` uses `allow_empty=True`, `_bind_session`
  routes through `_canonical_text`, `_serialize_update`'s raw-dict bypass
  removed.
- Modify `executor_runtime/acp_worker.py`: `AcpWorkerLaunchProfile.env` key
  validation rejects empty/`"="`-containing keys, `CanonicalAcpEventSink`
  construction's `ValueError` translated to `ExecutorAdapterInputError`,
  `run()` checks `sink.persistence_error` before calling `sink.fail()`.
- Modify `tests/test_acp_worker.py`: raw-dict update injections in
  `test_first_transient_event_binds_session_id`,
  `test_foreign_later_session_id_is_rejected`, and
  `test_completion_rejects_result_session_mismatch` replaced with
  `_sdk_update()`; `test_failure_payload_is_bounded_and_excludes_prompt_and_environment`
  rewritten so the original exception message actually contains the
  prompt/env secrets.

**New tests**

```
test_profile_rejects_empty_environment_key
test_profile_rejects_environment_key_containing_equals
test_profile_empty_env_remains_empty
test_profile_empty_environment_value_is_valid
test_session_update_rejects_nul_session_id
test_session_update_rejects_oversized_session_id
test_completion_rejects_oversized_session_id
test_json_shaped_non_sdk_update_is_rejected
test_permission_request_with_empty_optional_title_is_persisted
test_non_running_run_rejected_before_execution_started
test_completion_sequence_conflict_skips_execution_failed_append
test_streaming_persistence_failure_does_not_attempt_terminal_append
```

**RED confirmed, then GREEN**

Every new/modified test above was run against the pre-hardening
implementation first and failed for the intended reason (missing
validation, wrong exception type, or -- for the two persistence-precedence
tests -- an observable extra stale-sequence `record_many()` attempt/lost
`AcpEventSinkError` context) before the corresponding minimal production
change was made.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py tests/test_acp_worker_integration.py -q
```

Result: `84 passed` / `6 passed`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_client.py tests/test_acp_models.py tests/test_acp_fake_agent_integration.py \
  tests/test_acp_process_cleanup.py tests/test_native_worker_adapter.py \
  tests/test_fix_loop_runner.py tests/test_native_agent_bridge.py -q
```

Result: `180 passed, 5 skipped` (Windows-only skips, established).

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests --ignore=tests/test_keys.py -q
```

Result: `1335 passed, 9 skipped` (established `test_keys.py` exclusion plus
the same 5 Windows-only + 3 pre-existing skips; no new exclusion invented).

## Task 11: Implementation hardening round 2

An independent review of the Round-1 diff accepted every Round-1 fix and
found one remaining security blocker: `AcpWorkerAttemptAdapter._failure_message()`'s
sequential `str.replace()` redaction was not overlap-safe (a shorter
sensitive literal that is a prefix/substring of a longer one, when replaced
first, could leave a fragment of the longer secret behind). See the
companion design document's "Implementation hardening round 2" section for
the full narrative; this task records only the file/test inventory.

**Files**

- Modify `executor_runtime/acp_worker.py`: new module-level constant
  `SAFE_REDACTED_DIAGNOSTIC_MESSAGE`; `_failure_message()` rewritten to a
  fail-closed policy -- detect whether the raw message contains any
  sensitive literal (prompt, every env key, every env value, every launch
  argv member) and, if so, substitute the entire fixed safe message rather
  than attempting any partial substitution.
- Modify `tests/test_acp_worker.py`: `test_failure_payload_is_bounded_and_excludes_prompt_and_environment`
  updated to expect the fixed safe message (its raw exception already
  contains real secret material, so it now exercises the sensitive path);
  `test_failure_diagnostic_is_exactly_bounded_to_2000_chars` unchanged and
  still proves the independent 2,000-character bound for a non-sensitive
  long diagnostic.

**New tests**

```
test_failure_diagnostic_redaction_is_safe_when_env_key_is_prefix_of_value
test_failure_diagnostic_redaction_is_safe_when_prompt_overlaps_env_value
test_failure_diagnostic_redacts_sensitive_launch_argument
```

**RED evidence.** Before implementing the fix, the pre-fix
`_failure_message()` was invoked directly with the exact
`test_failure_diagnostic_redaction_is_safe_when_env_key_is_prefix_of_value`
scenario (env key `"TOKEN"`, value `"TOKEN_SECRET"`, synthetic test
literals only): the sequential replacement produced
`"failed with [environment key redacted]_SECRET rejected by agent"` -- the
`"_SECRET"` fragment of the secret value survived, confirming the predicted
partial-leak defect for the intended reason. The three new tests (and the
rewritten existing test) then failed against the H1 implementation with an
`ImportError` for the not-yet-existing `SAFE_REDACTED_DIAGNOSTIC_MESSAGE`
constant, and would have failed on the fragment-survives assertions once
that import was satisfied.

**GREEN.**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py tests/test_acp_worker_integration.py -q
```

Result: `87 passed` / `93 passed` combined (84 Round-1 + 3 new Round-2 in
`test_acp_worker.py`; `test_acp_worker_integration.py` unchanged at 6).

Round-1 persistence semantics reconfirmed unmodified:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_acp_worker.py -k \
  "test_completion_sequence_conflict_skips_execution_failed_append or \
   test_streaming_persistence_failure_does_not_attempt_terminal_append or \
   test_terminal_failure_persistence_error_wins_without_retry or \
   test_ordinary_post_start_dependency_failure_gets_execution_failed" -v
```

Result: `4 passed`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_acp_client.py tests/test_acp_models.py tests/test_acp_fake_agent_integration.py \
  tests/test_acp_process_cleanup.py tests/test_native_worker_adapter.py \
  tests/test_fix_loop_runner.py tests/test_native_agent_bridge.py -q
```

Result: `180 passed, 5 skipped` (Windows-only skips, established).

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests --ignore=tests/test_keys.py -q
```

Result: `1338 passed, 9 skipped` (same established exclusions; no new one
invented, no new hardening test skipped).

Milestone 3J2B
DESIGN COMPLETE
DESIGN HARDENING ROUND 3 COMPLETE
IMPLEMENTATION PLAN COMPLETE
IMPLEMENTED
IMPLEMENTATION HARDENING ROUND 1 COMPLETE
IMPLEMENTATION HARDENING ROUND 2 COMPLETE
INDEPENDENT ACTUAL CODE REVIEW PENDING
