"""Read-only projection of persisted native-agent runs for the run bridge."""

from __future__ import annotations

import json

from workspace.ownership import continuation_available

MAX_HISTORY_RUNS = 32
MAX_HISTORY_EVENTS = 2_000
EVENT_PAGE = 200
MAX_TASK_CHARS = 8_000
MAX_RETRY_TASK_CHARS = 20_000
MAX_TASK_RUN_SCAN = 129


def _bounded_task(prompt: str) -> tuple[str, bool]:
    if len(prompt) <= MAX_TASK_CHARS:
        return prompt, False
    return prompt[:MAX_TASK_CHARS] + "…", True


def _is_native_agent(run) -> bool:
    provider = run.routing.get("agent_provider") if isinstance(run.routing, dict) else None
    return isinstance(provider, str) and 0 < len(provider) <= 128


def _events(runtime, run_id: str):
    result = []
    after_seq = 0
    truncated = False
    while len(result) < MAX_HISTORY_EVENTS:
        page = runtime.events(run_id, after_seq=after_seq, limit=min(EVENT_PAGE, MAX_HISTORY_EVENTS - len(result)))
        if not page.events:
            break
        result.extend(page.events)
        after_seq = page.events[-1].seq
        if not page.has_more:
            break
        if len(result) >= MAX_HISTORY_EVENTS:
            truncated = True
            break
    return result, truncated


def _evidence(runtime, run):
    events, truncated = _events(runtime, run.run_id)
    if truncated:
        # The unseen tail could contain a rejection, application, or resumed
        # attempt; do not present an older receipt as current evidence.
        return None, True
    receipt = None
    current_attempt_after = max((event.seq for event in events if event.type in (
        "run.resumed", "run.restarted", "proposal.applied", "proposal.rejected", "run.cancelled",
        "run.failed", "run.interrupted", "checkpoint.restored",
    )), default=0)
    for event in events:
        if event.type == "proposal.ready" and event.seq > current_attempt_after:
            payload = event.payload
            # Native proposal.ready is raw EVIDENCE, not file contents.
            if isinstance(payload, dict) and len(json.dumps(payload, ensure_ascii=False)) <= 64_000:
                receipt = payload
            else:
                receipt = None
                truncated = True
    return receipt, truncated


def list_history(runtime, project_root: str, *, limit: int = MAX_HISTORY_RUNS) -> list[dict]:
    """Return bounded records for native-agent runs only, exact-root filtered."""
    # Read a bounded scan window so recent legacy rows cannot crowd all native
    # agent history out of the returned list.
    rows = runtime.store.list_runs(project_root=project_root, limit=128)
    result = []
    for run in rows:
        if not _is_native_agent(run):
            continue
        task = runtime.store.get_task(run.task_id)
        if task.project_root != project_root:
            continue
        prompt, task_truncated = _bounded_task(task.prompt)
        result.append({
            "runId": run.run_id, "taskId": run.task_id, "task": prompt,
            "status": run.status.value, "phase": run.phase.value,
            "providerId": run.routing["agent_provider"], "engine": "agent",
            "changedPathCount": None, "errorCode": run.error_code,
            "readOnly": True, "createdAt": run.created_at.isoformat(),
            "lastEventSeq": run.last_event_seq, "taskTruncated": task_truncated,
            "retryAvailable": retry_availability(runtime, project_root, run.run_id),
            "continuationAvailable": continuation_available(runtime, project_root, run.run_id),
        })
    return result[:min(max(limit, 0), MAX_HISTORY_RUNS)]


def retry_source(runtime, project_root: str, run_id: str) -> dict | None:
    """Return canonical retry inputs only for terminal native-agent executions."""
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        run = runtime.get_run(run_id)
        if not _is_native_agent(run) or run.status.value not in {
            "failed", "cancelled",
        }:
            return None
        events, truncated = _events(runtime, run_id)
        lifecycle_tail = [event for event in events if not event.type.startswith("workspace.")]
        if truncated or not lifecycle_tail or lifecycle_tail[-1].type not in {
            "run.failed", "run.cancelled",
        }:
            return None
        if any(event.type in {
            "proposal.ready", "proposal.applied", "proposal.rejected", "checkpoint.restored",
            "run.resumed", "run.restarted", "run.waiting_user",
        } for event in events):
            # Applying/rejecting evidence or retaining a proposal/worktree is
            # not an exhausted attempt; retry must never recreate that authority.
            return None
        # Retry only from the latest known attempt; otherwise the chain could
        # fork and make attempt numbering ambiguous. First attempts must be 1.
        if run.attempt < 1 or (run.retry_of_run_id is None and run.attempt != 1):
            return None
        # A capped query must contain the complete chain, otherwise latest-attempt
        # and sibling status cannot be established safely.
        attempts = runtime.store.list_runs(task_id=run.task_id, limit=MAX_TASK_RUN_SCAN)
        if (len(attempts) >= MAX_TASK_RUN_SCAN or len(attempts) != run.attempt
                or any(item.task_id != run.task_id for item in attempts)):
            return None
        by_id = {item.run_id: item for item in attempts}
        if len(by_id) != len(attempts) or run_id not in by_id:
            return None
        ordered = sorted(attempts, key=lambda item: item.attempt)
        if any(item.attempt != index + 1 for index, item in enumerate(ordered)):
            return None
        if any(item.attempt > run.attempt for item in attempts):
            return None
        providers = {item.routing.get("agent_provider") for item in attempts
                     if isinstance(item.routing, dict)}
        if len(providers) != 1 or providers != {run.routing.get("agent_provider")}:
            return None
        if any(item.status.value not in {"failed", "cancelled"}
               for item in attempts):
            return None
        for index, item in enumerate(ordered):
            expected_parent = ordered[index - 1].run_id if index else None
            if item.retry_of_run_id != expected_parent or not _is_native_agent(item):
                return None
        task = runtime.store.get_task(run.task_id)
        if task.project_root != project_root or len(task.prompt) > MAX_RETRY_TASK_CHARS:
            return None
        return {
            "taskId": task.task_id,
            "task": task.prompt,
            "providerId": run.routing["agent_provider"],
            "attempt": run.attempt,
            "taskTruncated": False,
        }
    except Exception:
        return None


def retry_availability(runtime, project_root: str, run_id: str) -> bool:
    """Bounded eligibility projection; never returns or copies the prompt."""
    return retry_source(runtime, project_root, run_id) is not None


def get_history(runtime, project_root: str, run_id: str) -> dict | None:
    """Load persisted native agent detail without acquiring execution authority."""
    try:
        run = runtime.get_run(run_id)
        if not _is_native_agent(run):
            return None
        task = runtime.store.get_task(run.task_id)
        if task.project_root != project_root:
            return None
        evidence, truncated = _evidence(runtime, run)
    except Exception:
        # Corrupt/malformed history fails closed at the bridge boundary.
        return None
    prompt, task_truncated = _bounded_task(task.prompt)
    return {
        "runId": run.run_id, "taskId": run.task_id, "task": prompt,
        "providerId": run.routing["agent_provider"], "status": run.status.value,
        "phase": run.phase.value, "engine": "agent", "evidence": evidence,
        "proposals": [], "totals": {"latency_s": None, "tokens": None, "cost_usd": None},
        "errorCode": run.error_code, "checkpointId": None, "readOnly": True,
        "historyTruncated": truncated, "createdAt": run.created_at.isoformat(),
        "lastEventSeq": run.last_event_seq, "taskTruncated": task_truncated,
        "retryAvailable": retry_availability(runtime, project_root, run.run_id),
        "continuationAvailable": continuation_available(runtime, project_root, run.run_id),
    }
