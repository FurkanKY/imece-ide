"""run_runtime.activity_projection -- canonical RunEvent -> compact UI
'live activity' item projection (Aşama 3 F1, "live agent activity").

`project_event(event) -> item | None` is a PURE, deterministic function: it
never touches SQLite, never calls out to another runtime, and always
returns the same item for the same RunEvent. It maps one already-durable
canonical event into the bounded shape the AI panel's "Etkinlik" tab
renders:

    {id, runId, seq, ts, role, kind, status, title, detail?}

`id` is stable across an item's lifecycle (e.g. one tool call's
requested -> started -> completed/failed transitions all share one id) so a
live consumer can update the same row in place instead of appending
duplicates -- see webhost.api.activity.ActivityStream for the stateful,
throttled consumer built on top of this pure function.

Event types outside the covered set (run.*, checkpoint.*, permission.*,
proposal.*, change.proposed, ...) are intentionally out of scope for the
live-activity feed and map to None.
"""

from __future__ import annotations

from typing import Any

from run_runtime.events import RunEvent, RunEventType

MAX_TITLE_CHARS = 300
MAX_DETAIL_CHARS = 4096
MAX_VERIFICATION_TAIL_CHARS = 4096

# ---------------- Turkish tool title mapping (tool_runtime/tools/*) ----------------


def _arg(arguments: Any, key: str, default: str = "?") -> str:
    if not isinstance(arguments, dict):
        return default
    value = arguments.get(key)
    if value is None:
        return default
    return str(value)


def _tool_title(tool_name: str, arguments: Any) -> str:
    if tool_name == "read_file":
        return f"Okundu: {_arg(arguments, 'path')}"
    if tool_name == "list_files":
        return f"Listelendi: {_arg(arguments, 'path', '.')}"
    if tool_name == "search_text":
        return f"Arandı: {_arg(arguments, 'query')}"
    if tool_name == "write_file":
        return f"Düzenlendi: {_arg(arguments, 'path')}"
    if tool_name == "delete_path":
        return f"Silindi: {_arg(arguments, 'path')}"
    if tool_name == "run_process":
        argv = arguments.get("argv") if isinstance(arguments, dict) else None
        if isinstance(argv, list) and argv:
            return f"Komut çalıştırıldı: {' '.join(str(a) for a in argv)}"
        return f"Komut çalıştırıldı: {_arg(arguments, 'command')}"
    if tool_name == "repo_map":
        return f"Depo haritası: {_arg(arguments, 'query', '(genel)')}"
    if tool_name == "search_code":
        return f"Kod arandı: {_arg(arguments, 'query')}"
    return f"Araç: {tool_name}"


# ACP ToolKind ("read"/"edit"/"delete"/"move"/"search"/"execute"/"think"/
# "fetch"/"switch_mode"/"other") -> Turkish verb, used both for the ACP
# worker's execution.output updates and the ACP planner/reviewer's
# agent.activity notices (see executor_runtime.acp_semantic).
ACP_KIND_VERB = {
    "read": "Okundu",
    "edit": "Düzenlendi",
    "delete": "Silindi",
    "move": "Taşındı",
    "search": "Arandı",
    "execute": "Çalıştırıldı",
    "fetch": "Getirildi",
    "switch_mode": "Mod değişti",
    "think": "Düşünüldü",
    "other": "İşlem",
}


def acp_tool_title(kind: str | None, title: str | None) -> str:
    verb = ACP_KIND_VERB.get(kind or "other", "İşlem")
    if title:
        return f"{verb}: {title}"
    return verb


_ACP_STATUS = {
    "pending": "running",
    "in_progress": "running",
    "completed": "ok",
    "failed": "error",
}


def _iso(event: RunEvent) -> str:
    return event.created_at.isoformat()


def _base(event: RunEvent, *, item_id: str, role: str, kind: str, status: str, title: str,
          detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "runId": event.run_id,
        "seq": event.seq,
        "ts": _iso(event),
        "role": role,
        "kind": kind,
        "status": status,
        "title": title[:MAX_TITLE_CHARS],
    }
    if detail is not None:
        item["detail"] = detail[:MAX_DETAIL_CHARS]
    return item


def _tail(text: Any, *, max_chars: int = MAX_VERIFICATION_TAIL_CHARS) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _project_tool(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    call_id = payload.get("call_id")
    tool_name = payload.get("tool_name", "?")
    if not isinstance(call_id, str):
        return None
    item_id = f"tool:{event.execution_id}:{call_id}"
    if event.type in (RunEventType.TOOL_REQUESTED, RunEventType.TOOL_STARTED):
        title = _tool_title(tool_name, payload.get("arguments"))
        return _base(event, item_id=item_id, role="worker", kind="tool", status="running", title=title)
    if event.type == RunEventType.TOOL_COMPLETED:
        # tool.completed carries `metadata`, not the original `arguments` --
        # but tool_runtime's executors consistently echo back the same
        # path/query/argv keys in metadata (see tool_runtime/tools/*.py), so
        # this keeps the title identical to the running -> ok transition
        # instead of regressing to the generic "Araç: <name>" fallback.
        title = _tool_title(tool_name, payload.get("metadata"))
        detail = _tail(payload.get("content"))
        return _base(event, item_id=item_id, role="worker", kind="tool", status="ok", title=title, detail=detail)
    if event.type == RunEventType.TOOL_FAILED:
        title = _tool_title(tool_name, payload.get("metadata"))
        detail = _tail(payload.get("message"))
        return _base(event, item_id=item_id, role="worker", kind="tool", status="error", title=title, detail=detail)
    return None


def _project_verification_check(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    verification_id = payload.get("verification_id")
    check_id = payload.get("check_id")
    item_id = f"check:{verification_id}:{check_id}"
    name = payload.get("name") or check_id or "check"
    if event.type == RunEventType.VERIFICATION_CHECK_STARTED:
        argv = payload.get("argv") or []
        cmd = " ".join(str(a) for a in argv) if argv else ""
        title = f"Kontrol: {name}"
        detail = cmd or None
        return _base(event, item_id=item_id, role="verification", kind="check", status="running",
                     title=title, detail=detail)
    if event.type == RunEventType.VERIFICATION_CHECK_COMPLETED:
        title = f"Kontrol geçti: {name}"
        detail = _tail(payload.get("stdout") or payload.get("stderr"))
        return _base(event, item_id=item_id, role="verification", kind="check", status="ok",
                     title=title, detail=detail)
    if event.type == RunEventType.VERIFICATION_CHECK_FAILED:
        title = f"Kontrol başarısız: {name}"
        detail = _tail(payload.get("stderr") or payload.get("stdout") or payload.get("error_message"))
        return _base(event, item_id=item_id, role="verification", kind="check", status="error",
                     title=title, detail=detail)
    return None


def _project_plan(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    plan_id = payload.get("plan_id")
    item_id = f"plan:{plan_id}"
    if event.type == RunEventType.PLAN_STARTED:
        return _base(event, item_id=item_id, role="planner", kind="stage", status="running", title="Planlanıyor…")
    if event.type == RunEventType.PLAN_COMPLETED:
        return _base(event, item_id=item_id, role="planner", kind="stage", status="ok", title="Plan tamamlandı",
                     detail=_tail(payload.get("summary")))
    if event.type == RunEventType.PLAN_FAILED:
        return _base(event, item_id=item_id, role="planner", kind="stage", status="error",
                     title="Plan başarısız", detail=_tail(payload.get("error_message")))
    return None


def _project_review(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    review_id = payload.get("review_id")
    item_id = f"review:{review_id}"
    if event.type == RunEventType.REVIEW_STARTED:
        return _base(event, item_id=item_id, role="reviewer", kind="stage", status="running", title="İnceleniyor…")
    if event.type == RunEventType.REVIEW_COMPLETED:
        verdict = payload.get("verdict") or "?"
        return _base(event, item_id=item_id, role="reviewer", kind="stage", status="ok",
                     title=f"İnceleme sonucu: {verdict}", detail=_tail(payload.get("summary")))
    if event.type == RunEventType.REVIEW_FAILED:
        return _base(event, item_id=item_id, role="reviewer", kind="stage", status="error",
                     title="İnceleme başarısız", detail=_tail(payload.get("error_message")))
    return None


def _project_fix(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    fix_loop_id = payload.get("fix_loop_id")
    if event.type == RunEventType.FIX_LOOP_STARTED:
        return _base(event, item_id=f"fix_loop:{fix_loop_id}", role="fix", kind="stage", status="running",
                     title="Otomatik düzeltme döngüsü başladı")
    if event.type in (RunEventType.FIX_LOOP_COMPLETED, RunEventType.FIX_LOOP_EXHAUSTED,
                      RunEventType.FIX_LOOP_FAILED):
        ok = event.type == RunEventType.FIX_LOOP_COMPLETED
        title = "Düzeltme döngüsü tamamlandı" if ok else "Düzeltme döngüsü bitti (başarısız)"
        return _base(event, item_id=f"fix_loop:{fix_loop_id}", role="fix", kind="stage",
                     status="ok" if ok else "error", title=title)
    if event.type == RunEventType.FIX_ATTEMPT_STARTED:
        idx = payload.get("attempt_index")
        item_id = f"fix_attempt:{fix_loop_id}:{payload.get('fix_attempt_id')}"
        return _base(event, item_id=item_id, role="fix", kind="stage", status="running",
                     title=f"Düzeltme denemesi {idx}")
    if event.type == RunEventType.FIX_ATTEMPT_COMPLETED:
        idx = payload.get("attempt_index")
        item_id = f"fix_attempt:{fix_loop_id}:{payload.get('fix_attempt_id')}"
        changed = payload.get("changed")
        return _base(event, item_id=item_id, role="fix", kind="stage", status="ok",
                     title=f"Düzeltme denemesi {idx} tamamlandı" + ("" if changed else " (değişiklik yok)"))
    return None


def _project_execution_stage(event: RunEvent) -> dict[str, Any] | None:
    item_id = f"execution:{event.execution_id}"
    if event.type == RunEventType.EXECUTION_STARTED:
        return _base(event, item_id=item_id, role="worker", kind="stage", status="running",
                     title="Çalışıyor…")
    if event.type == RunEventType.EXECUTION_COMPLETED:
        return _base(event, item_id=item_id, role="worker", kind="stage", status="ok",
                     title="Çalışma tamamlandı")
    if event.type == RunEventType.EXECUTION_FAILED:
        return _base(event, item_id=item_id, role="worker", kind="stage", status="error",
                     title="Çalışma başarısız", detail=_tail(event.payload.get("message")))
    return None


def _project_verification_stage(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    verification_id = payload.get("verification_id")
    item_id = f"verification:{verification_id}"
    if event.type == RunEventType.VERIFICATION_STARTED:
        return _base(event, item_id=item_id, role="verification", kind="stage", status="running",
                     title="Doğrulama çalıştırılıyor…")
    if event.type == RunEventType.VERIFICATION_COMPLETED:
        status_value = payload.get("status") or "?"
        ok = status_value == "pass"
        return _base(event, item_id=item_id, role="verification", kind="stage",
                     status="ok" if ok else "error", title=f"Doğrulama: {status_value}")
    return None


def _project_usage(event: RunEvent) -> dict[str, Any] | None:
    payload = event.payload
    total_tokens = payload.get("total_tokens")
    cost_usd = payload.get("cost_usd")
    title = f"Kullanım: {total_tokens or 0} token"
    if cost_usd:
        title += f", ${cost_usd:.4f}"
    item_id = f"usage:{event.event_id}"
    return _base(event, item_id=item_id, role="worker", kind="usage", status="info", title=title)


def _project_acp_output(event: RunEvent) -> dict[str, Any] | None:
    """execution.output emitted by run_runtime.acp.CanonicalAcpEventSink for
    the ACP Worker (transport == "acp"). Unpacks the raw serialized ACP
    session update (see CanonicalAcpEventSink._serialize_update) and maps
    tool_call / tool_call_update into one update-in-place tool item; every
    other update kind (agent_message_chunk, agent_thought_chunk, plan,
    ...) is intentionally dropped here (see module docstring) -- Planner's
    own plan progress arrives instead as an agent.activity note (see
    _project_agent_activity) when routed through a recorder."""
    payload = event.payload
    if payload.get("transport") != "acp":
        return None
    update = payload.get("update")
    if not isinstance(update, dict):
        return None
    kind = update.get("sessionUpdate")
    if kind not in ("tool_call", "tool_call_update"):
        return None
    tool_call_id = update.get("toolCallId")
    if not isinstance(tool_call_id, str):
        return None
    item_id = f"tool:{event.execution_id}:{tool_call_id}"
    status = _ACP_STATUS.get(update.get("status"), "running")
    title = acp_tool_title(update.get("kind"), update.get("title"))
    detail = None
    content = update.get("content")
    if isinstance(content, list) and content:
        pieces = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content", {}).get("text") if isinstance(
                    block.get("content"), dict) else block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        if pieces:
            detail = _tail("\n".join(pieces))
    return _base(event, item_id=item_id, role="worker", kind="tool", status=status, title=title, detail=detail)


def _project_agent_activity(event: RunEvent) -> dict[str, Any] | None:
    """agent.activity passthrough (see run_runtime.agent_activity) -- used by
    the ACP Planner/Reviewer mapping (executor_runtime.acp_semantic) since
    their session updates are never canonicalized as execution.*."""
    payload = event.payload
    role = payload.get("role")
    kind = payload.get("kind")
    status = payload.get("status")
    title = payload.get("title")
    if not isinstance(role, str) or not isinstance(kind, str) or not isinstance(status, str) \
            or not isinstance(title, str):
        return None
    tool_call_id = payload.get("tool_call_id")
    if isinstance(tool_call_id, str):
        item_id = f"activity_tool:{event.execution_id}:{role}:{tool_call_id}"
    else:
        item_id = f"activity:{event.event_id}"
    detail = payload.get("detail")
    return _base(event, item_id=item_id, role=role, kind=kind, status=status, title=title,
                 detail=detail if isinstance(detail, str) else None)


def _project_decision(event: RunEvent) -> dict[str, Any] | None:
    """decision.made (bkz. decision_runtime.recorder.CanonicalDecisionRecorder)
    -- Jev System One karar katmanının bir doğrulama FAIL'ini nasıl
    sınıflandırdığını Etkinlik akışında tek satırla özetler. Yalnızca
    "failure_kind" cevabını taşır: decide_triage_action'ın SEÇTİĞİ eylem bu
    olayın payload'ında YOKTUR -- decision.made, eylem belirlenmeden ÖNCE
    kaydedilir (bkz. decision_runtime.gate.VerificationFailureGate.evaluate)."""
    answers = event.payload.get("answers")
    answer = answers.get("failure_kind") if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        return None
    kind = answer.get("choice")
    if not isinstance(kind, str):
        return None
    confidence = answer.get("confidence")
    title = f"Karar: {kind}"
    if isinstance(confidence, (int, float)):
        title += f" (güven %{confidence * 100:.0f})"
    return _base(event, item_id=f"decision:{event.event_id}", role="system", kind="note",
                 status="info", title=title)


_PROJECTORS: dict[str, Any] = {
    RunEventType.TOOL_REQUESTED: _project_tool,
    RunEventType.TOOL_STARTED: _project_tool,
    RunEventType.TOOL_COMPLETED: _project_tool,
    RunEventType.TOOL_FAILED: _project_tool,
    RunEventType.VERIFICATION_CHECK_STARTED: _project_verification_check,
    RunEventType.VERIFICATION_CHECK_COMPLETED: _project_verification_check,
    RunEventType.VERIFICATION_CHECK_FAILED: _project_verification_check,
    RunEventType.VERIFICATION_STARTED: _project_verification_stage,
    RunEventType.VERIFICATION_COMPLETED: _project_verification_stage,
    RunEventType.PLAN_STARTED: _project_plan,
    RunEventType.PLAN_COMPLETED: _project_plan,
    RunEventType.PLAN_FAILED: _project_plan,
    RunEventType.REVIEW_STARTED: _project_review,
    RunEventType.REVIEW_COMPLETED: _project_review,
    RunEventType.REVIEW_FAILED: _project_review,
    RunEventType.FIX_LOOP_STARTED: _project_fix,
    RunEventType.FIX_LOOP_COMPLETED: _project_fix,
    RunEventType.FIX_LOOP_EXHAUSTED: _project_fix,
    RunEventType.FIX_LOOP_FAILED: _project_fix,
    RunEventType.FIX_ATTEMPT_STARTED: _project_fix,
    RunEventType.FIX_ATTEMPT_COMPLETED: _project_fix,
    RunEventType.EXECUTION_STARTED: _project_execution_stage,
    RunEventType.EXECUTION_COMPLETED: _project_execution_stage,
    RunEventType.EXECUTION_FAILED: _project_execution_stage,
    RunEventType.USAGE_RECORDED: _project_usage,
    RunEventType.EXECUTION_OUTPUT: _project_acp_output,
    RunEventType.AGENT_ACTIVITY: _project_agent_activity,
    RunEventType.DECISION_MADE: _project_decision,
}


def project_event(event: RunEvent) -> dict[str, Any] | None:
    """Map one canonical RunEvent to a bounded UI activity item, or None if
    this event type is out of scope for the live-activity feed (see module
    docstring)."""
    handler = _PROJECTORS.get(event.type)
    if handler is None:
        return None
    return handler(event)
