from types import SimpleNamespace

from run_runtime.models import RunStatus
from webhost.run_registry import RunRegistry, RunSlot


class Coordinator:
    def __init__(self, status):
        self.status = status

    def get_run(self):
        return SimpleNamespace(status=self.status)


def slot(run_id, root, status=RunStatus.RUNNING, workspace=object()):
    return RunSlot(run_id, f"task-{run_id}", root, "provider", Coordinator(status), workspace=workspace)


def test_capacity_is_two_per_project_and_clean_terminal_history_does_not_block():
    registry = RunRegistry()
    root = "/project"
    assert registry.reserve(root) and registry.reserve(root)
    assert not registry.reserve(root)
    registry.add(slot("a", root))
    registry.add(slot("b", root))
    assert len(registry.open_slots(root)) == 2
    a, b = registry.get("a"), registry.get("b")
    a.coordinator.status = RunStatus.SUCCEEDED
    b.coordinator.status = RunStatus.CANCELLED
    a.workspace = b.workspace = None
    assert not registry.open_slots(root)
    assert registry.reserve(root) and registry.reserve(root)
    assert not registry.reserve(root)


def test_terminal_cleanup_failure_remains_owned_and_consumes_capacity():
    registry = RunRegistry()
    root = "/project"
    assert registry.reserve(root)
    retained = slot("failed-cleanup", root, RunStatus.FAILED, workspace=object())
    registry.add(retained)
    assert registry.open_slots(root) == (retained,)
    assert registry.reserve(root)
    assert not registry.reserve(root)
    assert registry.get("failed-cleanup") is retained
    assert not registry.forget_cleaned("failed-cleanup")
    assert registry.open_slots("/other-project") == ()
    assert registry.open_slots() == (retained,)


def test_terminal_live_streamer_without_workspace_consumes_capacity_until_released():
    registry = RunRegistry()
    root = "/project"
    streamer = object()
    retained = slot("streaming", root, RunStatus.FAILED, workspace=None)
    retained.activity_streamer = streamer
    registry.add(retained)

    assert registry.open_slots(root) == (retained,)
    assert registry.reserve(root)
    assert not registry.reserve(root)

    retained.activity_streamer = None
    assert registry.open_slots(root) == ()
    assert registry.reserve(root)


def test_terminal_history_is_bounded_without_evicting_an_owned_run():
    registry = RunRegistry()
    owned = slot("owned", "/project")
    registry.add(owned)
    for index in range(40):
        cleaned = slot(f"clean-{index}", "/project", RunStatus.SUCCEEDED, workspace=None)
        cleaned.phase = "applied"
        registry.add(cleaned)
    assert len(registry.slots()) == registry.HISTORY + 1
    assert registry.get("owned") is owned
    assert registry.get("clean-0") is None
    assert registry.get("clean-39") is not None
