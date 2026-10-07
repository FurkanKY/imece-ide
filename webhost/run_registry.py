"""Process-local ownership for bounded native-agent runs."""
from dataclasses import dataclass, field
import threading
from run_runtime.models import RunStatus


@dataclass
class RunSlot:
    run_id: str
    task_id: str
    project_root: str
    provider_id: str
    coordinator: object
    worker: object = None
    workspace: object = None
    ports: object = None
    task: str = ""
    pinned_paths: list = field(default_factory=list)
    cancel_event: object = None
    proposals: list = field(default_factory=list)
    evidence: dict | None = None
    checkpoint_id: str | None = None
    activity_streamer: object = None
    phase: str = "starting"
    error_code: str | None = None
    retain_for_restart: bool = False
    totals: dict = field(default_factory=lambda: {"latency_s": None, "tokens": None, "cost_usd": None})
    lock: object = field(default_factory=threading.RLock, repr=False)


class RunRegistry:
    CAPACITY = 2
    HISTORY = 32

    def __init__(self):
        self._lock = threading.RLock()
        self._slots = {}
        self._reservations = {}

    def reserve(self, key):
        with self._lock:
            owned = 0
            for slot in self._slots.values():
                if slot.project_root != key or not _owns_live_resource(slot):
                    continue
                owned += 1
            if owned + self._reservations.get(key, 0) >= self.CAPACITY:
                return False
            self._reservations[key] = self._reservations.get(key, 0) + 1
            return True

    def release(self, key):
        with self._lock:
            count = self._reservations.get(key, 0)
            if count <= 1:
                self._reservations.pop(key, None)
            else:
                self._reservations[key] = count - 1

    def add(self, slot):
        with self._lock:
            count = self._reservations.get(slot.project_root, 0)
            if count <= 1:
                self._reservations.pop(slot.project_root, None)
            else:
                self._reservations[slot.project_root] = count - 1
            self._slots[slot.run_id] = slot
            self._prune_locked()

    def get(self, run_id):
        with self._lock:
            return self._slots.get(run_id)

    def slots(self):
        with self._lock:
            return tuple(self._slots.values())

    def open_slots(self, project_root=None):
        result = []
        for slot in self.slots():
            if project_root is not None and slot.project_root != project_root:
                continue
            try:
                if (_owns_live_resource(slot) or
                        slot.coordinator.get_run().status not in _TERMINAL_STATUSES):
                    result.append(slot)
            except Exception:
                result.append(slot)  # unreadable canonical state fails closed
        return tuple(result)

    def _prune_locked(self):
        terminal = []
        for key, slot in self._slots.items():
            ended = slot.phase in {status.value for status in _TERMINAL_STATUSES} | {
                "done", "applied", "rejected",
            }
            if ended and slot.worker is None and slot.workspace is None and slot.activity_streamer is None:
                terminal.append(key)
        for key in terminal[:-self.HISTORY]:
            self._slots.pop(key, None)

    def forget_cleaned(self, run_id):
        """Test/host teardown hook; never drops a live resource reference."""
        with self._lock:
            slot = self._slots.get(run_id)
            if slot is None:
                return True
            if slot.worker is not None or slot.workspace is not None or slot.activity_streamer is not None:
                return False
            self._slots.pop(run_id, None)
            return True


_TERMINAL_STATUSES = frozenset({
    RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED,
})


def _owns_live_resource(slot):
    """A slot stays open while any worker, workspace, or streamer is owned."""
    return slot.phase == "uncertain" or any(resource is not None for resource in
               (slot.worker, slot.workspace, slot.activity_streamer))


registry = RunRegistry()
