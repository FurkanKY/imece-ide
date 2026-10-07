"""Native-task CombinedCandidate: explicit selection, verification, apply, rollback.

Reuses the hardened candidate merge/fingerprint implementation, not the optional
collaboration transport. Canonical receipts live in the existing RunStore.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
import uuid

from checkpoints import CheckpointStore
from collab_runtime import candidates as assembly
from change_runtime.git import GitWorktreeChangeProvider, _git_text
from project import Project
from workspace.base import resolve_within_workspace
from workspace.ownership import WorkspaceLease
from workspace.worktree import GitWorktreeWorkspace


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class _Entry:
    path: str
    before_oid: str | None
    before_mode: str | None
    after_mode: str | None
    after_bytes: bytes | None

    @property
    def after_base64(self):
        return None if self.after_bytes is None else base64.b64encode(self.after_bytes).decode("ascii")


def _hash(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _read(path, limit):
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise CandidateError("Not a bounded regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise CandidateError("File identity changed")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise CandidateError("File exceeds integration limit")
    return raw


def _file(root, rel):
    path = resolve_within_workspace(root, rel, reject_symlinks=True)
    if not path.exists():
        return None
    return _read(path, assembly.MAX_BASELINE_FILE)


def _mode(root, rel):
    path = resolve_within_workspace(root, rel, reject_symlinks=True)
    return ("100755" if path.stat().st_mode & 0o111 else "100644") if path.exists() else None


class CombinedCandidates:
    def __init__(self, runtime, output_base, owner_context_supplier=None):
        self.runtime = runtime
        self.output_base = Path(output_base)
        self.owner_context_supplier = owner_context_supplier

    def prepare(self, project_root, slots, *, verify):
        if type(verify) is not bool or not 1 <= len(slots) <= 2 or len({s.run_id for s in slots}) != len(slots):
            raise CandidateError("Select one or two distinct native results and explicitly choose verification")
        root = Path(project_root).resolve()
        proposals = []
        selected = []
        repo = None
        commit = None
        for slot in sorted(slots, key=lambda s: s.run_id):
            run = slot.coordinator.get_run()
            task = self.runtime.store.get_task(run.task_id)
            if task.project_root != str(root) or run.routing.get("agent_provider") != slot.provider_id:
                raise CandidateError("Selected execution provenance does not match this project/provider")
            workspace = slot.workspace
            if (slot.project_root != str(root) or slot.phase != "waiting_user"
                    or run.status.value != "waiting_user" or not slot.proposals
                    or not isinstance(workspace, GitWorktreeWorkspace)
                    or slot.worker is not None and slot.worker.isRunning()):
                raise CandidateError("Selected result is not an owned, quiescent native proposal")
            snapshot = workspace.snapshot
            if snapshot.project_relative_root != Path(".") or snapshot.snapshot_commit != snapshot.source_head:
                raise CandidateError("Combined candidates currently require clean, top-level committed baselines")
            if repo is None:
                repo, commit = snapshot.repository_root, snapshot.source_head
                listing = assembly._read_baseline_listing(repo, commit)
                listing_by_path = {entry.path: entry for entry in listing}
                baseline = assembly._read_baseline_blobs(repo, listing)
            if repo != snapshot.repository_root or commit != snapshot.source_head:
                raise CandidateError("Selected results do not share a committed baseline")
            change_set = GitWorktreeChangeProvider().capture(workspace)
            if not slot.evidence or slot.evidence.get("diff_sha256") != change_set.diff_sha256:
                raise CandidateError("Selected result changed after its receipt")
            entries = []
            for rel in change_set.changed_paths:
                original = listing_by_path.get(rel)
                raw = _file(workspace.root, rel)
                mode = _mode(workspace.root, rel)
                if raw is not None and not assembly._is_text(raw):
                    raise CandidateError("Binary result changes are not supported")
                entries.append(_Entry(rel, original.oid if original else None,
                    original.mode if original else None, mode, raw))
            proposals.append(SimpleNamespace(proposal_id=slot.run_id, files=entries))
            selected.append({"runId": slot.run_id, "taskId": run.task_id,
                             "lastEventSeq": run.last_event_seq, "diffSha256": change_set.diff_sha256})
        assembly._verify_before_identities(listing_by_path, proposals)
        files = assembly._merge_changes(SimpleNamespace(store_path=repo), baseline, proposals)
        assembly._validate_final_set(files)
        final = {path: ("100755" if mode == 0o755 else "100644", raw) for path, raw, mode in files}
        changed = sorted(path for path in set(baseline) | set(final) if baseline.get(path) != final.get(path))
        if not changed:
            raise CandidateError("Selected results have no combined changes")
        candidate_id = "candidate_" + uuid.uuid4().hex
        if any(path.is_symlink() for path in (self.output_base, *self.output_base.parents)):
            raise CandidateError("Unsafe candidate output directory")
        self.output_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = assembly._validate_output_path(self.output_base / candidate_id, project=repo,
                     hub=repo, store_path=Path(self.runtime.store.db_path).parent / "unused-hub")
        for item in selected:
            if self.runtime.get_run(item["runId"]).last_event_seq != item["lastEventSeq"]:
                raise CandidateError("Selected result changed during assembly")
        assembly._materialize(output, files)
        paths = tuple(path for path, _, _ in files)
        verification = assembly._run_verification(output, paths) if verify else {"status": "not_run"}
        fingerprint, complete = assembly._fingerprint_records(output, paths)
        if not complete or verification.get("status") == "pass" and not verification.get("fingerprint_complete"):
            verification = {**verification, "status": "invalidated"}
        receipt = {
            "candidateId": candidate_id, "projectRoot": str(root), "candidateDir": str(output),
            "baseCommit": commit, "selected": selected, "originalPaths": list(paths),
            "changedPaths": changed, "fingerprint": fingerprint, "fingerprintComplete": complete,
            "verification": verification, "state": "prepared", "checkpointId": None,
            "beforeHashes": {rel: _hash(baseline.get(rel, (None, None))[1]) for rel in changed},
            "afterHashes": {rel: _hash(final.get(rel, (None, None))[1]) for rel in changed},
            "beforeModes": {rel: baseline.get(rel, (None, None))[0] for rel in changed},
            "afterModes": {rel: final.get(rel, (None, None))[0] for rel in changed},
        }
        for item in selected:
            if self.runtime.get_run(item["runId"]).last_event_seq != item["lastEventSeq"]:
                raise CandidateError("Selected result changed during verification")
        return self._persist_receipt(root, candidate_id, receipt)

    def _persist_receipt(self, root, candidate_id, receipt):
        task = self.runtime.create_task(project_root=str(root), prompt="Combine explicitly selected task results")
        self.runtime.create_run(task_id=task.task_id, run_id=candidate_id, routing={"combined_candidate": True})
        self.runtime.record(run_id=candidate_id, type="candidate.prepared", payload=receipt,
                            source="combined_candidate")
        return self.get(str(root), candidate_id)

    def prepare_shared(self, project_root, store, proposal_ids, *, expected_revision, verify,
                       session_id, epoch, store_path, hub_path, context_supplier=None):
        """Assemble explicitly selected owner proposals into the native receipt store."""
        from collab_runtime.proposals import read_proposal, _validate_recorded_provenance
        from collab_runtime.models import safe_id
        if type(verify) is not bool or verify is not True:
            raise CandidateError("Shared candidates require explicit verification")
        if not isinstance(proposal_ids, list) or not 1 <= len(proposal_ids) <= 2:
            raise CandidateError("Select one or two shared proposals")
        ids = [safe_id(item, "proposal id") for item in proposal_ids]
        if len(set(ids)) != len(ids):
            raise CandidateError("Selected proposal ids must be unique")
        root = Path(project_root).resolve(strict=True)
        if Path(store_path).resolve(strict=True) == root or root in Path(store_path).resolve(strict=True).parents:
            raise CandidateError("Owner metadata store must be outside the source project")
        if Path(hub_path).resolve(strict=True) == root or root in Path(hub_path).resolve(strict=True).parents:
            raise CandidateError("Owner metadata hub must be outside the source project")
        repo = Path(_git_text(["rev-parse", "--show-toplevel"], cwd=root).strip()).resolve()
        commit = _git_text(["rev-parse", "HEAD"], cwd=repo).strip()
        if repo != root or not commit or _git_text(["status", "--porcelain", "--untracked-files=all"], cwd=repo).strip():
            raise CandidateError("Shared candidates require a clean top-level committed source")
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
            raise CandidateError("Invalid source baseline")
        revision, current = store.fetch_state()
        if revision != expected_revision or current.session_id != session_id or current.base_commit != commit:
            raise CandidateError("Owner session or source baseline changed")
        if context_supplier is not None:
            context_supplier(session_id, epoch, revision, current.context_hash)
        proposals = []
        selected = []
        for identifier in sorted(ids):
            proposal = read_proposal(store, identifier)
            _validate_recorded_provenance(store, proposal, revision, current, require_live_task_match=True)
            if proposal.context_hash != current.context_hash or proposal.session_id != session_id or proposal.base_commit != commit:
                raise CandidateError("Proposal context or session is stale")
            proposals.append(proposal)
            selected.append({"proposalId": proposal.proposal_id, "taskId": proposal.task_id,
                             "owner": proposal.owner, "proposalRevision": store.remote_proposal_head(proposal.proposal_id)})
        if len({item["taskId"] for item in selected}) != len(selected):
            raise CandidateError("Select at most one cumulative proposal per task")
        listing = assembly._read_baseline_listing(repo, commit)
        listing_by_path = {entry.path: entry for entry in listing}
        baseline = assembly._read_baseline_blobs(repo, listing)
        assembly._verify_before_identities(listing_by_path, proposals)
        files = assembly._merge_changes(SimpleNamespace(store_path=repo), baseline, proposals)
        assembly._validate_final_set(files)
        final = {path: ("100755" if mode == 0o755 else "100644", raw) for path, raw, mode in files}
        changed = sorted(path for path in set(baseline) | set(final) if baseline.get(path) != final.get(path))
        if not changed:
            raise CandidateError("Selected proposals have no combined changes")
        # Recheck owner revision/context immediately before candidate creation.
        fresh_revision, fresh = store.fetch_state()
        if fresh_revision != revision or fresh.context_hash != current.context_hash:
            raise CandidateError("Owner session changed during assembly")
        if context_supplier is not None:
            context_supplier(session_id, epoch, revision, current.context_hash)
        candidate_id = "candidate_" + uuid.uuid4().hex
        if any(path.is_symlink() for path in (self.output_base, *self.output_base.parents)):
            raise CandidateError("Unsafe candidate output directory")
        self.output_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = assembly._validate_output_path(self.output_base / candidate_id, project=repo, hub=Path(hub_path), store_path=Path(store_path))
        assembly._materialize(output, files)
        paths = tuple(path for path, _, _ in files)
        verification = assembly._run_verification(output, paths)
        fingerprint, complete = assembly._fingerprint_records(output, paths)
        if not complete or verification.get("status") == "pass" and not verification.get("fingerprint_complete"):
            verification = {**verification, "status": "invalidated"}
        receipt = {
            "candidateId": candidate_id, "projectRoot": str(root), "candidateDir": str(output),
            "baseCommit": commit, "selected": [], "selectedProposals": selected,
            "sharedProvenance": {"sessionId": session_id, "baseCommit": commit,
                "contextHash": current.context_hash, "revision": revision, "epoch": epoch,
                "proposalIds": [item["proposalId"] for item in selected],
                "storePath": str(Path(store_path).resolve()), "hubPath": str(Path(hub_path).resolve())},
            "originalPaths": list(paths), "changedPaths": changed, "fingerprint": fingerprint,
            "fingerprintComplete": complete, "verification": verification, "state": "prepared",
            "checkpointId": None,
            "beforeHashes": {rel: _hash(baseline.get(rel, (None, None))[1]) for rel in changed},
            "afterHashes": {rel: _hash(final.get(rel, (None, None))[1]) for rel in changed},
            "beforeModes": {rel: baseline.get(rel, (None, None))[0] for rel in changed},
            "afterModes": {rel: final.get(rel, (None, None))[0] for rel in changed},
        }
        final_revision, final_state = store.fetch_state()
        if final_revision != revision or final_state.context_hash != current.context_hash:
            raise CandidateError("Owner session changed during verification")
        if context_supplier is not None:
            context_supplier(session_id, epoch, revision, current.context_hash)
        if _git_text(["rev-parse", "HEAD"], cwd=repo).strip() != commit or _git_text(["status", "--porcelain", "--untracked-files=all"], cwd=repo).strip():
            raise CandidateError("Source baseline changed during verification")
        return self._persist_receipt(root, candidate_id, receipt)

    def _record(self, project_root, candidate_id):
        run = self.runtime.get_run(candidate_id)
        task = self.runtime.store.get_task(run.task_id)
        receipt = run.workspace_snapshot
        if (run.routing != {"combined_candidate": True} or task.project_root != project_root
                or not isinstance(receipt, dict) or receipt.get("candidateId") != candidate_id
                or receipt.get("projectRoot") != project_root):
            raise CandidateError("Candidate identity/project mismatch")
        return run, receipt

    def get(self, project_root, candidate_id):
        _run, receipt = self._record(project_root, candidate_id)
        return {key: value for key, value in receipt.items()
                if key not in {"beforeHashes", "afterHashes", "beforeModes", "afterModes", "checkpointModes", "originalPaths"}}

    def list(self, project_root):
        rows = self.runtime.store.list_runs(project_root=project_root, limit=128)
        result = []
        for row in rows:
            if row.routing != {"combined_candidate": True}:
                continue
            try:
                result.append(self.get(project_root, row.run_id))
            except CandidateError:
                continue  # An interrupted/unprepared record grants no authority.
        return result[:32]

    def apply(self, project_root, candidate_id):
        lease = WorkspaceLease.acquire(self.runtime.store.db_path, candidate_id)
        try:
            run, receipt = self._record(project_root, candidate_id)
            if receipt["state"] != "prepared" or receipt["verification"].get("status") != "pass":
                raise CandidateError("Only a newly verified, unapplied candidate may be integrated")
            output = Path(receipt["candidateDir"])
            if output != self.output_base.resolve() / candidate_id:
                raise CandidateError("Candidate output identity mismatch")
            fingerprint, complete = assembly._fingerprint_records(output, tuple(receipt["originalPaths"]))
            if (not complete or not receipt["fingerprintComplete"] or fingerprint != receipt["fingerprint"]
                    or fingerprint != receipt["verification"].get("fingerprint_after")):
                raise CandidateError("Candidate changed after verification")
            provenance = receipt.get("sharedProvenance")
            if provenance:
                if self.owner_context_supplier is None:
                    raise CandidateError("Selected owner session is unavailable")
                owner = self.owner_context_supplier(Path(project_root), provenance)
                from collab_runtime.proposals import read_proposal, _validate_recorded_provenance
                owner_store = owner.get("store") if isinstance(owner, dict) else None
                if owner_store is None:
                    raise CandidateError("Selected owner store is unavailable")
                revision, current = owner_store.fetch_state()
                if revision != provenance["revision"] or current.context_hash != provenance["contextHash"]:
                    raise CandidateError("Shared context changed after candidate preparation")
                for selected in receipt.get("selectedProposals", []):
                    proposal = read_proposal(owner_store, selected["proposalId"])
                    _validate_recorded_provenance(owner_store, proposal, revision, current, require_live_task_match=True)
                    if owner_store.remote_proposal_head(proposal.proposal_id) != selected["proposalRevision"] or proposal.task_id != selected["taskId"] or proposal.owner != selected["owner"]:
                        raise CandidateError("Selected proposal provenance changed")
            if _git_text(["rev-parse", "HEAD"], cwd=Path(project_root)).strip() != receipt["baseCommit"]:
                raise CandidateError("Source baseline changed")
            if provenance and _git_text(["status", "--porcelain", "--untracked-files=all"], cwd=Path(project_root)).strip():
                raise CandidateError("Source has uncommitted edits")
            prepared = {rel: _file(output, rel) for rel in receipt["changedPaths"]}
            for rel, raw in prepared.items():
                if (_hash(raw) != receipt["afterHashes"][rel]
                        or _mode(output, rel) != receipt["afterModes"][rel]):
                    raise CandidateError("Candidate bytes do not match verified output")
            return self._write(project_root, run, receipt, prepared)
        finally:
            lease.close()

    def _write(self, root, run, receipt, prepared):
        for rel in receipt["changedPaths"]:
            if _hash(_file(Path(root), rel)) != receipt["beforeHashes"][rel]:
                raise CandidateError("Source changed; integration refused")
            if _mode(Path(root), rel) != receipt["beforeModes"][rel]:
                raise CandidateError("Source file mode changed")
        proj = Project(root)
        resolve_within_workspace(Path(root), ".imece/checkpoints", reject_symlinks=True)
        store = CheckpointStore(root)
        checkpoint = store.create(proj, receipt["changedPaths"], run.run_id)
        checkpoint_record = json.loads(_read(Path(store._path(checkpoint["id"])), 2 * assembly.MAX_BASELINE_TOTAL))
        checkpoint_modes = {item["path"]: item.get("mode") for item in checkpoint_record["files"]}
        for rel in receipt["changedPaths"]:
            if _hash(_file(Path(root), rel)) != receipt["beforeHashes"][rel]:
                raise CandidateError("Source changed while preparing checkpoint")
        try:
            for rel, raw in prepared.items():
                target = resolve_within_workspace(Path(root), rel, reject_symlinks=True)
                if raw is None:
                    if target.exists():
                        target.unlink()
                else:
                    store._write_atomic(str(target), raw)
                    os.chmod(target, 0o755 if receipt["afterModes"][rel] == "100755" else 0o644)
            self.runtime.record(run_id=run.run_id, type="candidate.applied",
                payload={"checkpointId": checkpoint["id"], "checkpointModes": checkpoint_modes}, expected_last_event_seq=run.last_event_seq,
                source="combined_candidate")
        except Exception:
            store.restore(proj, checkpoint["id"])
            raise
        return {"applied": receipt["changedPaths"], "checkpointId": checkpoint["id"]}

    def rollback(self, project_root, candidate_id):
        lease = WorkspaceLease.acquire(self.runtime.store.db_path, candidate_id)
        try:
            run, receipt = self._record(project_root, candidate_id)
            if receipt["state"] != "applied" or not receipt.get("checkpointId"):
                raise CandidateError("Candidate has not been applied")
            for rel in receipt["changedPaths"]:
                if (_hash(_file(Path(project_root), rel)) != receipt["afterHashes"][rel]
                        or _mode(Path(project_root), rel) != receipt["afterModes"][rel]):
                    raise CandidateError("Source edited after apply; rollback refused")
            proj = Project(project_root)
            resolve_within_workspace(Path(project_root), ".imece/checkpoints", reject_symlinks=True)
            store = CheckpointStore(project_root)
            checkpoint_path = Path(store._path(receipt["checkpointId"]))
            checkpoint = json.loads(_read(checkpoint_path, 2 * assembly.MAX_BASELINE_TOTAL))
            if (checkpoint.get("id") != receipt["checkpointId"] or checkpoint.get("runId") != run.run_id
                    or not isinstance(checkpoint.get("files"), list)
                    or sorted(item.get("path", "") for item in checkpoint["files"]) != sorted(receipt["changedPaths"])):
                raise CandidateError("Checkpoint provenance does not match candidate")
            for item in checkpoint["files"]:
                if (type(item.get("exists")) is not bool
                        or item.get("mode") != receipt["checkpointModes"].get(item["path"])):
                    raise CandidateError("Checkpoint existence/mode metadata is invalid")
                raw = base64.b64decode(item["content"], validate=True)
                expected = receipt["beforeHashes"][item["path"]]
                if (_hash(raw) if item["exists"] else None) != expected:
                    raise CandidateError("Checkpoint contents changed")
            safety = store.create(proj, receipt["changedPaths"], run.run_id)
            try:
                restored = store.restore(proj, receipt["checkpointId"])
                self.runtime.record(run_id=run.run_id, type="candidate.rolled_back", payload={},
                    expected_last_event_seq=run.last_event_seq, source="combined_candidate")
            except Exception:
                store.restore(proj, safety["id"])
                raise
            return {"restored": restored}
        finally:
            lease.close()
