"""Explicit local owner-side collaboration lifecycle.

This module is inert at import/construction time. It only creates private
metadata repositories, or binds a loopback listener, on explicit calls.
"""

from __future__ import annotations

import secrets
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from time import monotonic as _monotonic

from collab_runtime.coordinator import Coordinator
from collab_runtime.coordination import project_change
from collab_runtime.errors import AccessDeniedError, StaleRevisionError, TaskCapacityError, TaskExistsError, ValidationError
from collab_runtime.host import CollaborationHost
from collab_runtime.models import (
    ACTIVE_STATUSES, MAX_TASKS, SessionState, build_context,
    build_initial_state, build_task, compute_overlaps, safe_id,
)
from collab_runtime.store import GitStore
from collab_runtime.transport import LoopbackServer
from runtime_paths import collab_owners_dir


_PREVIEW_TTL = 300.0
_MAX_PREVIEWS = 4
_WARNINGS = ["metadata-only; no project code or listener", "will not overwrite occupied paths"]


class OwnerError(Exception):
    """Fixed-code, sanitized error suitable for bridge serialization."""

    def __init__(self, code: str, receipt: dict[str, Any] | None = None) -> None:
        self.code = code
        self.receipt = _detached(receipt or {})
        super().__init__(code)


def _detached(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _detached(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detached(item) for item in value]
    if isinstance(value, tuple):
        return [_detached(item) for item in value]
    return value


class OwnerSessionManager:
    """One explicitly selected owner session, with credentials held in memory."""

    def __init__(self, private_root: Path | None = None, *,
                 store_factory: Callable[..., GitStore] | None = None,
                 coordinator_factory: Callable[..., Coordinator] | None = None,
                 server_factory: Callable[..., LoopbackServer] | None = None) -> None:
        self._private_root = Path(private_root) if private_root is not None else collab_owners_dir()
        self._store_factory = store_factory or GitStore
        self._coordinator_factory = coordinator_factory or Coordinator
        self._server_factory = server_factory or LoopbackServer
        self._op_lock = threading.Lock()
        self._lock = threading.RLock()
        self._state = "unconfigured"
        self._config: dict[str, Any] | None = None
        self._previews: dict[str, dict[str, Any]] = {}
        self._server: Any = None
        self._coordinator: Any = None
        self._credentials: dict[str, str] = {}
        self._exported: set[str] = set()
        self._epoch = 0
        self._endpoint: str | None = None
        self._created_paths: list[str] = []
        self._host = CollaborationHost(self._private_root / "cursors")

    @staticmethod
    def _project(root: Path) -> tuple[Path, str]:
        try:
            supplied = Path(root).resolve(strict=True)
            top = GitStore.project_toplevel(supplied)
            if top != supplied:
                raise OwnerError("invalid_project_root")
            head = GitStore.project_head(top)
            if not re_full_sha(head):
                raise OwnerError("invalid_project_head")
            return top, head
        except OwnerError:
            raise
        except Exception:
            raise OwnerError("invalid_project") from None

    def preview_create(self, project_root: Path, *, session_id: str, target_version: str,
                       goal: str, owner_id: str, member_ids: list[str],
                       tasks: list[dict[str, Any]]) -> dict[str, Any]:
        root, head = self._project(project_root)
        try:
            safe_id(session_id, "session_id")
            members = validate_members(owner_id, member_ids)
            context = build_context(goal=goal, decisions=[], interfaces={})
            build_initial_state(session_id=session_id, target_version=target_version, base_commit=head)
            if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
                raise ValidationError("invalid tasks")
            parsed: list[dict[str, Any]] = []
            ids: set[str] = set()
            for raw in tasks:
                if not isinstance(raw, dict) or set(raw) - {"id", "owner", "goal", "scopes", "status"}:
                    raise ValidationError("invalid task fields")
                if not {"id", "owner", "goal", "scopes"} <= set(raw):
                    raise ValidationError("missing task fields")
                task_id = safe_id(raw["id"], "task id")
                if task_id in ids:
                    raise ValidationError("duplicate task")
                ids.add(task_id)
                if raw["owner"] not in members:
                    raise ValidationError("invalid task owner")
                built = build_task(task_id=task_id, owner=raw["owner"], goal=raw["goal"],
                                   scopes=raw["scopes"], status=raw.get("status", "queued"),
                                   context_revision="0" * 40)
                if built.status not in ACTIVE_STATUSES:
                    raise ValidationError("invalid task status")
                parsed.append({"id": built.id, "owner": built.owner, "goal": built.goal,
                               "scopes": list(built.scopes), "status": built.status})
            # Reject a private namespace within, or above, the project before any write.
            private = self._private_root.resolve()
            if private == root or private in root.parents or root in private.parents:
                raise OwnerError("unsafe_private_root")
            with self._lock:
                now = _monotonic()
                self._prune_previews_locked(now)
                if len(self._previews) >= _MAX_PREVIEWS:
                    raise OwnerError("preview_capacity")
                preview_id = secrets.token_urlsafe(24)
                record = {"created": now, "root": str(root), "head": head,
                          "session_id": session_id, "target_version": target_version,
                          "goal": context.goal, "owner_id": owner_id, "member_ids": members,
                          "tasks": parsed, "plan_id": str(uuid.uuid4())}
                self._previews[preview_id] = record
            return {"previewId": preview_id, "projectRoot": str(root), "sessionId": session_id,
                    "targetVersion": target_version, "baseCommit": head, "goal": context.goal,
                    "ownerId": owner_id, "memberIds": list(members), "tasks": _detached(parsed),
                    "mode": "create", "warnings": list(_WARNINGS)}
        except OwnerError:
            raise
        except Exception:
            raise OwnerError("invalid_metadata") from None

    def create(self, preview_id: str, project_root: Path) -> dict[str, Any]:
        with self._op_lock:
            writes_started = False
            try:
                root, head = self._project(project_root)
                with self._lock:
                    if self._server is not None or self._state in {"starting", "stopping", "cleanup_failed"}:
                        raise OwnerError("stop_required")
                    if self._config is not None and self._state not in {"stopped", "unconfigured", "creation_failed"}:
                        raise OwnerError("stop_required")
                    self._prune_previews_locked(_monotonic())
                    record = self._previews.get(preview_id) if isinstance(preview_id, str) else None
                    if (record is None or record["root"] != str(root) or record["head"] != head
                            or _monotonic() - record["created"] >= _PREVIEW_TTL):
                        raise OwnerError("preview_stale")
                plan = Path(record["plan_id"])
                namespace = self._private_root / plan
                private = self._private_root.resolve()
                if private == root or private in root.parents or root in private.parents:
                    raise OwnerError("unsafe_private_root")
                self._validate_private_root()
                with self._lock:
                    # Recheck identity/TTL after preflight and make confirmation one-shot.
                    if self._previews.get(preview_id) is not record or _monotonic() - record["created"] >= _PREVIEW_TTL:
                        raise OwnerError("preview_stale")
                    self._previews.pop(preview_id)
                    self._created_paths = []
                writes_started = True
                self._ensure_private_root()
                namespace.mkdir(mode=0o700)
                with self._lock: self._created_paths = [str(namespace)]
                hub, store_path = namespace / "hub.git", namespace / "store.git"
                goal_revision = None
                with self._lock:
                    self._created_paths.append(str(hub))
                GitStore.create_bare(hub, what="hub")
                with self._lock: self._created_paths.append(str(store_path))
                GitStore.create_bare(store_path, what="store")
                store = self._store_factory(store=store_path, remote=str(hub))
                context = build_context(goal=record["goal"], decisions=[], interfaces={})
                state = SessionState(record["session_id"], record["target_version"], head, context, {})
                revision = store.init_session(state)
                goal_revision = revision
                for task_data in record["tasks"]:
                    task = build_task(task_id=task_data["id"], owner=task_data["owner"],
                                      goal=task_data["goal"], scopes=task_data["scopes"],
                                      status=task_data["status"], context_revision=goal_revision)
                    revision = store.upsert_task(task, expected_revision=revision)
                latest, fetched = store.fetch_state()
                with self._lock:
                    self._set_config(root, store, record, latest, fetched, store_path, hub)
                    self._state = "configured"
                return self.status()
            except OwnerError as exc:
                if writes_started:
                    with self._lock:
                        self._state = "creation_failed"
                        self._created_paths = [path for path in self._created_paths if Path(path).exists()]
                    exc.receipt = {"createdPaths": list(self._created_paths)}
                raise
            except Exception as exc:
                with self._lock:
                    self._state = "creation_failed"
                    self._created_paths = [path for path in self._created_paths if Path(path).exists()]
                raise OwnerError("creation_failed", {"createdPaths": list(self._created_paths)}) from None

    def discard_create_preview(self, preview_id: str) -> None:
        """Drop an unused in-memory create plan (for stale bridge operations)."""
        with self._lock:
            self._previews.pop(preview_id, None)

    def select_existing(self, project_root: Path, *, store_path: Path, hub_path: Path,
                        owner_id: str, member_ids: list[str]) -> dict[str, Any]:
        with self._op_lock:
            try:
                root, head = self._project(project_root)
                members = validate_members(owner_id, member_ids)
                with self._lock:
                    if self._server is not None or self._state in {"starting", "stopping", "cleanup_failed"}:
                        raise OwnerError("stop_required")
                    if self._config is not None and self._state not in {"stopped", "unconfigured", "creation_failed"}:
                        raise OwnerError("stop_required")
                reject_symlink_path(store_path)
                reject_symlink_path(hub_path)
                store_resolved, hub_resolved = Path(store_path).resolve(strict=True), Path(hub_path).resolve(strict=True)
                for path in (store_resolved, hub_resolved):
                    if path == root or root in path.parents or path in root.parents:
                        raise OwnerError("unsafe_metadata_path")
                    if path.is_symlink() or not path.is_dir():
                        raise OwnerError("invalid_metadata_path")
                if store_resolved == hub_resolved or store_resolved in hub_resolved.parents or hub_resolved in store_resolved.parents:
                    raise OwnerError("invalid_metadata_paths")
                store = self._store_factory(store=store_resolved, remote=str(hub_resolved))
                revision, state = store.fetch_state()
                if state.base_commit != head:
                    raise OwnerError("source_head_mismatch")
                if owner_id not in members or any(task.owner not in members for task in state.tasks.values()):
                    raise OwnerError("invalid_members")
                record = {"session_id": state.session_id, "target_version": state.target_version,
                          "goal": state.context.goal, "owner_id": owner_id, "member_ids": members,
                          "tasks": [dict({"id": key}, **task.to_dict()) for key, task in state.tasks.items()]}
                with self._lock:
                    self._set_config(root, store, record, revision, state, store_resolved, hub_resolved)
                    self._state = "configured"
                return self.status()
            except OwnerError:
                raise
            except Exception:
                raise OwnerError("invalid_existing_session") from None

    def start(self, project_root: Path, *, port: int = 0) -> dict[str, Any]:
        if type(port) is not int or not 0 <= port <= 65535:
            raise OwnerError("invalid_port")
        with self._op_lock:
            with self._lock:
                config = self._config
                if config is None or self._state not in {"configured", "stopped"}:
                    raise OwnerError("not_configured")
                self._state = "starting"
            server = coordinator = None
            try:
                root, head = self._project(project_root)
                if str(root) != config["projectRoot"] or head != config["baseCommit"]:
                    raise OwnerError("source_head_mismatch")
                revision, state = config["store"].fetch_state()
                checked_root, checked_head = self._project(project_root)
                if checked_root != root or checked_head != head:
                    raise OwnerError("source_head_mismatch")
                if (state.session_id, state.base_commit, state.target_version) != (
                    config["sessionId"], config["baseCommit"], config["targetVersion"]):
                    raise OwnerError("session_identity_mismatch")
                if any(task.owner not in config["memberIds"] for task in state.tasks.values()):
                    raise OwnerError("invalid_members")
                with self._lock:
                    if self._config is not config:
                        raise OwnerError("product_stale")
                    config.update(revision=revision, goal=state.context.goal,
                                  context=state.context.to_dict(),
                                  tasks=[{"id": key, "owner": task.owner, "goal": task.goal,
                                          "scopes": list(task.scopes), "status": task.status,
                                          "contextRevision": task.context_revision}
                                         for key, task in state.tasks.items()])
                credentials: dict[str, str] = {}
                used: set[str] = set()
                for member in config["memberIds"]:
                    token = secrets.token_urlsafe(32)
                    while token in used:
                        token = secrets.token_urlsafe(32)
                    credentials[member] = token
                    used.add(token)
                coordinator = self._coordinator_factory(config["store"], session_id=config["sessionId"],
                                                        owner_id=config["ownerId"], member_credentials=credentials)
                server = self._server_factory(coordinator, port=port)
                server.start()
                with self._lock:
                    self._epoch += 1
                    self._credentials = credentials
                    self._exported.clear()
                    self._coordinator, self._server = coordinator, server
                    self._endpoint = server.base_url
                    self._state = "running"
            except Exception as exc:
                cleanup_failed = False
                if server is not None:
                    try: server.close()
                    except Exception: cleanup_failed = True
                elif coordinator is not None:
                    close = getattr(coordinator, "close", None)
                    if close:
                        try: close()
                        except Exception: cleanup_failed = True
                with self._lock:
                    if cleanup_failed:
                        self._credentials = credentials if 'credentials' in locals() else {}
                        self._coordinator, self._server = coordinator, server
                        self._state = "cleanup_failed"
                    else:
                        self._state = "configured"
                if isinstance(exc, OwnerError):
                    raise exc
                raise OwnerError("start_failed") from None
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._op_lock:
            with self._lock:
                server = self._server
                if server is None:
                    if self._state == "configured": self._state = "stopped"
                    return self._status_locked()
                self._state = "stopping"
            try:
                server.close()
            except Exception:
                with self._lock: self._state = "cleanup_failed"
                raise OwnerError("cleanup_failed", {"retryRequired": True}) from None
            with self._lock:
                self._server = self._coordinator = None
                self._credentials.clear()
                self._exported.clear()
                self._endpoint = None
                self._state = "stopped"
                return self._status_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def _product_read(self, project_root: Path, expected_session_id: str | None = None) -> tuple[dict[str, Any], SessionState, str, int, str]:
        """Fetch the authoritative state without holding the short status lock."""
        root, head = self._project(project_root)
        with self._lock:
            config, epoch, lifecycle = self._config, self._epoch, self._state
            if config is None:
                raise OwnerError("not_configured")
            if str(root) != config["projectRoot"]:
                raise OwnerError("wrong_project")
            if expected_session_id is not None and expected_session_id != config["sessionId"]:
                raise OwnerError("session_identity_mismatch")
            if head != config["baseCommit"]:
                raise OwnerError("source_head_mismatch")
            if lifecycle not in {"configured", "stopped", "running"}:
                raise OwnerError("not_running")
            coordinator = self._coordinator if lifecycle == "running" else None
            credential = self._credentials.get(config["ownerId"]) if coordinator is not None else None
        try:
            if coordinator is not None:
                if not credential:
                    raise OwnerError("not_running")
                snap = coordinator.snapshot(credential)
                revision, state = snap.revision, snap.state
            else:
                revision, state = config["store"].fetch_state()
            checked_root, checked_head = self._project(project_root)
            if checked_root != root or checked_head != head:
                raise OwnerError("source_head_mismatch")
            if (state.session_id, state.base_commit, state.target_version) != (
                    config["sessionId"], config["baseCommit"], config["targetVersion"]):
                raise OwnerError("session_identity_mismatch")
            if state.base_commit != head or any(task.owner not in config["memberIds"] for task in state.tasks.values()):
                raise OwnerError("invalid_members")
        except OwnerError:
            raise
        except Exception:
            raise OwnerError("product_read_failed") from None
        with self._lock:
            if self._config is not config or self._epoch != epoch or self._state != lifecycle:
                raise OwnerError("product_stale")
            # Mutate in place: localPreview uses config object identity as a guard.
            config.update(revision=revision, goal=state.context.goal, context=state.context.to_dict(),
                          tasks=[{"id": key, "owner": task.owner, "goal": task.goal,
                                  "scopes": list(task.scopes), "status": task.status,
                                  "contextRevision": task.context_revision}
                                 for key, task in state.tasks.items()])
        return config, state, revision, epoch, lifecycle

    @staticmethod
    def _product_dto(config: dict[str, Any], state: SessionState, revision: str,
                     epoch: int, lifecycle: str) -> dict[str, Any]:
        waiting = [task.id for task in state.tasks.values() if task.status == "waiting"]
        return {"projectRoot": config["projectRoot"], "sessionId": state.session_id,
                "baseCommit": state.base_commit, "targetVersion": state.target_version,
                "revision": revision, "contextHash": state.context_hash,
                "context": state.context.to_dict(), "ownerId": config["ownerId"],
                "memberIds": list(config["memberIds"]),
                "tasks": [{"id": key, "owner": task.owner, "goal": task.goal,
                           "scopes": list(task.scopes), "status": task.status,
                           "contextRevision": task.context_revision}
                          for key, task in state.tasks.items()],
                "overlaps": compute_overlaps(state.tasks), "waitingTaskIds": waiting,
                "epoch": epoch, "state": lifecycle}

    def product_snapshot(self, project_root: Path, *, expected_session_id: str | None = None) -> dict[str, Any]:
        with self._op_lock:
            config, state, revision, epoch, lifecycle = self._product_read(project_root, expected_session_id)
            return self._product_dto(config, state, revision, epoch, lifecycle)

    def product_changes(self, project_root: Path, *, expected_session_id: str | None,
                        after_revision: str, limit: int = 16) -> dict[str, Any]:
        if not isinstance(expected_session_id, str) or not expected_session_id:
            raise OwnerError("session_identity_mismatch")
        try:
            safe_id(expected_session_id, "session_id")
            from collab_runtime.models import sha_hex
            sha_hex(after_revision, "after_revision")
            if type(limit) is not int or not 1 <= limit <= 16:
                raise ValueError
        except Exception:
            raise OwnerError("product_invalid") from None
        with self._op_lock:
            config, state, revision, epoch, lifecycle = self._product_read(project_root, expected_session_id)
            try:
                replay = config["store"].replay_states(after_revision, limit=limit)
            except Exception as exc:
                if exc.__class__.__name__ == "ReplayUnavailableError":
                    raise OwnerError("product_history_unavailable") from None
                if exc.__class__.__name__ == "ValidationError":
                    raise OwnerError("product_invalid") from None
                raise OwnerError("product_read_failed") from None
            identity = (state.session_id, state.base_commit, state.target_version)
            chain = [(replay.after_revision, replay.previous_state), *replay.states]
            for _, item in chain:
                if (item.session_id, item.base_commit, item.target_version) != identity or any(
                        task.owner not in config["memberIds"] for task in item.tasks.values()):
                    raise OwnerError("product_invalid")
            post_config, post_state, post_revision, post_epoch, post_lifecycle = self._product_read(
                project_root, expected_session_id)
            if (post_config is not config or post_revision != replay.head_revision or
                    revision != replay.head_revision or post_epoch != epoch or post_lifecycle != lifecycle or
                    (post_state.session_id, post_state.base_commit, post_state.target_version) != identity):
                raise OwnerError("product_stale")
            with self._lock:
                if self._config is not config or self._epoch != epoch or self._state != lifecycle:
                    raise OwnerError("product_stale")
            events = [project_change(chain[index][1], chain[index + 1][1], chain[index][0], chain[index + 1][0])
                      for index in range(len(chain) - 1)]
            return {"sessionId": state.session_id, "baseCommit": state.base_commit, "epoch": epoch,
                    "headRevision": replay.head_revision,
                    "lastRevision": replay.states[-1][0] if replay.states else replay.after_revision,
                    "hasMore": replay.has_more, "events": events}

    def _product_write(self, project_root: Path, *, expected_revision: str,
                       expected_epoch: int, expected_session_id: str,
                       action: str, context: dict[str, Any] | None = None,
                       task_id: str | None = None, status: str | None = None,
                       member_id: str | None = None) -> dict[str, Any]:
        with self._op_lock:
            root, head = self._project(project_root)
            with self._lock:
                config, epoch, lifecycle = self._config, self._epoch, self._state
                if config is None: raise OwnerError("not_configured")
                if str(root) != config["projectRoot"]: raise OwnerError("wrong_project")
                if head != config["baseCommit"]: raise OwnerError("source_head_mismatch")
                if expected_session_id != config["sessionId"]: raise OwnerError("session_identity_mismatch")
                if type(expected_epoch) is not int or epoch != expected_epoch: raise OwnerError("epoch_mismatch")
                if lifecycle != "running" or self._coordinator is None: raise OwnerError("not_running")
                coordinator = self._coordinator
                identity = (config["sessionId"], config["baseCommit"], config["targetVersion"])
                if member_id is not None:
                    if member_id not in config["memberIds"]: raise OwnerError("invalid_members")
                    credential = self._credentials.get(member_id)
                else:
                    credential = self._credentials.get(config["ownerId"])
            if not credential: raise OwnerError("not_running")
            # Read and validate the assignment against the same expected CAS
            # view before entering Coordinator. This is validation, never rebasing.
            try:
                checked_config, checked_state, current_revision, checked_epoch, checked_lifecycle = self._product_read(
                    project_root, expected_session_id)
                if checked_config is not config or checked_epoch != epoch or checked_lifecycle != "running":
                    raise OwnerError("product_stale")
                if current_revision != expected_revision:
                    raise OwnerError("product_stale_revision")
                if action == "task" and task_id not in checked_state.tasks:
                    raise OwnerError("product_invalid")
            except OwnerError:
                raise
            try:
                if action == "context":
                    revision = coordinator.update_context(credential, context, expected_revision=expected_revision)
                else:
                    revision = coordinator.update_task_status(credential, task_id=task_id, status=status,
                                                              expected_revision=expected_revision)
            except StaleRevisionError:
                raise OwnerError("product_stale_revision") from None
            except AccessDeniedError:
                raise OwnerError("product_access_denied") from None
            except ValidationError:
                raise OwnerError("product_invalid") from None
            except Exception:
                raise OwnerError("product_write_rejected") from None
            return {"revision": revision, "sessionId": identity[0], "epoch": epoch,
                    "action": action, **({"taskId": task_id, "status": status} if action == "task" else {})}

    def update_product_context(self, project_root: Path, *, context: dict[str, Any],
                               expected_revision: str, expected_epoch: int,
                               expected_session_id: str) -> dict[str, Any]:
        return self._product_write(project_root, expected_revision=expected_revision,
                                   expected_epoch=expected_epoch, expected_session_id=expected_session_id,
                                   action="context", context=context)

    def update_product_task(self, project_root: Path, *, task_id: str, status: str,
                            expected_revision: str, expected_epoch: int,
                            expected_session_id: str, member_id: str | None = None) -> dict[str, Any]:
        return self._product_write(project_root, expected_revision=expected_revision,
                                   expected_epoch=expected_epoch, expected_session_id=expected_session_id,
                                   action="task", task_id=task_id, status=status, member_id=member_id)

    def create_product_task(self, project_root: Path, *, task_id: str, owner: str, goal: str,
                            scopes: list[str], expected_revision: str, expected_epoch: int,
                            expected_session_id: str) -> dict[str, Any]:
        """Create a single queued assignment in the currently running session."""
        with self._op_lock:
            root, head = self._project(project_root)
            with self._lock:
                config, epoch, lifecycle = self._config, self._epoch, self._state
                if config is None: raise OwnerError("not_configured")
                if str(root) != config["projectRoot"]: raise OwnerError("wrong_project")
                if head != config["baseCommit"]: raise OwnerError("source_head_mismatch")
                if expected_session_id != config["sessionId"]: raise OwnerError("session_identity_mismatch")
                if type(expected_epoch) is not int or epoch != expected_epoch: raise OwnerError("epoch_mismatch")
                if lifecycle != "running" or self._coordinator is None: raise OwnerError("not_running")
                if owner not in config["memberIds"]: raise OwnerError("invalid_members")
                coordinator = self._coordinator
                credential = self._credentials.get(config["ownerId"])
                identity = (config["sessionId"], epoch)
            if not credential: raise OwnerError("not_running")
            try:
                checked_config, checked_state, current_revision, checked_epoch, checked_lifecycle = self._product_read(
                    project_root, expected_session_id)
                if checked_config is not config or checked_epoch != epoch or checked_lifecycle != "running":
                    raise OwnerError("product_stale")
                if current_revision != expected_revision:
                    raise OwnerError("product_stale_revision")
                revision = coordinator.create_task(credential, task_id=task_id, owner=owner, goal=goal,
                                                   scopes=scopes, expected_revision=expected_revision)
            except OwnerError:
                raise
            except StaleRevisionError:
                raise OwnerError("product_stale_revision") from None
            except AccessDeniedError:
                raise OwnerError("product_access_denied") from None
            except TaskExistsError:
                raise OwnerError("task_exists") from None
            except TaskCapacityError:
                raise OwnerError("task_capacity") from None
            except ValidationError as exc:
                raise OwnerError("product_invalid") from None
            except Exception:
                raise OwnerError("product_write_rejected") from None
            return {"revision": revision, "sessionId": identity[0], "epoch": identity[1],
                    "action": "createTask", "taskId": task_id, "status": "queued",
                    "owner": owner, "contextRevision": expected_revision}

    def product_proposals(self, project_root: Path, *, expected_session_id: str) -> dict[str, Any]:
        if not isinstance(expected_session_id, str) or not expected_session_id:
            raise OwnerError("session_identity_mismatch")
        with self._op_lock:
            config, state, revision, epoch, lifecycle = self._product_read(project_root, expected_session_id)
            try:
                from collab_runtime.proposals import list_proposals
                receipts = list_proposals(config["store"])
            except Exception:
                raise OwnerError("proposals_read_failed") from None
            post_config, post_state, post_revision, post_epoch, post_lifecycle = self._product_read(
                project_root, expected_session_id)
            if (post_config is not config or post_revision != revision or
                    post_state.session_id != state.session_id or post_state.base_commit != state.base_commit or
                    post_state.context_hash != state.context_hash or post_epoch != epoch or
                    post_lifecycle != lifecycle):
                raise OwnerError("product_stale")
            with self._lock:
                if self._config is not config or self._epoch != epoch or self._state != lifecycle:
                    raise OwnerError("product_stale")
            return {"sessionId": state.session_id, "baseCommit": state.base_commit, "revision": revision,
                    "contextHash": state.context_hash, "epoch": epoch,
                    "proposals": [{"proposalId": item.proposal_id, "taskId": item.task_id,
                                   "owner": item.owner, "contextRevision": item.context_revision,
                                   "proposalRevision": item.commit, "baseCommit": item.base_commit,
                                   "sessionId": item.session_id, "contextHash": item.context_hash, "fileCount": item.file_count,
                                   "staleContext": item.context_hash != state.context_hash}
                                  for item in receipts]}

    def _status_locked(self) -> dict[str, Any]:
        config = self._config or {}
        tasks = config.get("tasks", [])
        return {"state": self._state, "projectRoot": config.get("projectRoot"),
                "sessionId": config.get("sessionId"), "targetVersion": config.get("targetVersion"),
                "baseCommit": config.get("baseCommit"), "revision": config.get("revision"),
                "goal": config.get("goal"), "ownerId": config.get("ownerId"),
                "memberIds": list(config.get("memberIds", [])), "tasks": _detached(tasks),
                "storePath": config.get("storePath"), "hubPath": config.get("hubPath"),
                "endpoint": self._endpoint, "epoch": self._epoch,
                "exportedMembers": sorted(self._exported),
                "retryRequired": self._state in {"cleanup_failed", "creation_failed"},
                "createdPaths": list(self._created_paths)}

    def reveal_member_once(self, member_id: str) -> dict[str, Any]:
        with self._lock:
            if self._state != "running" or member_id not in self._credentials:
                raise OwnerError("not_running_or_member")
            if member_id in self._exported:
                raise OwnerError("already_shared")
            config = self._config
            credential = self._credentials[member_id]
            epoch = self._epoch
            self._exported.add(member_id)
            task_ids = [task["id"] for task in config["tasks"] if task["owner"] == member_id]
            return {"endpoint": self._endpoint, "sessionId": config["sessionId"],
                    "baseCommit": config["baseCommit"], "targetVersion": config["targetVersion"],
                    "memberId": member_id, "credential": credential,
                    "storePath": config["storePath"], "hubPath": config["hubPath"],
                    "taskIds": task_ids, "epoch": epoch, "scope": "loopback-only"}

    def preview_local_collaboration(self, collaboration_host: CollaborationHost, project_root: Path,
                                    *, member_id: str, task_id: str) -> dict[str, Any]:
        # Refresh the cached assignment/status before authorizing this local
        # handoff; no network work is performed while holding the memory lock.
        try:
            self.product_snapshot(project_root)
        except OwnerError:
            raise
        with self._lock:
            if self._state != "running" or member_id not in self._credentials:
                raise OwnerError("not_running_or_member")
            try:
                root = Path(project_root).resolve(strict=True)
            except Exception:
                raise OwnerError("invalid_project") from None
            if str(root) != self._config["projectRoot"]:
                raise OwnerError("wrong_project")
            task = next((item for item in self._config["tasks"] if item["id"] == task_id), None)
            if task is None or task["owner"] != member_id or task["status"] not in ACTIVE_STATUSES:
                raise OwnerError("invalid_task_assignment")
            credential, endpoint, epoch = self._credentials[member_id], self._endpoint, self._epoch
            config = self._config
        try:
            preview = collaboration_host.preview(Path(project_root), endpoint, credential, member_id, task_id)
        except Exception:
            raise OwnerError("local_preview_failed") from None
        with self._lock:
            stale = self._state != "running" or self._epoch != epoch or self._config is not config
        if stale:
            collaboration_host.drop_preview(preview.get("previewId"))
            raise OwnerError("stale_local_preview")
        return {"preview": preview, "storePath": config["storePath"], "hubPath": config["hubPath"],
                "endpoint": endpoint, "memberId": member_id, "taskId": task_id, "epoch": epoch}

    def _validate_private_root(self) -> None:
        path = self._private_root
        absolute = path.absolute()
        if any(part.is_symlink() for part in (absolute, *absolute.parents)):
            raise OwnerError("unsafe_private_root")
        existing = absolute
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            metadata = existing.stat()
        except OSError:
            raise OwnerError("unsafe_private_root") from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise OwnerError("unsafe_private_root")
        if hasattr(os, "getuid") and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
            raise OwnerError("unsafe_private_parent")
        if absolute.exists():
            info = absolute.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise OwnerError("unsafe_private_root")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise OwnerError("unsafe_private_owner")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o700:
                raise OwnerError("unsafe_private_permissions")

    def _ensure_private_root(self) -> None:
        path = self._private_root.absolute()
        missing: list[Path] = []
        current = path
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        if os.name != "nt":
            info = path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OwnerError("unsafe_private_root")

    def _prune_previews_locked(self, now: float) -> None:
        self._previews = {key: item for key, item in self._previews.items()
                          if now - item["created"] < _PREVIEW_TTL}

    def _set_config(self, root: Path, store: GitStore, record: dict[str, Any], revision: str,
                    state: SessionState, store_path: Path, hub_path: Path) -> None:
        self._config = {"projectRoot": str(root), "store": store, "sessionId": state.session_id,
                        "targetVersion": state.target_version, "baseCommit": state.base_commit,
                        "revision": revision, "goal": state.context.goal, "context": state.context.to_dict(), "ownerId": record["owner_id"],
                        "memberIds": list(record["member_ids"]),
                        "tasks": [{"id": key, "owner": task.owner, "goal": task.goal,
                                   "scopes": list(task.scopes), "status": task.status,
                                   "contextRevision": task.context_revision}
                                  for key, task in state.tasks.items()],
                        "storePath": str(store_path), "hubPath": str(hub_path)}


def validate_members(owner_id: str, member_ids: list[str]) -> list[str]:
    owner = safe_id(owner_id, "owner id")
    if not isinstance(member_ids, list) or not 1 <= len(member_ids) <= 256:
        raise ValidationError("member_ids must contain 1-256 members")
    members = [safe_id(item, "member id") for item in member_ids]
    if len(set(members)) != len(members) or owner not in members:
        raise ValidationError("members must be unique and include the owner")
    return members


def re_full_sha(value: str) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def reject_symlink_path(value: Path) -> None:
    path = Path(value).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise OwnerError("symlink_metadata_path")
