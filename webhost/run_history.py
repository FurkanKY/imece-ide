"""Read-only projection of persisted native-agent runs for the run bridge."""

from __future__ import annotations

import json

MAX_HISTORY_RUNS = 32
MAX_HISTORY_EVENTS = 2_000
EVENT_PAGE = 200
MAX_TASK_CHARS = 8_000


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
        "run.resumed", "proposal.applied", "proposal.rejected", "run.cancelled",
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
        })
    return result[:min(max(limit, 0), MAX_HISTORY_RUNS)]


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
    }
