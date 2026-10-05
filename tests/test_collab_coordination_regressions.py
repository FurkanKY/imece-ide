"""Regressions for the bounded metadata change projection.

Every case here is a pure in-process unit test over ``project_change``: two
already-validated states plus the two revision ids that name the adjacent pair.
No git, no listener, no model and no network are involved, and nothing outside
this module is monkeypatched.

What the contract claims and is pinned here: interface changes are reported by
IDENTIFIER only (values never appear), a context change conservatively affects
every still-active task (queued/running/waiting) but never a finished one, a
task-only change is isolated to that task id, added/removed tasks carry no
both-sided state and no field names, the ``fields`` list names exactly what
differed, both lists are capped at 32 with explicit totals and truncation flags
and a deterministic sort, an unchanged state at a new revision is an explicit
metadata checkpoint while the same revision may only carry the identical state,
and every malformed input (session identity, revision id, oversized or mistyped
state) is refused without mutating the caller's data and without leaking it.
"""

from __future__ import annotations

import json

import pytest

from collab_runtime.coordination import MAX_AFFECTED_TASKS, MAX_EVENT_TASKS, project_change
from collab_runtime.errors import ValidationError
from collab_runtime.models import (
    ACTIVE_STATUSES, ALL_STATUSES, MAX_GOAL_CHARS, MAX_INTERFACE_VALUE_CHARS, MAX_TASKS,
    parse_state_dict,
)


BASE_COMMIT = "a" * 40
FROM_REVISION = "b" * 40
TO_REVISION = "c" * 40
SECRET_GOAL = "goal-secret-9f2c"
SECRET_DECISION = "decision-secret-9f2c"
SECRET_VALUE = "value-secret-9f2c"


def task(status: str = "queued", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"owner": "alice", "goal": "task goal",
                                  "scopes": ["src/"], "status": status,
                                  "context_revision": "d" * 40}
    payload.update(overrides)
    return payload


def state(tasks: dict[str, dict[str, object]] | None = None, *, goal: str = "",
          decisions: list[str] | None = None, interfaces: dict[str, str] | None = None,
          **identity: object) -> object:
    return parse_state_dict({
        "schema": 1, "session_id": "demo", "target_version": "v1", "base_commit": BASE_COMMIT,
        "context": {"goal": goal, "decisions": list(decisions or []),
                    "interfaces": dict(interfaces or {})},
        "tasks": dict(tasks or {}), **identity})


def raw_state(tasks: dict[str, dict[str, object]] | None = None, *, goal: str = "",
              decisions: list[str] | None = None, interfaces: dict[str, str] | None = None,
              **identity: object) -> dict[str, object]:
    """A state payload that ``parse_state_dict`` has NOT validated yet."""
    return {
        "schema": 1, "session_id": "demo", "target_version": "v1", "base_commit": BASE_COMMIT,
        "context": {"goal": goal, "decisions": list(decisions or []),
                    "interfaces": dict(interfaces or {})},
        "tasks": dict(tasks or {}), **identity}


class _Unparsed:
    """A state-shaped object whose payload is only validated by the projection."""

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def to_dict(self) -> object:
        return self.payload


def blob(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


# --------------------------------------------------------------------------
# 1-2. interface changes are named by identifier only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bucket,before,after", [
    ("added", {}, {"api": SECRET_VALUE}),
    ("removed", {"api": SECRET_VALUE}, {}),
    ("changed", {"api": "v1-" + SECRET_VALUE}, {"api": "v2-" + SECRET_VALUE}),
    ("mixed", {"gone": SECRET_VALUE, "api": "v1-" + SECRET_VALUE, "stay": "same"},
     {"api": "v2-" + SECRET_VALUE, "fresh": SECRET_VALUE, "stay": "same"}),
], ids=["added", "removed", "changed", "mixed"])
def test_interface_changes_report_identifiers_and_never_values(bucket, before, after):
    tasks = {"task-a": task("running")}
    event = project_change(state(tasks, interfaces=before), state(tasks, interfaces=after),
                           FROM_REVISION, TO_REVISION)
    interfaces = event["interfaces"]
    assert set(interfaces) == {"added", "removed", "changed"}
    assert interfaces["added"] == sorted(interfaces["added"])
    assert interfaces["removed"] == sorted(interfaces["removed"])
    assert interfaces["changed"] == sorted(interfaces["changed"])
    assert not (set(interfaces["added"]) & set(interfaces["removed"]))
    assert not (set(interfaces["added"]) & set(interfaces["changed"]))
    assert not (set(interfaces["removed"]) & set(interfaces["changed"]))
    if bucket == "mixed":
        assert interfaces == {"added": ["fresh"], "removed": ["gone"], "changed": ["api"]}
    else:
        assert interfaces[bucket] == ["api"]
        assert sum(len(interfaces[key]) for key in interfaces) == 1

    payload = blob(event)
    assert SECRET_VALUE not in payload and "v1-value" not in payload and "v2-value" not in payload
    # An unchanged interface is never reported, and no task is invented by it.
    assert "stay" not in payload
    assert event["taskChanges"] == [] and event["affectedTaskIds"] == ["task-a"]


# --------------------------------------------------------------------------
# 3-4. a context change conservatively affects active tasks only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [*ACTIVE_STATUSES, "done"], ids=list(ALL_STATUSES))
@pytest.mark.parametrize("change,goal_changed,decisions_changed", [
    ({"goal": SECRET_GOAL}, True, False),
    ({"decisions": [SECRET_DECISION]}, False, True),
], ids=["goal", "decisions"])
def test_a_context_change_affects_every_active_task_and_no_finished_one(
        status, change, goal_changed, decisions_changed):
    tasks = {"active": task(status), "finished": task("done")}
    event = project_change(state(tasks), state(tasks, **change), FROM_REVISION, TO_REVISION)
    assert event["goalChanged"] is goal_changed
    assert event["decisionsChanged"] is decisions_changed
    assert event["contextChanged"] is True
    expected = ["active"] if status in ACTIVE_STATUSES else []
    assert event["affectedTaskIds"] == expected
    assert event["affectedTaskCount"] == len(expected)
    assert event["affectedTasksTruncated"] is False
    # A context change is not a task change: no task fields are reported.
    assert event["taskChanges"] == [] and event["taskChangeCount"] == 0
    payload = blob(event)
    assert SECRET_GOAL not in payload and SECRET_DECISION not in payload


def test_an_interface_only_change_is_also_conservative_for_active_tasks():
    tasks = {"active": task("waiting"), "finished": task("done")}
    event = project_change(state(tasks), state(tasks, interfaces={"api": "v1"}),
                           FROM_REVISION, TO_REVISION)
    assert event["contextChanged"] is True
    assert event["goalChanged"] is False and event["decisionsChanged"] is False
    assert event["interfaces"]["added"] == ["api"]
    assert event["affectedTaskIds"] == ["active"] and event["affectedTaskCount"] == 1


# --------------------------------------------------------------------------
# 5-7. task-level change reporting
# --------------------------------------------------------------------------


@pytest.mark.parametrize("moved,changed", [
    ("queued", False), ("running", True), ("waiting", True), ("done", True),
], ids=["queued-unchanged", "running", "waiting", "done"])
def test_a_task_only_change_is_isolated_to_that_task(moved, changed):
    tasks = {"task-a": task("queued"), "task-b": task("queued"), "task-c": task("done")}
    after = dict(tasks, **{"task-b": task(moved)})
    event = project_change(state(tasks), state(after), FROM_REVISION, TO_REVISION)
    assert event["contextChanged"] is False
    assert event["goalChanged"] is False and event["decisionsChanged"] is False
    assert event["interfaces"] == {"added": [], "removed": [], "changed": []}
    affected = ["task-b"] if changed else []
    assert event["affectedTaskIds"] == affected == [record["taskId"] for record in event["taskChanges"]]
    assert event["taskChangeCount"] == len(affected)
    assert event["taskChangesTruncated"] is False and event["affectedTasksTruncated"] is False


@pytest.mark.parametrize("bucket,before,after,expected", [
    ("added", {"task-a": task("queued")},
     {"task-a": task("queued"), "task-b": task("running", owner="bob")},
     {"taskId": "task-b", "change": "added", "previousStatus": None, "status": "running",
      "previousOwner": None, "owner": "bob", "fields": []}),
    ("removed", {"task-a": task("queued"), "task-b": task("running", owner="bob")},
     {"task-a": task("queued")},
     {"taskId": "task-b", "change": "removed", "previousStatus": "running", "status": None,
      "previousOwner": "bob", "owner": None, "fields": []}),
], ids=["added", "removed"])
def test_added_and_removed_tasks_carry_neither_a_field_list_nor_a_both_sided_state(
        bucket, before, after, expected):
    event = project_change(state(before), state(after), FROM_REVISION, TO_REVISION)
    assert [record for record in event["taskChanges"]] == [expected]
    assert set(event["taskChanges"][0]) == {"taskId", "change", "previousStatus", "status",
                                            "previousOwner", "owner", "fields"}
    assert event["affectedTaskIds"] == ["task-b"] and event["affectedTaskCount"] == 1
    assert event["taskChangeCount"] == 1 and event["taskChangesTruncated"] is False


@pytest.mark.parametrize("field,mutation", [
    ("goal", {"goal": "a different goal"}),
    ("assignment", {"owner": "bob"}),
    ("scopes", {"scopes": ["docs/"]}),
    ("contextRevision", {"context_revision": "e" * 40}),
    ("status", {"status": "running"}),
], ids=["goal", "assignment", "scopes", "contextRevision", "status"])
def test_a_changed_task_names_only_the_field_that_differed(field, mutation):
    before = state({"task-a": task("queued"), "task-b": task("queued", owner="bob")})
    after = state({"task-a": task(**{"status": "queued", **mutation}),
                   "task-b": task("queued", owner="bob")})
    records = project_change(before, after, FROM_REVISION, TO_REVISION)["taskChanges"]
    assert len(records) == 1
    record = records[0]
    assert record["taskId"] == "task-a" and record["change"] == "changed"
    assert record["fields"] == [field]
    assert record["previousStatus"] == "queued"
    assert record["status"] == ("running" if field == "status" else "queued")
    assert record["previousOwner"] == "alice"
    assert record["owner"] == ("bob" if field == "assignment" else "alice")


def test_several_changed_fields_are_named_in_the_documented_order():
    before = state({"task-a": task("queued")})
    after = state({"task-a": task("running", owner="bob", goal="other goal",
                                  scopes=["docs/"], context_revision="e" * 40)})
    record = project_change(before, after, FROM_REVISION, TO_REVISION)["taskChanges"][0]
    assert record["fields"] == ["goal", "assignment", "scopes", "contextRevision", "status"]
    assert record["previousOwner"] == "alice" and record["owner"] == "bob"
    assert record["previousStatus"] == "queued" and record["status"] == "running"


# --------------------------------------------------------------------------
# 8-10. bounded records, bounded affected ids, deterministic order
# --------------------------------------------------------------------------


@pytest.mark.parametrize("changed,records,truncated", [
    (31, 31, False), (32, MAX_EVENT_TASKS, False), (33, MAX_EVENT_TASKS, True),
    (40, MAX_EVENT_TASKS, True),
])
def test_task_records_are_bounded_while_the_total_is_explicit(changed, records, truncated):
    before = {f"task-{index:02d}": task("queued") for index in range(40)}
    after = dict(before)
    for index in range(changed):
        after[f"task-{index:02d}"] = task("running")
    event = project_change(state(before), state(after), FROM_REVISION, TO_REVISION)
    assert len(event["taskChanges"]) == records
    assert MAX_EVENT_TASKS == 32
    assert event["taskChangeCount"] == changed
    assert event["taskChangesTruncated"] is truncated
    reported = [record["taskId"] for record in event["taskChanges"]]
    changed_ids = sorted(f"task-{index:02d}" for index in range(changed))
    assert reported == sorted(reported) == changed_ids[:records]
    assert all(record["fields"] == ["status"] for record in event["taskChanges"])
    assert event["affectedTaskIds"] == changed_ids[:MAX_AFFECTED_TASKS]
    assert event["affectedTaskCount"] == changed
    assert event["affectedTasksTruncated"] is (changed > MAX_AFFECTED_TASKS)


@pytest.mark.parametrize("active,reported,truncated", [
    (31, 31, False), (32, MAX_AFFECTED_TASKS, False), (33, MAX_AFFECTED_TASKS, True),
    (64, MAX_AFFECTED_TASKS, True),
])
def test_affected_ids_are_bounded_sorted_and_counted(active, reported, truncated):
    tasks = {f"task-{index:02d}": task("queued") for index in range(active)}
    tasks.update({f"done-{index:02d}": task("done") for index in range(3)})
    before, after = state(tasks), state(tasks, goal="the goal moved")
    event = project_change(before, after, FROM_REVISION, TO_REVISION)
    assert len(event["affectedTaskIds"]) == reported
    assert event["affectedTaskCount"] == active
    assert event["affectedTasksTruncated"] is truncated
    assert event["affectedTaskIds"] == sorted(event["affectedTaskIds"])
    assert all(task_id.startswith("task-") for task_id in event["affectedTaskIds"])
    assert blob(project_change(before, after, FROM_REVISION, TO_REVISION)) == blob(event)


# --------------------------------------------------------------------------
# 11-12. checkpoints and same-revision compatibility
# --------------------------------------------------------------------------


def test_an_unchanged_state_at_a_new_revision_is_an_explicit_metadata_checkpoint():
    before = state({"task-a": task("queued"), "task-b": task("done")})
    event = project_change(before, before, FROM_REVISION, TO_REVISION)
    assert event["metadataCheckpoint"] is True
    assert event["fromRevision"] == FROM_REVISION and event["toRevision"] == TO_REVISION
    assert event["taskChanges"] == [] and event["taskChangeCount"] == 0
    assert event["taskChangesTruncated"] is False
    assert event["affectedTaskIds"] == [] and event["affectedTaskCount"] == 0
    assert event["affectedTasksTruncated"] is False
    assert event["contextChanged"] is False
    assert event["goalChanged"] is False and event["decisionsChanged"] is False
    assert event["interfaces"] == {"added": [], "removed": [], "changed": []}


def test_the_same_revision_may_only_carry_the_identical_state():
    before = state({"task-a": task("queued")})
    identical = project_change(before, before, FROM_REVISION, FROM_REVISION)
    assert identical["metadataCheckpoint"] is False  # no new revision was published
    assert identical["fromRevision"] == identical["toRevision"] == FROM_REVISION
    for change in ({"task-a": task("running")}, {"task-a": task("queued"), "task-b": task("queued")}):
        with pytest.raises(ValidationError, match="same revision has different state"):
            project_change(before, state(change), FROM_REVISION, FROM_REVISION)


# --------------------------------------------------------------------------
# 13-15. strict refusal of foreign identity, loose revisions and bad states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("session_id", "other"), ("base_commit", "e" * 40), ("target_version", "v2"),
], ids=["session_id", "base_commit", "target_version"])
def test_a_different_session_identity_is_never_projected(field, value):
    before = state({"task-a": task("queued")})
    after = state({"task-a": task("running")}, **{field: value})
    with pytest.raises(ValidationError, match="identity mismatch"):
        project_change(before, after, FROM_REVISION, TO_REVISION)


@pytest.mark.parametrize("bad", [
    "z" * 40, "a" * 39, "a" * 41, "", "A" * 40, FROM_REVISION + "\n", 7, None, b"a" * 40,
], ids=["non-hex", "short", "long", "empty", "uppercase", "newline", "int", "none", "bytes"])
def test_a_revision_argument_must_be_a_full_lowercase_sha(bad):
    before = state({"task-a": task("queued")})
    for from_revision, to_revision in ((bad, TO_REVISION), (FROM_REVISION, bad)):
        with pytest.raises(ValidationError, match="invalid change projection input"):
            project_change(before, before, from_revision, to_revision)


@pytest.mark.parametrize("case", [
    "too-many-tasks", "oversized-goal", "oversized-interface-value", "oversized-decision",
    "unknown-task-key", "unknown-state-key", "unknown-status", "schema",
])
def test_a_state_the_projection_cannot_strictly_parse_is_refused(case):
    payloads: dict[str, object] = {
        "too-many-tasks": raw_state({f"task-{index:03d}": task("queued")
                                     for index in range(MAX_TASKS + 1)}),
        "oversized-goal": raw_state(goal="g" * (MAX_GOAL_CHARS + 1)),
        "oversized-interface-value": raw_state(interfaces={"api": "v" * (MAX_INTERFACE_VALUE_CHARS + 1)}),
        "oversized-decision": raw_state(decisions=["d" * 2001]),
        "unknown-task-key": raw_state({"task-a": dict(task("queued"), extra="value")}),
        "unknown-state-key": raw_state({}, unexpected="value"),
        "unknown-status": raw_state({"task-a": task("archived")}),
        "schema": raw_state({}, schema=2),
    }
    payload = payloads[case]
    before, after = json.dumps(payload, sort_keys=True), payload
    for previous, current in ((payload, payload), (state({"task-a": task("queued")}), payload)):
        with pytest.raises(ValidationError, match="invalid change projection input"):
            project_change(_Unparsed(previous), _Unparsed(current), FROM_REVISION, TO_REVISION)
    assert payload == after and json.dumps(payload, sort_keys=True) == before

    for not_a_state in (None, "state", 7, {"schema": 1, "tasks": {}}):
        with pytest.raises(ValidationError, match="invalid change projection input"):
            project_change(not_a_state, not_a_state, FROM_REVISION, TO_REVISION)


# --------------------------------------------------------------------------
# 16-17. display contract, input immutability and detached results
# --------------------------------------------------------------------------


def test_the_event_shape_is_exactly_the_documented_display_contract():
    before = state({"task-a": task("queued")})
    after = state({"task-a": task("running")}, goal="moved", decisions=["d"],
                  interfaces={"api": "v1"})
    event = project_change(before, after, FROM_REVISION, TO_REVISION)
    assert set(event) == {"fromRevision", "toRevision", "metadataCheckpoint", "goalChanged",
                          "decisionsChanged", "interfaces", "taskChanges", "taskChangeCount",
                          "taskChangesTruncated", "affectedTaskIds", "affectedTaskCount",
                          "affectedTasksTruncated", "contextChanged"}
    assert blob(event) == blob(json.loads(blob(event)))  # plain JSON values only
    assert all(revision == revision.lower() for revision in (event["fromRevision"], event["toRevision"]))


def test_the_projection_never_mutates_its_inputs_and_returns_detached_results():
    before = state({"task-a": task("queued"), "task-b": task("running", owner="bob")})
    after = state({"task-a": task("running"), "task-b": task("running", owner="bob")},
                  goal="the goal moved", interfaces={"api": "v1"})
    before_json, after_json = before.to_dict(), after.to_dict()

    event = project_change(before, after, FROM_REVISION, TO_REVISION)
    assert before.to_dict() == before_json and after.to_dict() == after_json
    assert event["affectedTaskIds"] == ["task-a", "task-b"]

    event["interfaces"]["added"].append("injected")
    event["taskChanges"][0]["fields"].append("injected")
    event["affectedTaskIds"].append("injected")
    fresh = project_change(before, after, FROM_REVISION, TO_REVISION)
    assert fresh["interfaces"]["added"] == ["api"]
    assert fresh["taskChanges"][0]["fields"] == ["status"]
    assert fresh["affectedTaskIds"] == ["task-a", "task-b"]