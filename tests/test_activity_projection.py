"""Unit tests for run_runtime.activity_projection.project_event -- pure
canonical RunEvent -> UI 'live activity' item mapping (Aşama 3 F1).

No SQLite/RunRuntime involved: RunEvent instances are built directly via
run_runtime.events.build_event so each canonical type's payload shape can be
exercised in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.activity_projection import (
    MAX_DETAIL_CHARS,
    MAX_TITLE_CHARS,
    acp_tool_title,
    project_event,
)
from run_runtime.events import RunEventType, build_event


def _ev(type_, payload, *, seq=1, execution_id=None, turn_id=None, item_id=None):
    return build_event(
        run_id="run-1", seq=seq, type=type_, payload=payload,
        execution_id=execution_id, turn_id=turn_id, item_id=item_id,
    )


# ---------------- tool.* ----------------

def test_tool_requested_maps_turkish_read_title():
    ev = _ev(
        RunEventType.TOOL_REQUESTED,
        {"call_id": "c1", "tool_name": "read_file", "arguments": {"path": "src/a.py"}},
        execution_id="exec-1",
    )
    item = project_event(ev)
    assert item is not None
    assert item["role"] == "worker"
    assert item["kind"] == "tool"
    assert item["status"] == "running"
    assert item["title"] == "Okundu: src/a.py"
    assert item["id"] == "tool:exec-1:c1"
    assert item["runId"] == "run-1"
    assert item["seq"] == 1


def test_tool_titles_cover_every_registered_tool():
    cases = {
        "read_file": ({"path": "a.py"}, "Okundu: a.py"),
        "list_files": ({"path": "src"}, "Listelendi: src"),
        "search_text": ({"query": "foo"}, "Arandı: foo"),
        "write_file": ({"path": "a.py"}, "Düzenlendi: a.py"),
        "delete_path": ({"path": "a.py"}, "Silindi: a.py"),
        "run_process": ({"argv": ["pytest", "-q"]}, "Komut çalıştırıldı: pytest -q"),
        "repo_map": ({"query": "auth"}, "Depo haritası: auth"),
        "search_code": ({"query": "def foo"}, "Kod arandı: def foo"),
        "unknown_tool": ({}, "Araç: unknown_tool"),
    }
    for tool_name, (args, expected_title) in cases.items():
        ev = _ev(
            RunEventType.TOOL_REQUESTED,
            {"call_id": "c", "tool_name": tool_name, "arguments": args},
            execution_id="e",
        )
        assert project_event(ev)["title"] == expected_title


def test_tool_requested_started_completed_share_the_same_update_in_place_id():
    requested = _ev(RunEventType.TOOL_REQUESTED, {"call_id": "c1", "tool_name": "read_file",
                                                    "arguments": {"path": "a.py"}}, seq=1, execution_id="e1")
    started = _ev(RunEventType.TOOL_STARTED, {"call_id": "c1", "tool_name": "read_file"},
                   seq=2, execution_id="e1")
    completed = _ev(RunEventType.TOOL_COMPLETED, {"call_id": "c1", "tool_name": "read_file", "content": "ok",
                                                    "metadata": {}}, seq=3, execution_id="e1")
    ids = {project_event(e)["id"] for e in (requested, started, completed)}
    assert ids == {"tool:e1:c1"}
    assert project_event(completed)["status"] == "ok"


def test_tool_failed_status_error_with_bounded_detail():
    long_message = "x" * (MAX_DETAIL_CHARS + 500)
    ev = _ev(RunEventType.TOOL_FAILED, {
        "call_id": "c1", "tool_name": "run_process", "error_type": "Boom",
        "message": long_message, "stage": "execute", "recoverable": False,
    }, execution_id="e1")
    item = project_event(ev)
    assert item["status"] == "error"
    assert item["id"] == "tool:e1:c1"
    assert len(item["detail"]) == MAX_DETAIL_CHARS


# ---------------- verification.check_* ----------------

def test_verification_check_lifecycle():
    started = _ev(RunEventType.VERIFICATION_CHECK_STARTED, {
        "verification_id": "v1", "check_id": "unit", "name": "Unit tests",
        "argv": ["python3", "-m", "pytest", "-q"], "cwd": ".", "timeout_ms": 1000,
        "pass_exit_codes": [0], "error_exit_codes": [], "env_override_keys": [],
    })
    completed = _ev(RunEventType.VERIFICATION_CHECK_COMPLETED, {
        "verification_id": "v1", "check_id": "unit", "name": "Unit tests", "status": "pass",
        "exit_code": 0, "timed_out": False, "duration_ms": 500,
        "stdout": "9 passed", "stderr": "",
    })
    failed = _ev(RunEventType.VERIFICATION_CHECK_FAILED, {
        "verification_id": "v1", "check_id": "unit", "name": "Unit tests", "status": "fail",
        "error_type": "AssertionError", "error_message": "boom", "exit_code": 1,
        "timed_out": False, "duration_ms": 500, "stdout": "", "stderr": "1 failed",
    })
    s, c, f = (project_event(e) for e in (started, completed, failed))
    assert s["id"] == c["id"] == f["id"] == "check:v1:unit"
    assert s["role"] == "verification" and s["kind"] == "check" and s["status"] == "running"
    assert c["status"] == "ok" and "geçti" in c["title"]
    assert f["status"] == "error" and "başarısız" in f["title"]
    assert f["detail"] == "1 failed"


# ---------------- plan.* / review.* / fix_loop.* ----------------

def test_plan_lifecycle_ids_and_status():
    started = _ev(RunEventType.PLAN_STARTED, {"plan_id": "p1"})
    completed = _ev(RunEventType.PLAN_COMPLETED, {"plan_id": "p1", "summary": "do it"})
    failed = _ev(RunEventType.PLAN_FAILED, {"plan_id": "p1", "error_type": "E", "error_message": "bad"})
    assert project_event(started)["id"] == project_event(completed)["id"] == project_event(failed)["id"] == "plan:p1"
    assert project_event(started)["role"] == "planner"
    assert project_event(completed)["status"] == "ok"
    assert project_event(failed)["status"] == "error"


def test_review_lifecycle_ids_and_status():
    started = _ev(RunEventType.REVIEW_STARTED, {"review_id": "r1"})
    completed = _ev(RunEventType.REVIEW_COMPLETED, {"review_id": "r1", "verdict": "APPROVED", "summary": "ok"})
    assert project_event(started)["id"] == project_event(completed)["id"] == "review:r1"
    assert project_event(completed)["role"] == "reviewer"
    assert "APPROVED" in project_event(completed)["title"]


def test_fix_loop_and_attempt_ids():
    loop_started = _ev(RunEventType.FIX_LOOP_STARTED, {"fix_loop_id": "f1"})
    attempt_started = _ev(RunEventType.FIX_ATTEMPT_STARTED, {
        "fix_loop_id": "f1", "fix_attempt_id": "a1", "attempt_index": 1,
        "trigger_kind": "VERIFICATION_FAIL", "worker_execution_id": "we1", "before_diff_sha256": "x",
    })
    attempt_completed = _ev(RunEventType.FIX_ATTEMPT_COMPLETED, {
        "fix_loop_id": "f1", "fix_attempt_id": "a1", "attempt_index": 1,
        "worker_execution_id": "we1", "before_diff_sha256": "x", "after_diff_sha256": "y", "changed": True,
    })
    loop_completed = _ev(RunEventType.FIX_LOOP_COMPLETED, {
        "fix_loop_id": "f1", "attempts_used": 1, "final_execution_id": "we1",
        "verification_id": "v1", "review_id": "r1", "diff_sha256": "y",
    })
    assert project_event(loop_started)["id"] == project_event(loop_completed)["id"] == "fix_loop:f1"
    assert project_event(attempt_started)["id"] == project_event(attempt_completed)["id"] == "fix_attempt:f1:a1"
    assert project_event(loop_started)["role"] == "fix"


# ---------------- usage.recorded / execution.* ----------------

def test_usage_recorded_is_a_standalone_note():
    ev = _ev(RunEventType.USAGE_RECORDED, {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.002,
    }, execution_id="e1")
    item = project_event(ev)
    assert item["kind"] == "usage"
    assert "150 token" in item["title"]
    assert "0.0020" in item["title"]


def test_execution_lifecycle_is_worker_stage():
    started = _ev(RunEventType.EXECUTION_STARTED, {"transport": "native", "task": "t"}, execution_id="e1")
    completed = _ev(RunEventType.EXECUTION_COMPLETED, {"final_text": "", "model_turns": 1, "tool_calls": 0,
                                                          "tool_errors": 0, "input_tokens": 1, "output_tokens": 1,
                                                          "cost_usd": None}, execution_id="e1")
    assert project_event(started)["id"] == project_event(completed)["id"] == "execution:e1"
    assert project_event(started)["role"] == "worker"
    assert project_event(completed)["status"] == "ok"


# ---------------- execution.output (ACP worker transport) ----------------

def _acp_output(update, *, execution_id="e-acp"):
    return _ev(RunEventType.EXECUTION_OUTPUT, {
        "transport": "acp", "session_id": "s1", "update": update, "serialized_chars": 10,
    }, execution_id=execution_id)


def test_acp_tool_call_start_mapped_to_running_tool_item():
    ev = _acp_output({
        "toolCallId": "tc1", "title": "Read src/x.py", "kind": "read",
        "status": "in_progress", "sessionUpdate": "tool_call",
    })
    item = project_event(ev)
    assert item is not None
    assert item["id"] == "tool:e-acp:tc1"
    assert item["role"] == "worker"
    assert item["status"] == "running"
    assert item["title"] == "Okundu: Read src/x.py"


def test_acp_tool_call_update_completed_mapped_to_ok():
    ev = _acp_output({"toolCallId": "tc1", "status": "completed", "sessionUpdate": "tool_call_update"})
    item = project_event(ev)
    assert item["status"] == "ok"
    assert item["id"] == "tool:e-acp:tc1"


def test_acp_thought_and_message_chunks_are_dropped():
    thought = _acp_output({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
    message = _acp_output({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}})
    assert project_event(thought) is None
    assert project_event(message) is None


def test_non_acp_execution_output_is_out_of_scope():
    ev = _ev(RunEventType.EXECUTION_OUTPUT, {"transport": "native", "chunk": "..."}, execution_id="e1")
    assert project_event(ev) is None


# ---------------- agent.activity passthrough ----------------

def test_agent_activity_passthrough_with_tool_call_id():
    ev = _ev(RunEventType.AGENT_ACTIVITY, {
        "role": "reviewer", "kind": "tool", "title": "Okundu: a.py", "status": "ok", "tool_call_id": "tc9",
    }, execution_id="acp_review_exec_r1")
    item = project_event(ev)
    assert item["role"] == "reviewer"
    assert item["id"] == "activity_tool:acp_review_exec_r1:reviewer:tc9"


def test_agent_activity_passthrough_without_tool_call_id_uses_event_id():
    ev = _ev(RunEventType.AGENT_ACTIVITY, {
        "role": "planner", "kind": "note", "title": "Düşünüyor…", "status": "info",
    })
    item = project_event(ev)
    assert item["id"] == f"activity:{ev.event_id}"


def test_agent_activity_with_malformed_payload_is_skipped_not_raised():
    # Defensive: an agent.activity event missing a required field never
    # raises out of project_event -- it is simply out of scope.
    ev = _ev(RunEventType.AGENT_ACTIVITY, {"role": "planner"})
    assert project_event(ev) is None


# ---------------- out-of-scope types ----------------

def test_decision_made_is_a_one_line_note():
    ev = _ev(RunEventType.DECISION_MADE, {
        "decision_id": "verification_failure_triage", "answers": {
            "failure_kind": {"kind": "choice", "choice": "missing_dependency", "probabilities": {}, "confidence": 0.87},
        },
    })
    item = project_event(ev)
    assert item["role"] == "system"
    assert item["kind"] == "note"
    assert item["title"] == "Karar: missing_dependency (güven %87)"


def test_decision_made_with_malformed_payload_is_skipped_not_raised():
    assert project_event(_ev(RunEventType.DECISION_MADE, {})) is None
    assert project_event(_ev(RunEventType.DECISION_MADE, {"answers": {}})) is None


def test_unknown_event_type_returns_none():
    ev = _ev(RunEventType.RUN_CREATED, {})
    assert project_event(ev) is None


def test_checkpoint_and_proposal_events_are_out_of_scope():
    for type_, payload in (
        (RunEventType.CHECKPOINT_CREATED, {}),
        (RunEventType.PROPOSAL_READY, {}),
        (RunEventType.PROPOSAL_APPLIED, {}),
    ):
        assert project_event(_ev(type_, payload)) is None


# ---------------- helpers ----------------

def test_acp_tool_title_verb_mapping_and_bounds():
    assert acp_tool_title("read", "a.py") == "Okundu: a.py"
    assert acp_tool_title("edit", "") == "Düzenlendi"
    assert acp_tool_title(None, "x") == "İşlem: x"


def test_title_and_id_never_exceed_bounds_even_for_pathological_input():
    ev = _ev(RunEventType.TOOL_REQUESTED, {
        "call_id": "c1", "tool_name": "read_file", "arguments": {"path": "a" * 10_000},
    }, execution_id="e1")
    item = project_event(ev)
    assert len(item["title"]) <= MAX_TITLE_CHARS
