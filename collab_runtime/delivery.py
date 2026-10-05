"""Explicit local proposal publication and candidate delivery for native runs.

This adapter is configuration-only: callers must provide already-existing
local bare Git store/hub paths. It has no network, model, or background-job
behavior. Preview tickets retain only a bounded, immutable selected proposal.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collab_runtime.candidates import CandidateConflictError, assemble_candidate
from collab_runtime.context import SharedSnapshot, parse_snapshot_dict
from collab_runtime.errors import CollabError, ValidationError
from collab_runtime.models import safe_id
from collab_runtime.proposals import (
    MAX_FILES, Proposal, capture_proposal, list_proposals, publish_proposal,
    proposal_bytes, proposal_file_path, _source_head,
)
from collab_runtime.store import GitStore
from workspace.worktree import GitWorktreeWorkspace

_MAX_TICKET_ARTIFACT = 2 * 1024 * 1024
_WARNINGS = [
    "selected code is shared verbatim; embedded secrets are not redacted",
    "capture is cumulative from the session base for selected paths; prior WIP may be included",
    "task scopes are advisory; out-of-scope paths require explicit confirmation",
    "no source commit history is published; the normal project remote is never used",
    "local bare store/hub paths are trusted configuration; native HTTP credentials do not grant code access",
]


class DeliveryError(CollabError):
    """Safe fixed-message delivery failure (never includes paths or artifacts)."""

    def __init__(self, code: str = "invalid") -> None:
        messages = {
            "invalid": "The delivery request is invalid.",
            "stale": "The delivery context changed; preview or synchronize again.",
            "busy": "The native run must be idle before delivery.",
            "unavailable": "The configured local collaboration store is unavailable.",
            "expired": "The publication preview expired or is no longer available.",
            "conflict": "The selected proposals conflict; no candidate was created or verified.",
        }
        self.code = code if code in messages else "invalid"
        super().__init__(messages[self.code])


class DeliveryConflictError(DeliveryError):
    """Safe structured conflict carrier with path names only."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__("conflict")
        self.conflicts = tuple(sorted(set(paths)))


@dataclass(frozen=True, repr=False)
class _Ticket:
    proposal: Proposal
    store_path: Path
    hub_path: Path
    project_root: Path
    run_id: str
    expected_revision: str
    binding_revision: str
    binding_hash: str
    created: float
    out_of_scope_paths: tuple[str, ...]

    def __repr__(self) -> str:
        return "<publication ticket>"


class SharedDeliveryService:
    """Thread-safe bounded in-memory publication previews and explicit delivery."""

    def __init__(self, *, ticket_limit: int = 4, ttl_seconds: float = 300,
                 clock: Any = time.monotonic):
        if type(ticket_limit) is not int or not 1 <= ticket_limit <= 32:
            raise ValueError("ticket_limit must be between 1 and 32")
        if (type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 600
                or not math.isfinite(ttl_seconds)):
            raise ValueError("ttl_seconds must be finite, positive, and at most 600")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._limit, self._ttl, self._clock = ticket_limit, float(ttl_seconds), clock
        self._lock = threading.RLock()
        self._tickets: dict[str, _Ticket] = {}

    def _prune(self) -> None:
        now = self._clock()
        for key in tuple(self._tickets):
            if now - self._tickets[key].created >= self._ttl:
                del self._tickets[key]

    @staticmethod
    def _snapshot(session: Any) -> SharedSnapshot:
        binding = getattr(session, "accepted_binding", None)
        if not isinstance(binding, SharedSnapshot):
            raise DeliveryError("stale")
        try:
            return parse_snapshot_dict(binding.to_dict())
        except Exception:
            raise DeliveryError("stale") from None

    @staticmethod
    def _store(store_path: Path | str, hub_path: Path | str, root: Path, workroot: Path) -> GitStore:
        try:
            store = Path(store_path).resolve(strict=True)
            hub = Path(hub_path).resolve(strict=True)
            if store == hub or any(
                p == boundary or boundary in p.parents
                for p in (store, hub) for boundary in (root, workroot)
            ) or store in hub.parents or hub in store.parents:
                raise ValidationError("unsafe placement")
            return GitStore(store=store, remote=str(hub))
        except Exception:
            raise DeliveryError("unavailable") from None

    @staticmethod
    def _check_session(session: Any, workspace: GitWorktreeWorkspace,
                       binding: SharedSnapshot) -> tuple[Path, Path, str]:
        if not isinstance(workspace, GitWorktreeWorkspace):
            raise DeliveryError("invalid")
        if getattr(session, "active", True) is not False:
            raise DeliveryError("busy")
        try:
            root = Path(session.project_root).resolve(strict=True)
            snap = workspace.snapshot
            workroot = Path(workspace.root).resolve(strict=True)
            run_id = safe_id(session.run_id, "run_id")
            status = session.status()
        except Exception:
            raise DeliveryError("invalid") from None
        if (Path(snap.source_root).resolve() != root or
                snap.source_head != binding.state.base_commit or
                status.get("sessionId") != binding.state.session_id or
                status.get("taskId") != binding.task_id or
                status.get("memberId") != binding.selected_task.owner or
                binding.task_id not in binding.state.tasks):
            raise DeliveryError("stale")
        return root, workroot, run_id

    def preview_publication(self, session: Any, workspace: GitWorktreeWorkspace, *,
                            store_path: Path | str, hub_path: Path | str,
                            paths: list[str] | tuple[str, ...], proposal_id: str | None = None) -> dict[str, Any]:
        binding = self._snapshot(session)
        root, workroot, run_id = self._check_session(session, workspace, binding)
        store = self._store(store_path, hub_path, root, workroot)
        if not isinstance(paths, (list, tuple)) or not paths or len(paths) > MAX_FILES:
            raise DeliveryError("invalid")
        try:
            cleaned = list(dict.fromkeys(proposal_file_path(p) for p in paths))
            identifier = safe_id(proposal_id, "proposal_id") if proposal_id is not None else "native-" + uuid.uuid4().hex
            head, state = store.fetch_state()
            if (binding.state.session_id != state.session_id or
                    binding.state.base_commit != state.base_commit or
                    binding.state.target_version != state.target_version or
                    binding.task_id not in state.tasks or
                    (binding.selected_task.owner, binding.selected_task.goal, binding.selected_task.scopes) !=
                    (state.tasks[binding.task_id].owner, state.tasks[binding.task_id].goal,
                     state.tasks[binding.task_id].scopes)):
                raise DeliveryError("stale")
            proposal = capture_proposal(
                store, workroot, identifier, binding.task_id, cleaned, head, binding=binding)
            payload = proposal_bytes(proposal)
            if len(payload) > _MAX_TICKET_ARTIFACT:
                raise DeliveryError("invalid")
        except DeliveryError:
            raise
        except ValidationError:
            raise DeliveryError("invalid") from None
        except CollabError:
            raise DeliveryError("stale") from None
        except Exception:
            raise DeliveryError("invalid") from None
        with self._lock:
            self._prune()
            if len(self._tickets) >= self._limit:
                raise DeliveryError("busy")
            ticket_id = secrets.token_urlsafe(24)
            self._tickets[ticket_id] = _Ticket(
                proposal, store.store_path, Path(store.remote), root, run_id, head,
                binding.revision, binding.context_hash, self._clock(), proposal.out_of_scope_paths,
            )
        return {
            "previewId": ticket_id, "proposalId": identifier, "runId": run_id,
            "sessionId": proposal.session_id, "taskId": proposal.task_id, "owner": proposal.owner,
            "baseCommit": proposal.base_commit, "contextRevision": proposal.context_revision,
            "contextHash": proposal.context_hash, "expectedRevision": head,
            "paths": [f.path for f in proposal.files], "outOfScopePaths": list(proposal.out_of_scope_paths),
            "fileCount": len(proposal.files), "artifactBytes": len(payload),
            "contentDigest": hashlib.sha256(payload).hexdigest(), "warnings": list(_WARNINGS),
        }

    def confirm_publication(self, preview_id: str, *, run_id: str, project_root: Path,
                            allow_out_of_scope: bool = False) -> dict[str, Any]:
        if type(allow_out_of_scope) is not bool:
            raise DeliveryError("invalid")
        with self._lock:
            self._prune()
            ticket = self._tickets.get(preview_id) if isinstance(preview_id, str) else None
            if ticket is None:
                raise DeliveryError("expired")
            if ticket.out_of_scope_paths and not allow_out_of_scope:
                raise DeliveryError("invalid")
            try:
                same_root = Path(project_root).resolve() == ticket.project_root
            except Exception:
                same_root = False
            if run_id != ticket.run_id or not same_root:
                del self._tickets[preview_id]
                raise DeliveryError("stale")
            del self._tickets[preview_id]  # one terminal attempt; never silently retry
        try:
            store = GitStore(store=ticket.store_path, remote=str(ticket.hub_path))
            if ticket.proposal.context_revision != ticket.binding_revision or ticket.proposal.context_hash != ticket.binding_hash:
                raise DeliveryError("stale")
            if _source_head(ticket.project_root) != ticket.proposal.base_commit:
                raise DeliveryError("stale")
            session_revision, proposal_revision = publish_proposal(
                store, ticket.proposal, expected_revision=ticket.expected_revision)
        except DeliveryError:
            raise
        except CollabError:
            raise DeliveryError("stale") from None
        except Exception:
            raise DeliveryError("unavailable") from None
        return {
            "proposalId": ticket.proposal.proposal_id, "sessionRevision": session_revision,
            "proposalRevision": proposal_revision, "contextRevision": ticket.binding_revision,
            "contextHash": ticket.binding_hash, "paths": [f.path for f in ticket.proposal.files],
            "outOfScopePaths": list(ticket.out_of_scope_paths),
            "outOfScopeAuthorized": bool(ticket.out_of_scope_paths and allow_out_of_scope),
        }

    def discard_publication(self, preview_id: str) -> None:
        if not isinstance(preview_id, str):
            return
        with self._lock:
            self._tickets.pop(preview_id, None)

    def list_shared_proposals(self, *, store_path: Path | str, hub_path: Path | str,
                              project_root: Path | str, binding: SharedSnapshot) -> list[dict[str, Any]]:
        if not isinstance(binding, SharedSnapshot):
            raise DeliveryError("invalid")
        try:
            root = Path(project_root).resolve(strict=True)
            binding = parse_snapshot_dict(binding.to_dict())
        except Exception:
            raise DeliveryError("invalid") from None
        store = self._store(store_path, hub_path, root, root)
        try:
            head, state = store.fetch_state()
            if (binding.state.session_id != state.session_id or binding.state.base_commit != state.base_commit or
                    binding.state.target_version != state.target_version or not store.is_ancestor(binding.revision, head) or
                    binding.state.to_dict() != store._read_state(binding.revision).to_dict()):
                raise DeliveryError("stale")
            receipts = list_proposals(store)
        except DeliveryError:
            raise
        except CollabError:
            raise DeliveryError("stale") from None
        except Exception:
            raise DeliveryError("invalid") from None
        return [{
            "proposalId": r.proposal_id, "proposalRevision": r.commit, "taskId": r.task_id,
            "owner": r.owner, "sessionId": r.session_id, "baseCommit": r.base_commit,
            "contextRevision": r.context_revision, "contextHash": r.context_hash,
            "fileCount": r.file_count, "currentContextHashMatches": r.context_hash == state.context_hash,
        } for r in receipts]

    def assemble_shared_candidate(self, *, project_root: Path | str, store_path: Path | str,
                                  hub_path: Path | str, binding: SharedSnapshot,
                                  proposal_ids: list[str] | tuple[str, ...],
                                  output_path: Path | str, verify: bool = False) -> dict[str, Any]:
        if type(verify) is not bool or not isinstance(binding, SharedSnapshot):
            raise DeliveryError("invalid")
        try:
            root = Path(project_root).resolve(strict=True)
            binding = parse_snapshot_dict(binding.to_dict())
        except Exception:
            raise DeliveryError("invalid") from None
        store = self._store(store_path, hub_path, root, root)
        try:
            head, state = store.fetch_state()
            if (binding.state.session_id != state.session_id or binding.state.base_commit != state.base_commit or
                    binding.state.target_version != state.target_version or not store.is_ancestor(binding.revision, head) or
                    binding.state.to_dict() != store._read_state(binding.revision).to_dict()):
                raise DeliveryError("stale")
            return assemble_candidate(store, root, proposal_ids, output_path,
                                     expected_revision=head, verify=verify, binding=binding)
        except CandidateConflictError as exc:
            raise DeliveryConflictError(exc.conflict_paths) from None
        except DeliveryError:
            raise
        except CollabError:
            raise DeliveryError("stale") from None
        except Exception:
            raise DeliveryError("unavailable") from None
