import pytest

from collab_runtime.coordination import project_change
from collab_runtime.errors import ValidationError
from collab_runtime.models import parse_state_dict


def state(goal="", decisions=None, interfaces=None, tasks=None):
    return parse_state_dict({"schema": 1, "session_id": "demo", "target_version": "v1", "base_commit": "a" * 40,
        "context": {"goal": goal, "decisions": decisions or [], "interfaces": interfaces or {}}, "tasks": tasks or {}})


def task(status="queued", goal="task"):
    return {"owner": "alice", "goal": goal, "scopes": ["src/"], "status": status, "context_revision": "b" * 40}


def test_context_change_conservatively_affects_active_tasks_without_values():
    before = state(tasks={"one": task(), "two": task("done")})
    after = state("secret", ["secret decision"], {"api": "secret interface"}, {"one": task(), "two": task("done")})
    event = project_change(before, after, "c" * 40, "d" * 40)
    assert event["goalChanged"] and event["decisionsChanged"]
    assert event["interfaces"]["added"] == ["api"]
    assert event["affectedTaskIds"] == ["one"]
    assert "secret" not in str(event)


def test_task_only_isolation_checkpoint_and_validation():
    before = state(tasks={"one": task(), "two": task()})
    after = state(tasks={"one": task("done"), "two": task()})
    event = project_change(before, after, "c" * 40, "d" * 40)
    assert event["affectedTaskIds"] == ["one"] and not event["contextChanged"]
    assert project_change(before, before, "c" * 40, "d" * 40)["metadataCheckpoint"]
    with pytest.raises(ValidationError):
        project_change(before, after, "c" * 40, "c" * 40)
