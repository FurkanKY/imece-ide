"""Bounded orchestration for one autonomous agent attempt and its evidence.

This is intentionally not a UI/run manager. The caller owns the Run lifecycle
and workspace lifetime; pending proposals are never applied or disposed here.
"""

from __future__ import annotations

import uuid
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

import engine_factory
from agent_runtime.cancellation import CancellationToken, OperationCancelledError
from change_runtime import GitWorktreeChangeProvider
from change_runtime.provider import ChangeProvider
from context_runtime import ProjectRules
from executor_runtime import NativeVerificationAttemptAdapter
from fix_runtime.models import InitialWorkerRequest, _capture_initial_worker_render_context
from fix_runtime.prompt import render_initial_worker_input
from fix_runtime.ports import WorkerAttemptRunner, VerificationAttemptRunner
from pipeline_runtime.verification_detect import detect_verification_plan
from run_runtime.pipeline import CanonicalPipelineRecorder
from run_runtime.service import RunRuntime
from run_runtime.events import RunEventType
from verification_runtime.models import VerificationPlan, VerificationReport, new_verification_id
from executor_runtime.errors import ExecutorAdapterCancelledError
from collab_runtime.candidates import _FP_ENTRY_BUDGET, _FP_MAX_DEPTH, _fingerprint_records


class AgentExecutionStatus(StrEnum):
    NEEDS_USER = "needs_user"
    NO_CHANGES = "no_changes"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentExecutionPorts:
    worker: WorkerAttemptRunner
    verification: VerificationAttemptRunner
    change_provider: ChangeProvider


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    task: str
    provider_id: str
    workspace: object
    rules: ProjectRules | None = None
    pinned_paths: Sequence[str] = ()
    verification_plan: VerificationPlan | None = None


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    run_id: str
    status: AgentExecutionStatus
    reason: str
    changed_paths: tuple[str, ...] = ()
    verification_outcome: str = "not_run"
    execution_id: str | None = None
    verification_report: VerificationReport | None = None


def _find_execution_completion(runtime: RunRuntime, run_id: str, execution_id: str):
    """Find this execution's receipt across a finite run-history snapshot."""
    through_seq = runtime.get_run(run_id).last_event_seq
    after_seq = 0
    found = None
    while after_seq < through_seq:
        page = runtime.events(run_id, after_seq=after_seq, limit=200)
        if not page.events:
            break
        for event in page.events:
            if event.seq > through_seq:
                break
            if event.type == "execution.completed" and event.execution_id == execution_id:
                found = event
        last_seq = page.events[-1].seq
        if last_seq <= after_seq:
            break
        after_seq = last_seq
        if not page.has_more:
            break
    return found


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _workspace_inventory(workspace) -> tuple[tuple[str, ...], bool]:
    """Pin the bounded pre-verification regular-file path set.

    This inventories files, not contents. The candidate fingerprint then
    hashes precisely these originals at both snapshots, applying the existing
    new-cache exclusions to other entries. Dependency trees and generated
    runtimes are subject to the same entry/depth/file/total-byte bounds; an
    incomplete inventory is explicitly not verification evidence.
    """
    root = os.fspath(workspace.root)
    if os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd:
        # Unsupported nofollow directory-handle inspection is incomplete
        # evidence, not permission to follow ordinary paths and claim PASS.
        return (), False
    paths: list[str] = []
    count = 0
    complete = True

    def visit(fd: int, prefix: str, depth: int) -> None:
        nonlocal count, complete
        if depth > _FP_MAX_DEPTH:
            complete = False
            return
        entries = []
        scanner_fd = None
        try:
            scanner_fd = os.dup(fd)
            with os.scandir(scanner_fd) as scanner:
                for entry in scanner:
                    if entry.name == ".git":
                        continue
                    if len(entries) >= _FP_ENTRY_BUDGET - count:
                        complete = False
                        return
                    entries.append(entry.name)
        except OSError:
            complete = False
            return
        finally:
            if scanner_fd is not None:
                os.close(scanner_fd)
        entries.sort()
        for name in entries:
            if count >= _FP_ENTRY_BUDGET:
                complete = False
                return
            count += 1
            rel = f"{prefix}/{name}" if prefix else name
            try:
                lst = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                complete = False
                continue
            if stat.S_ISDIR(lst.st_mode):
                try:
                    child_fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=fd)
                    try:
                        opened = os.fstat(child_fd)
                        if (not stat.S_ISDIR(opened.st_mode)
                                or (opened.st_dev, opened.st_ino) != (lst.st_dev, lst.st_ino)):
                            complete = False
                        else:
                            visit(child_fd, rel, depth + 1)
                    finally:
                        os.close(child_fd)
                except OSError:
                    complete = False
            elif stat.S_ISREG(lst.st_mode):
                try:
                    file_fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        opened = os.fstat(file_fd)
                        if (not stat.S_ISREG(opened.st_mode)
                                or (opened.st_dev, opened.st_ino) != (lst.st_dev, lst.st_ino)):
                            complete = False
                        else:
                            paths.append(rel)
                    finally:
                        os.close(file_fd)
                except OSError:
                    complete = False
            else:
                # Symlinks and special files are not inputs we can safely
                # inventory; candidate fingerprint also emits an anomaly.
                complete = False

    try:
        root_fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return (), False
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            return (), False
        visit(root_fd, "", 1)
    except OSError:
        complete = False
    finally:
        os.close(root_fd)
    return tuple(paths), complete


def _workspace_fingerprint(workspace, original_paths: tuple[str, ...] | None = None) -> tuple[str | None, bool]:
    """Fingerprint using pinned originals and candidate nofollow/budget policy.

    When called independently, inventory is captured once for that snapshot;
    execute_task explicitly pins one inventory and reuses it before and after.
    """
    if original_paths is None:
        original_paths, inventory_complete = _workspace_inventory(workspace)
    else:
        inventory_complete = True
    digest, fingerprint_complete = _fingerprint_records(workspace.root, original_paths)
    return digest, inventory_complete and fingerprint_complete


def _canonical_verification_matches(runtime: RunRuntime, run_id: str, verification_id: str,
                                    plan: VerificationPlan, report: VerificationReport) -> bool:
    """Require a complete canonical receipt for this exact requested attempt."""
    if (report.verification_id != verification_id or report.plan_id != plan.plan_id
            or tuple(result.check_id for result in report.results) != tuple(c.check_id for c in plan.checks)):
        return False
    run = runtime.get_run(run_id)
    after_seq, receipt = 0, None
    check_results: dict[str, str] = {}
    while after_seq < run.last_event_seq:
        page = runtime.events(run_id, after_seq=after_seq, limit=200)
        if not page.events:
            break
        for event in page.events:
            payload = event.payload
            if payload.get("verification_id") != verification_id:
                continue
            if event.type == RunEventType.VERIFICATION_CHECK_COMPLETED:
                check_results[payload.get("check_id")] = payload.get("status")
            elif event.type == RunEventType.VERIFICATION_CHECK_FAILED:
                check_results[payload.get("check_id")] = "error"
            elif event.type == RunEventType.VERIFICATION_COMPLETED:
                receipt = payload
        after_seq = page.events[-1].seq
        if not page.has_more:
            break
    expected = {check.check_id for check in plan.checks}
    expected_statuses = {result.check_id: result.status.value for result in report.results}
    return bool(
        receipt and receipt.get("plan_id") == plan.plan_id
        and receipt.get("status") == report.status.value
        and set(check_results) == expected
        and check_results == expected_statuses
    )


def build_agent_ports(
    runtime: RunRuntime,
    run_id: str,
    provider_id: str,
    *,
    backend_factory=None,
    acp_client_factory=None,
    change_provider: ChangeProvider | None = None,
    worker_safe_point=None,
) -> AgentExecutionPorts:
    """Build one worker, deterministic verifier and change provider only.

    Provider support and safe-point compatibility are checked before any
    backend/ACP constructor can perform I/O.
    """
    supported, why = engine_factory.role_supported(provider_id)
    if not supported:
        raise engine_factory.EngineUnsupportedError(why)
    entry, kind = engine_factory._provider_kind(provider_id)
    if worker_safe_point is not None and kind not in engine_factory._NATIVE_KINDS:
        raise engine_factory.EngineUnsupportedError("Collaboration safe points require a native worker provider.")
    worker = engine_factory._build_worker(
        runtime, run_id, provider_id=provider_id, entry=entry, kind=kind,
        backend_factory=backend_factory or engine_factory._default_backend_factory,
        acp_client_factory=acp_client_factory or engine_factory._default_acp_client_factory,
        worker_safe_point=worker_safe_point,
    )
    return AgentExecutionPorts(
        worker=worker,
        verification=NativeVerificationAttemptAdapter(runtime, run_id),
        change_provider=change_provider or GitWorktreeChangeProvider(),
    )


def execute_task(
    runtime: RunRuntime,
    run_id: str,
    request: AgentExecutionRequest,
    *,
    ports: AgentExecutionPorts,
    cancel_token: CancellationToken | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> AgentExecutionResult:
    """Run one task attempt, capture its diff, verify, then wait for a human.

    `ports` is an explicit injection seam for deterministic local tests and
    callers that have already constructed their worker adapter.
    """
    recorder = CanonicalPipelineRecorder(runtime, run_id)
    execution_id = f"exec_{uuid.uuid4()}"
    try:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        if on_stage:
            on_stage("working")
        plan = request.verification_plan
        if plan is None:
            plan = detect_verification_plan(request.workspace.root)
        rendered = render_initial_worker_input(
            task=request.task, plan=None, verification_plan=plan, rules=request.rules,
            pinned_paths=request.pinned_paths,
        )
        worker_request = InitialWorkerRequest(
            task=request.task, rendered_input=rendered, plan=None,
            render_context=_capture_initial_worker_render_context(
                "(no deterministic verification plan was detected for this workspace)"
                if plan is None else "\n".join(
                    f"- {c.name} ({c.check_id}): {' '.join(c.request.argv)}" for c in plan.checks
                ), request.pinned_paths,
            ),
        )
        receipt = ports.worker.run(request.workspace, worker_request, execution_id=execution_id,
                                   cancel_token=cancel_token)
        execution_id = receipt.execution_id
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        completed = _find_execution_completion(runtime, run_id, execution_id)
        if completed is None:
            raise RuntimeError("Worker attempt did not record execution.completed")
        original_paths, inventory_complete = _workspace_inventory(request.workspace)
        before_verification, before_fingerprint_complete = _workspace_fingerprint(
            request.workspace, original_paths
        )
        before_complete = inventory_complete and before_fingerprint_complete
        change_set = ports.change_provider.capture(request.workspace)
        if not change_set.changed_paths:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            recorder.completed_no_changes()
            return AgentExecutionResult(run_id, AgentExecutionStatus.NO_CHANGES, "no_changes", execution_id=execution_id)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        report = None
        outcome = "not_run" if plan is None else "error"
        if plan is not None:
            requested_verification_id = new_verification_id()
            try:
                if on_stage:
                    on_stage("verifying")
                report = ports.verification.run(
                    request.workspace, plan, verification_id=requested_verification_id, cancel_token=cancel_token,
                )
                outcome = report.status.value
                if not _canonical_verification_matches(
                    runtime, run_id, requested_verification_id, plan, report
                ):
                    outcome = "error"
                    report = None
            except (OperationCancelledError, ExecutorAdapterCancelledError):
                raise
            except Exception:
                # Verification infrastructure failure is evidence too: leave
                # the changed proposal available, clearly marked as failed.
                outcome = "error"
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        after_verification, after_complete = _workspace_fingerprint(request.workspace, original_paths)
        fingerprint_complete = before_complete and after_complete
        changed_during_verification = before_verification != after_verification
        # Incomplete or changed evidence can only revoke a claimed pass. Keep
        # real fail/not_run/error outcomes intact so the displayed reason is
        # not replaced by an evidence-quality classification.
        if outcome == "pass" and (not fingerprint_complete or changed_during_verification):
            outcome = "invalidated"
        # Checks can modify unchanged inputs; capture final proposal after checks.
        change_set = ports.change_provider.capture(request.workspace)
        payload = {
            "reason": "single_agent_proposal",
            "execution_id": execution_id,
            "agent_message": str(completed.payload.get("final_text", ""))[:4000],
            "attempt_receipt": {
                "model_turns": completed.payload.get("model_turns"),
                "tool_calls": completed.payload.get("tool_calls"),
            },
            "changed_paths": list(change_set.changed_paths),
            "diff_sha256": change_set.diff_sha256,
            "verification": {
                "outcome": outcome,
                "fingerprint_complete": fingerprint_complete,
                "changed_content": changed_during_verification,
                "verification_id": report.verification_id if report else None,
                "plan_id": report.plan_id if report else None,
                "checks": [
                    {"check_id": r.check_id, "status": r.status.value}
                    for r in report.results
                ] if report else [],
            },
        }
        recorder.needs_user(payload=payload)
        return AgentExecutionResult(
            run_id, AgentExecutionStatus.NEEDS_USER, "proposal_pending", change_set.changed_paths,
            outcome, execution_id, report,
        )
    except Exception as exc:
        # Cancellation and failures are explicit Run outcomes; verification
        # failure itself is not raised and remains a reviewable proposal.
        if isinstance(exc, ExecutorAdapterCancelledError) or isinstance(exc, OperationCancelledError):
            recorder.cancelled()
            return AgentExecutionResult(run_id, AgentExecutionStatus.CANCELLED, "cancelled", execution_id=execution_id)
        recorder.failed(error_code="agent_execution_failed", error_message="Agent execution failed.")
        return AgentExecutionResult(run_id, AgentExecutionStatus.FAILED, "agent_execution_failed", execution_id=execution_id)
