"""Opt-in validation/binding of collaboration context at native worker boundaries.

The host owns consumer lifecycle and supplies an authenticated authoritative
snapshot provider (for example ``lambda: coordinator.snapshot(credential)``).
This module never reads the workspace snapshot file, starts a consumer, or
invokes a model. The default binder uses saved render metadata and the
canonical bounded prompt renderer; an explicit callback remains a trusted
compatibility override.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import math
from time import monotonic, sleep
from types import MappingProxyType
from typing import Any

from collab_runtime.context import (
    SNAPSHOT_SECTION_HEADER, SharedSnapshot, parse_snapshot_dict, render_snapshot_block,
)
from collab_runtime.coordinator import Snapshot
from collab_runtime.consumer import RevisionConsumer
from collab_runtime.models import ACTIVE_STATUSES, Task
from fix_runtime.models import FixWorkerRequest, InitialWorkerRequest
from agent_runtime.cancellation import CancellationToken, OperationCancelledError

SAFE_POINT_ERROR = "Collaboration context could not be safely accepted; worker attempt was not started."
_RUNNABLE_CONSUMER_STATES = {"connecting", "streaming", "retrying", "inbox_full"}


class SafePointError(Exception):
    """Fixed, non-sensitive failure at an opted-in safe point."""

    def __init__(self) -> None:
        super().__init__(SAFE_POINT_ERROR)


@dataclass(frozen=True)
class PreparedWorkerInput:
    request: Any
    _consumer: RevisionConsumer
    _captured_consumed: str
    _ack_revision: str | None
    binding: SharedSnapshot | None = None

    def acknowledge(self, *, cancel_token: CancellationToken | None = None) -> None:
        try:
            _check_cancel(cancel_token)
            status = self._consumer.status()
            if (
                status.get("state") not in _RUNNABLE_CONSUMER_STATES
                or status.get("consumed_revision") != self._captured_consumed
            ):
                raise ValueError()
            _check_cancel(cancel_token)
            if self._ack_revision is not None:
                self._consumer.acknowledge_at_safe_point(self._ack_revision)
        except OperationCancelledError:
            raise
        except Exception:
            raise SafePointError() from None


def _check_cancel(cancel_token: CancellationToken | None) -> None:
    if cancel_token is not None:
        cancel_token.raise_if_cancelled()


def _check_consumer_status(consumer: RevisionConsumer, consumed: str) -> None:
    status = consumer.status()
    if (
        status.get("state") not in _RUNNABLE_CONSUMER_STATES
        or status.get("consumed_revision") != consumed
    ):
        raise ValueError()


class NativeWorkerSafePoint:
    """Bind one captured inbox prefix to one next worker attempt.

    ``approved_task`` and ``member_id`` are trusted host configuration, not
    values learned from the snapshot. A changed task identity/goal/scopes
    requires a new explicitly approved binding, rather than retargeting here.
    """

    def __init__(
        self,
        consumer: RevisionConsumer,
        snapshot_provider: Callable[[], Snapshot],
        *,
        initial_snapshot: Snapshot,
        member_id: str,
        approved_task: Task,
        bind_input: Callable[[SharedSnapshot, Any, Any], Any] | None = None,
        replay_wait_timeout: float = 0.0,
    ) -> None:
        if (
            isinstance(replay_wait_timeout, bool)
            or type(replay_wait_timeout) not in (int, float)
            or not 0 <= replay_wait_timeout <= 5
            or (type(replay_wait_timeout) is float and not math.isfinite(replay_wait_timeout))
        ):
            raise ValueError("replay_wait_timeout must be a finite number from 0 to 5 seconds.")
        self._consumer = consumer
        self._snapshot_provider = snapshot_provider
        self._member_id = member_id
        self._approved_task = approved_task
        self._replay_wait_timeout = float(replay_wait_timeout)
        if bind_input is None:
            from collab_runtime.binding import bind_worker_input

            bind_input = bind_worker_input
        self._bind_input = bind_input
        try:
            pinned = parse_snapshot_dict({
                "schema": 1,
                "revision": initial_snapshot.revision,
                "context_hash": initial_snapshot.state.context.content_hash,
                "state": initial_snapshot.state.to_dict(),
                "task_id": approved_task.id,
            })
            self._identity = (pinned.state.session_id, pinned.state.base_commit, pinned.state.target_version)
            if (
                consumer.session_identity != self._identity
                or consumer.member_id != member_id
            ):
                raise ValueError()
            self._validated(initial_snapshot)
        except Exception:
            raise SafePointError() from None

    def _validated(self, authoritative: Snapshot) -> SharedSnapshot:
        try:
            if not isinstance(authoritative, Snapshot):
                raise ValueError()
            snapshot = parse_snapshot_dict({
                "schema": 1,
                "revision": authoritative.revision,
                "context_hash": authoritative.state.context.content_hash,
                "state": authoritative.state.to_dict(),
                "task_id": self._approved_task.id,
            })
            selected = snapshot.selected_task
            if (snapshot.state.session_id, snapshot.state.base_commit, snapshot.state.target_version) != (
                self._consumer_identity
            ):
                raise ValueError()
            if selected.id != self._approved_task.id or selected.owner != self._member_id:
                raise ValueError()
            if selected.status not in ACTIVE_STATUSES:
                raise ValueError()
            if (selected.owner, selected.goal, selected.scopes) != (
                self._approved_task.owner, self._approved_task.goal, self._approved_task.scopes
            ):
                raise ValueError()
            immutable_state = type(snapshot.state)(
                snapshot.state.session_id,
                snapshot.state.target_version,
                snapshot.state.base_commit,
                snapshot.state.context,
                MappingProxyType(dict(snapshot.state.tasks)),
            )
            return SharedSnapshot(snapshot.revision, snapshot.context_hash, immutable_state, snapshot.task_id)
        except Exception:
            raise SafePointError() from None

    @property
    def _consumer_identity(self) -> tuple[str, str, str]:
        # The supplied approved initial snapshot pins identity independently
        # from later snapshots and from the workspace artifact.
        return self._identity

    def prepare(
        self, request: Any, workspace: Any, *, cancel_token: CancellationToken | None = None,
    ) -> PreparedWorkerInput:
        """Capture, validate and bind pending events for this attempt only."""
        deadline = monotonic() + self._replay_wait_timeout if self._replay_wait_timeout > 0 else None
        try:
            _check_cancel(cancel_token)
            status = self._consumer.status()
            if status.get("state") not in _RUNNABLE_CONSUMER_STATES:
                raise ValueError()
            pending = self._consumer.peek()
            consumed = status["consumed_revision"]
            captured = tuple(pending)
            initial_pending_count = len(captured)
            ack_revision = captured[-1].revision if captured else None
            authoritative = self._snapshot_provider()
            _check_cancel(cancel_token)
            _check_consumer_status(self._consumer, consumed)
            snapshot = self._validated(authoritative)
            if workspace.snapshot.source_head != snapshot.state.base_commit:
                raise ValueError()
            target_revision = ack_revision if ack_revision is not None else consumed
            if self._replay_wait_timeout == 0:
                if snapshot.revision != target_revision:
                    raise ValueError()
                covered_count = len(captured)
            else:
                # SHA values are opaque. Only the captured inbox's actual order
                # proves which prefix an authoritative snapshot covers.
                wait_target = None
                waiting_started = False
                while True:
                    _check_cancel(cancel_token)
                    _check_consumer_status(self._consumer, consumed)
                    if waiting_started and monotonic() >= deadline:
                        raise ValueError()
                    if snapshot.revision == target_revision:
                        covered_count = initial_pending_count
                        break
                    known = [event.revision for event in captured]
                    if snapshot.revision == consumed:
                        covered_count = 0
                        break
                    if snapshot.revision in known:
                        covered_count = known.index(snapshot.revision) + 1
                        break
                    if wait_target is None:
                        wait_target = snapshot.revision
                    waiting_started = True
                    if monotonic() >= deadline:
                        raise ValueError()
                    live_pending = tuple(self._consumer.peek())
                    # The single host cursor writer preserves this captured
                    # chain; accept only observed append-only progress.
                    if tuple(event.revision for event in live_pending[:len(captured)]) != tuple(
                        event.revision for event in captured
                    ):
                        raise ValueError()
                    captured = live_pending
                    target_arrived = wait_target in tuple(event.revision for event in captured)
                    if len(captured) >= 32 and not target_arrived:
                        raise ValueError()
                    if target_arrived:
                        _check_cancel(cancel_token)
                        authoritative = self._snapshot_provider()
                        _check_cancel(cancel_token)
                        if monotonic() >= deadline:
                            raise ValueError()
                        snapshot = self._validated(authoritative)
                        if workspace.snapshot.source_head != snapshot.state.base_commit:
                            raise ValueError()
                        wait_target = None
                        continue
                    timeout = min(0.05, max(0.0, deadline - monotonic()))
                    if cancel_token is None:
                        sleep(timeout)
                    elif cancel_token.wait(timeout):
                        _check_cancel(cancel_token)
                    if monotonic() >= deadline:
                        raise ValueError()
                    # The next loop iteration polls public state/inbox once;
                    # if the target arrived it will then refetch the snapshot.
            _check_cancel(cancel_token)
            _check_consumer_status(self._consumer, consumed)
            if self._replay_wait_timeout > 0 and waiting_started:
                if monotonic() >= deadline:
                    raise ValueError()
            _check_cancel(cancel_token)
            bound = self._bind_input(snapshot, request, workspace)
            _check_cancel(cancel_token)
            bound = self._validate_replacement(request, bound, snapshot)
            _check_cancel(cancel_token)
            ack_revision = captured[covered_count - 1].revision if covered_count else None
            return PreparedWorkerInput(bound, self._consumer, consumed, ack_revision, snapshot)
        except OperationCancelledError:
            raise
        except SafePointError:
            raise
        except Exception:
            raise SafePointError() from None

    def accept_reset(self, snapshot: Snapshot, request: Any, workspace: Any,
                     cancel_token: Any = None) -> Any:
        """Explicit host-approved reset, binding first and resetting last."""
        try:
            _check_cancel(cancel_token)
            validated = self._validated(snapshot)
            if workspace.snapshot.source_head != validated.state.base_commit:
                raise ValueError()
            bound = self._bind_input(validated, request, workspace)
            _check_cancel(cancel_token)
            bound = self._validate_replacement(request, bound, validated)
            _check_cancel(cancel_token)
            self._consumer.reset_at_safe_point(snapshot)
            return bound
        except OperationCancelledError:
            raise
        except Exception:
            raise SafePointError() from None

    @staticmethod
    def _validate_replacement(original: Any, replacement: Any, snapshot: SharedSnapshot) -> Any:
        if type(original) not in (FixWorkerRequest, InitialWorkerRequest) or type(original) is not type(replacement):
            raise ValueError()
        for field in ("task", "plan", "trigger", "attempt_index", "render_context"):
            if hasattr(original, field) and getattr(original, field) != getattr(replacement, field):
                raise ValueError()
        text = replacement.rendered_input
        canonical = render_snapshot_block(snapshot)
        if text.count(SNAPSHOT_SECTION_HEADER) != 1 or text.count(canonical) != 1:
            raise ValueError()
        # Reconstruct even frozen requests: callers can bypass frozen dataclass
        # validation with object.__setattr__, so the bound input must be checked
        # again at the model boundary.
        return replace(replacement, rendered_input=text)
