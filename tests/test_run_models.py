"""run_runtime.models / run_runtime.events alan modeli sözleşmesi (saf Python, SQLite yok)."""

import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_runtime.errors import EventValidationError  # noqa: E402
from run_runtime.events import RunEvent, RunEventType, build_event, validate_event_payload  # noqa: E402
from run_runtime.jsonutil import (  # noqa: E402
    canonical_dict_copy,
    canonical_json_dumps,
    canonical_optional_dict_copy,
    validate_json_dict,
    validate_json_value,
)
from run_runtime.models import (  # noqa: E402
    RunPhase,
    RunRecord,
    RunStatus,
    TaskRecord,
    new_event_id,
    new_run_id,
    new_task_id,
    utcnow,
)


def test_task_record_valid_construction():
    task = TaskRecord(task_id="task_x", project_root="/tmp/proj", prompt="do X", created_at=utcnow())
    assert task.task_id == "task_x"
    assert task.created_at.tzinfo is not None


def test_run_record_new_sets_created_projection_defaults():
    run = RunRecord.new(run_id="run_x", task_id="task_x")
    assert run.status == RunStatus.CREATED
    assert run.phase == RunPhase.CREATED
    assert run.last_event_seq == 0
    assert run.attempt == 1
    assert run.retry_of_run_id is None
    assert run.routing == {}
    assert run.budget is None
    assert run.workspace_snapshot is None
    assert run.prompt_tokens == run.completion_tokens == run.total_tokens == 0
    assert run.cost_usd == run.latency_s == 0.0
    assert run.created_at.tzinfo is not None
    assert run.started_at is None and run.finished_at is None


def test_run_record_new_copies_mutable_inputs_defensively():
    routing = {"planner": "a"}
    run = RunRecord.new(run_id="run_x", task_id="task_x", routing=routing)
    routing["planner"] = "mutated"
    assert run.routing == {"planner": "a"}  # dışarıdaki mutasyon içeri sızmaz


def test_task_record_is_immutable():
    task = TaskRecord(task_id="task_x", project_root="/tmp", prompt="p", created_at=utcnow())
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.prompt = "changed"  # type: ignore[misc]


def test_run_record_is_immutable():
    run = RunRecord.new(run_id="run_x", task_id="task_x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.status = RunStatus.RUNNING  # type: ignore[misc]


def test_run_record_updates_go_through_replace_not_mutation():
    run = RunRecord.new(run_id="run_x", task_id="task_x")
    updated = dataclasses.replace(run, status=RunStatus.RUNNING)
    assert run.status == RunStatus.CREATED  # orijinal değişmedi
    assert updated.status == RunStatus.RUNNING


def test_task_record_rejects_naive_datetime():
    with pytest.raises(ValueError):
        TaskRecord(task_id="t", project_root="/tmp", prompt="p", created_at=datetime.now())


def test_run_record_rejects_naive_created_at():
    with pytest.raises(ValueError):
        RunRecord.new(run_id="run_x", task_id="task_x", created_at=datetime.now())


def test_id_helpers_use_readable_prefixes_and_are_unique():
    task_ids = {new_task_id() for _ in range(50)}
    run_ids = {new_run_id() for _ in range(50)}
    event_ids = {new_event_id() for _ in range(50)}
    assert len(task_ids) == 50 and all(i.startswith("task_") for i in task_ids)
    assert len(run_ids) == 50 and all(i.startswith("run_") for i in run_ids)
    assert len(event_ids) == 50 and all(i.startswith("evt_") for i in event_ids)


def test_utcnow_is_timezone_aware_utc():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timezone.utc.utcoffset(None)


def test_build_event_produces_valid_event_with_defaults():
    event = build_event(
        run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={"a": 1},
    )
    assert event.run_id == "run_x"
    assert event.seq == 1
    assert event.type == "run.created"
    assert event.schema_version == 1
    assert event.source == "system"
    assert event.created_at.tzinfo is not None
    assert event.event_id.startswith("evt_")
    assert event.payload == {"a": 1}


def test_build_event_is_immutable():
    event = build_event(run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.seq = 2  # type: ignore[misc]


def test_build_event_copies_payload_defensively():
    payload = {"a": 1}
    event = build_event(run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload=payload)
    payload["a"] = 999
    assert event.payload == {"a": 1}


@pytest.mark.parametrize("bad_seq", [0, -1, -100])
def test_build_event_rejects_non_positive_seq(bad_seq):
    with pytest.raises(EventValidationError):
        build_event(run_id="run_x", seq=bad_seq, type=RunEventType.RUN_CREATED, payload={})


def test_build_event_rejects_empty_run_id():
    with pytest.raises(EventValidationError):
        build_event(run_id="", seq=1, type=RunEventType.RUN_CREATED, payload={})


def test_build_event_rejects_empty_type():
    with pytest.raises(EventValidationError):
        build_event(run_id="run_x", seq=1, type="", payload={})


def test_build_event_accepts_unknown_forward_compatible_type():
    """Depolama/olay modeli, RunEventType'ta henüz olmayan gelecekteki bir tür string'ini kabul eder."""
    event = build_event(run_id="run_x", seq=1, type="future.something_new", payload={"x": 1})
    assert event.type == "future.something_new"


def test_build_event_rejects_naive_created_at():
    with pytest.raises(ValueError):
        build_event(
            run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={},
            created_at=datetime.now(),
        )


def test_validate_event_payload_rejects_non_dict():
    with pytest.raises(EventValidationError):
        validate_event_payload(["not", "a", "dict"])
    with pytest.raises(EventValidationError):
        validate_event_payload("also not a dict")
    with pytest.raises(EventValidationError):
        validate_event_payload(None)


def test_validate_event_payload_rejects_non_json_serializable_values():
    class Unserializable:
        pass

    with pytest.raises(EventValidationError):
        validate_event_payload({"obj": Unserializable()})
    with pytest.raises(EventValidationError):
        validate_event_payload({"bytes": b"raw bytes"})


def test_validate_event_payload_accepts_json_compatible_dict():
    payload = {"a": 1, "b": "s", "c": [1, 2, 3], "d": {"nested": True}, "e": None}
    assert validate_event_payload(payload) == payload


# ---------------- 1: strict canonical JSON ----------------


def test_validate_json_value_rejects_nan_and_infinity_at_any_nesting_depth():
    for bad in (
        {"a": float("nan")},
        {"a": [1, {"b": float("inf")}]},
        {"a": {"b": {"c": float("-inf")}}},
        [float("nan")],
    ):
        with pytest.raises(EventValidationError):
            validate_json_value(bad)


def test_validate_json_value_rejects_nested_tuple_set_bytes_and_non_str_keys():
    with pytest.raises(EventValidationError):
        validate_json_value({"a": (1, 2)})
    with pytest.raises(EventValidationError):
        validate_json_value({"a": {1, 2}})
    with pytest.raises(EventValidationError):
        validate_json_value({"a": b"raw"})
    with pytest.raises(EventValidationError):
        validate_json_value({"a": [{"b": {1: "nested bad key"}}]})
    with pytest.raises(EventValidationError):
        validate_json_value({"a": [Path("/tmp")]})
    with pytest.raises(EventValidationError):
        validate_json_value({"a": [datetime.now(timezone.utc)]})


def test_validate_json_value_accepts_valid_nested_structure():
    validate_json_value({"a": [1, 2.5, "s", True, None, {"nested": [1, {"x": "y"}]}]})


def test_validate_json_dict_rejects_top_level_non_dict():
    for bad in ([], (), "x", 1, None, {1: "x"}):
        with pytest.raises(EventValidationError):
            validate_json_dict(bad, field="f")


def test_canonical_dict_copy_returns_independent_deep_copy():
    original = {"a": {"b": [1, 2, {"c": 3}]}}
    copy_ = canonical_dict_copy(original, field="f")
    assert copy_ == original
    original["a"]["b"].append(999)
    original["a"]["b"][2]["c"] = "mutated"
    assert copy_ == {"a": {"b": [1, 2, {"c": 3}]}}  # kopya, kaynaktaki mutasyondan etkilenmedi


def test_canonical_optional_dict_copy_passes_through_none():
    assert canonical_optional_dict_copy(None, field="f") is None


def test_canonical_json_dumps_rejects_nan_and_is_deterministic():
    with pytest.raises(ValueError):
        canonical_json_dumps({"a": float("nan")})
    out1 = canonical_json_dumps({"b": 1, "a": 2})
    out2 = canonical_json_dumps({"a": 2, "b": 1})
    assert out1 == out2  # sort_keys=True -> anahtar sırası girdi sırasından bağımsız


# ---------------- 2: finite usage numbers (build_event/payload doğrulaması) ----------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_event_payload_rejects_non_finite_floats_for_usage_like_fields(bad):
    with pytest.raises(EventValidationError):
        validate_event_payload({"cost_usd": bad})
    with pytest.raises(EventValidationError):
        validate_event_payload({"latency_s": bad})


# ---------------- 3: run metadata validation (RunRecord.new) ----------------


def test_run_record_new_rejects_non_dict_routing():
    with pytest.raises(EventValidationError):
        RunRecord.new(run_id="run_x", task_id="task_x", routing=[])


def test_run_record_new_rejects_non_string_routing_keys():
    with pytest.raises(EventValidationError):
        RunRecord.new(run_id="run_x", task_id="task_x", routing={1: "model"})


def test_run_record_new_rejects_non_json_value_in_routing():
    with pytest.raises(EventValidationError):
        RunRecord.new(run_id="run_x", task_id="task_x", routing={"x": Path("/tmp")})


def test_run_record_new_rejects_nan_in_budget():
    with pytest.raises(EventValidationError):
        RunRecord.new(run_id="run_x", task_id="task_x", budget={"max": float("nan")})


def test_run_record_new_rejects_tuple_in_workspace_snapshot():
    with pytest.raises(EventValidationError):
        RunRecord.new(run_id="run_x", task_id="task_x", workspace_snapshot={"x": (1, 2)})


def test_run_record_new_preserves_nested_valid_metadata_exactly():
    routing = {"planner": "claude", "nested": {"a": [1, 2, {"b": True}]}}
    budget = {"max_usd": 1.5, "tags": ["x", "y"]}
    snapshot = {"run_id": "wsrun", "meta": {"deep": {"value": None}}}
    run = RunRecord.new(
        run_id="run_x", task_id="task_x", routing=routing, budget=budget, workspace_snapshot=snapshot,
    )
    assert run.routing == routing
    assert run.budget == budget
    assert run.workspace_snapshot == snapshot


# ---------------- 4: event seq / schema_version validation ----------------


def test_build_event_rejects_boolean_seq():
    with pytest.raises(EventValidationError):
        build_event(run_id="run_x", seq=True, type=RunEventType.RUN_CREATED, payload={})


def test_build_event_rejects_string_seq():
    with pytest.raises(EventValidationError):
        build_event(run_id="run_x", seq="1", type=RunEventType.RUN_CREATED, payload={})


def test_build_event_rejects_zero_schema_version():
    with pytest.raises(EventValidationError):
        build_event(run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={}, schema_version=0)


def test_build_event_rejects_boolean_schema_version():
    with pytest.raises(EventValidationError):
        build_event(
            run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={}, schema_version=True,
        )


def test_build_event_rejects_string_schema_version():
    with pytest.raises(EventValidationError):
        build_event(
            run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={}, schema_version="1",
        )


# ---------------- 5: canonical UTC domain datetimes ----------------


def test_aware_non_utc_input_canonicalizes_to_equivalent_utc():
    plus3 = timezone(timedelta(hours=3))
    local = datetime(2026, 1, 1, 12, 0, 0, tzinfo=plus3)  # == 2026-01-01T09:00:00+00:00
    task = TaskRecord(task_id="t", project_root="/tmp", prompt="p", created_at=local)
    assert task.created_at.tzinfo == timezone.utc
    assert task.created_at == datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def test_run_record_started_at_and_finished_at_canonicalize_non_utc_input():
    plus3 = timezone(timedelta(hours=3))
    run = RunRecord.new(run_id="run_x", task_id="task_x")
    started = dataclasses.replace(run, started_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus3))
    assert started.started_at == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert started.started_at.tzinfo == timezone.utc


def test_naive_input_rejected_for_task_run_and_event():
    with pytest.raises(ValueError):
        TaskRecord(task_id="t", project_root="/tmp", prompt="p", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        RunRecord.new(run_id="run_x", task_id="task_x", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        build_event(
            run_id="run_x", seq=1, type=RunEventType.RUN_CREATED, payload={},
            created_at=datetime(2026, 1, 1),
        )


# ---------------- direct dataclass construction cannot bypass invariants ----------------
#
# RunRecord/RunEvent are public types; a valid domain object must not depend on
# callers remembering to use RunRecord.new()/build_event(). __post_init__ is the
# FINAL invariant boundary, so these tests construct the dataclasses DIRECTLY
# (bypassing the factories) and assert the same guarantees still hold.


def _direct_run_event(**overrides):
    kwargs = dict(
        event_id="evt_direct", run_id="run_x", seq=1, type="run.created", schema_version=1,
        created_at=utcnow(), execution_id=None, turn_id=None, item_id=None,
        causation_id=None, correlation_id=None, source="system", payload={"a": 1},
    )
    kwargs.update(overrides)
    return RunEvent(**kwargs)


def _direct_run_record(**overrides):
    kwargs = dict(
        run_id="run_x", task_id="task_x", status=RunStatus.CREATED, phase=RunPhase.CREATED,
        attempt=1, retry_of_run_id=None, routing={}, budget=None, workspace_snapshot=None,
        last_event_seq=0, prompt_tokens=0, completion_tokens=0, total_tokens=0,
        cost_usd=0.0, latency_s=0.0, created_at=utcnow(), started_at=None, finished_at=None,
        error_code=None, error_message=None,
    )
    kwargs.update(overrides)
    return RunRecord(**kwargs)


def test_run_event_direct_construction_rejects_nan_payload():
    with pytest.raises(EventValidationError):
        _direct_run_event(payload={"x": float("nan")})


def test_run_event_direct_construction_rejects_non_string_nested_key():
    with pytest.raises(EventValidationError):
        _direct_run_event(payload={"a": {1: "bad"}})


def test_run_event_direct_construction_canonicalizes_non_utc_created_at():
    plus3 = timezone(timedelta(hours=3))
    event = _direct_run_event(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus3))
    assert event.created_at == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert event.created_at.tzinfo == timezone.utc


def test_run_event_direct_construction_rejects_naive_created_at():
    with pytest.raises(ValueError):
        _direct_run_event(created_at=datetime(2026, 1, 1))


def test_run_event_direct_construction_defensively_copies_payload():
    payload = {"a": {"b": [1, 2]}}
    event = _direct_run_event(payload=payload)
    payload["a"]["b"].append(999)
    payload["c"] = "new"
    assert event.payload == {"a": {"b": [1, 2]}}  # dışarıdaki mutasyon içeri sızmadı


def test_run_event_direct_construction_valid_matches_build_event_semantics():
    fixed_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    direct = _direct_run_event(payload={"a": 1}, created_at=fixed_time, event_id="evt_direct")
    factory = build_event(
        run_id="run_x", seq=1, type="run.created", payload={"a": 1},
        created_at=fixed_time, event_id="evt_direct",
    )
    assert direct == factory


def test_run_record_direct_construction_rejects_invalid_routing():
    with pytest.raises(EventValidationError):
        _direct_run_record(routing={1: "bad"})


def test_run_record_direct_construction_rejects_invalid_budget():
    with pytest.raises(EventValidationError):
        _direct_run_record(budget={"max": float("nan")})


def test_run_record_direct_construction_rejects_invalid_workspace_snapshot():
    with pytest.raises(EventValidationError):
        _direct_run_record(workspace_snapshot={"x": (1, 2)})


def test_run_record_direct_construction_canonicalizes_non_utc_timestamps():
    plus3 = timezone(timedelta(hours=3))
    record = _direct_run_record(
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus3),
        started_at=datetime(2026, 1, 1, 13, 0, tzinfo=plus3),
        finished_at=datetime(2026, 1, 1, 14, 0, tzinfo=plus3),
    )
    assert record.created_at == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert record.started_at == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert record.finished_at == datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    for dt in (record.created_at, record.started_at, record.finished_at):
        assert dt.tzinfo == timezone.utc


def test_run_record_direct_construction_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        _direct_run_record(created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        _direct_run_record(started_at=datetime(2026, 1, 1))


def test_run_record_direct_construction_defensively_copies_metadata():
    routing = {"planner": "claude"}
    record = _direct_run_record(routing=routing)
    routing["planner"] = "mutated"
    assert record.routing == {"planner": "claude"}


def test_run_record_direct_construction_valid_matches_factory_semantics():
    fixed_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    direct = _direct_run_record(routing={"planner": "claude"}, created_at=fixed_time)
    factory = RunRecord.new(
        run_id="run_x", task_id="task_x", routing={"planner": "claude"}, created_at=fixed_time,
    )
    assert direct == factory
