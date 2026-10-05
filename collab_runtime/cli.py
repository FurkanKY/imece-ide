"""collab_runtime.cli — offline collaboration CLI (metadata + explicit code proposals).

`python -m collab_runtime <command> ...` operates ONLY on:
- the source checkout's committed HEAD (read-only), plus the one explicitly
  written, git-ignored `.imece/shared-context.json` binding artifact, and
- dedicated bare git repositories (one local hub + one bare store per
  checkout) that transport schema1 session metadata and, only through the
  two EXPLICIT code-proposal commands, user-selected raw code artifacts:
  `proposal-publish` captures and publishes selected changes as private
  immutable hub refs; `proposal-list` prints metadata receipts only; the
  receipt/revision output NEVER includes proposal file contents. The
  METADATA commands (init/join/status/sync/context-set/task-set/bind)
  never read or transport any code.

No network remotes (URL-like/scp-like are rejected), no user config edits,
no authentication, no logs/credentials gathered. Failures print one fixed,
actionable message and return a nonzero exit code (CollabError.exit_code;
6 for filesystem errors, 70 for unexpected internal errors) — never a
traceback, never raw git stderr or metadata values.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from collab_runtime.context import (
    ARTIFACT_RELPATH,
    MAX_ARTIFACT_BYTES,
    SharedSnapshot,
    read_regular_file_bounded,
)
from collab_runtime.candidates import CandidateConflictError
from collab_runtime.errors import CollabError, ValidationError
from collab_runtime.proposals import capture_proposal, list_proposals, publish_proposal
from collab_runtime.models import (
    ALL_STATUSES,
    build_context,
    build_initial_state,
    build_task,
    canonical_json,
    canonical_json_bytes,
    compute_overlaps,
    parse_json_bytes,
)
from collab_runtime.store import GitStore, validate_remote
from workspace.base import resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError

_CONTEXT_FILE_KEYS = frozenset({"goal", "decisions", "interfaces"})
_FS_EXIT_CODE = 6
_INTERNAL_EXIT_CODE = 70
_CANDIDATE_VERIFICATION_EXIT = 9  # fail/timeout/error/invalidated verification


def _emit(payload: dict) -> None:
    print(canonical_json(payload))


# -- shared placement validations (all BEFORE anything is created) --------


def _require_new_store_path(store_raw: str, toplevel: Path) -> Path:
    path = Path(store_raw).resolve()
    if path.exists() and not path.is_dir():
        raise ValidationError("store path must be a directory.")
    if path.exists() and any(path.iterdir()):
        raise ValidationError("store path must be an empty or non-existent directory.")
    GitStore.ensure_outside_project(path, toplevel, what="store")
    return path


def _validate_hub(remote: str, toplevel: Path, store_path: Path) -> str:
    hub = validate_remote(remote)
    GitStore.ensure_outside_project(Path(hub), toplevel, what="hub")
    if Path(hub) == store_path:
        raise ValidationError("the store and the hub must be two different bare repositories.")
    return hub


# -- commands -------------------------------------------------------------


def cmd_hub_init(args: argparse.Namespace) -> dict:
    hub = GitStore.create_bare(Path(args.path), what="hub")
    return {"command": "hub-init", "hub": str(hub)}


def cmd_init(args: argparse.Namespace) -> dict:
    project = Path(args.project)
    head = GitStore.project_head(project)
    toplevel = GitStore.project_toplevel(project)
    store_path = _require_new_store_path(args.store, toplevel)
    hub = _validate_hub(args.remote, toplevel, store_path)
    # Build/validate the initial state BEFORE any directory is created, so an
    # invalid input can never leave an occupied store path behind.
    state = build_initial_state(
        session_id=args.session_id, target_version=args.target_version, base_commit=head
    ).with_context(build_context(goal=args.goal, decisions=[], interfaces={}))
    GitStore.create_bare(store_path, what="store")
    store = GitStore(store=store_path, remote=hub)
    revision = store.init_session(state)
    return {
        "command": "init",
        "revision": revision,
        "store": str(store_path),
        "session_id": state.session_id,
        "base_commit": head,
        "context_hash": state.context.content_hash,
    }


def cmd_join(args: argparse.Namespace) -> dict:
    project = Path(args.project)
    head = GitStore.project_head(project)
    toplevel = GitStore.project_toplevel(project)
    store_path = _require_new_store_path(args.store, toplevel)
    hub = _validate_hub(args.remote, toplevel, store_path)
    GitStore.create_bare(store_path, what="store")
    store = GitStore(store=store_path, remote=hub)
    revision, state = store.fetch_state()
    if state.base_commit != head:
        raise ValidationError(
            "the project's committed HEAD does not match the session baseline; "
            "join from a checkout of the session's base commit. "
            "The client store directory was created and is kept; retry with a fresh empty store path."
        )
    return {
        "command": "join",
        "revision": revision,
        "store": str(store_path),
        "session_id": state.session_id,
        "base_commit": state.base_commit,
    }


def _status_payload(store: GitStore) -> dict:
    revision, state = store.fetch_state()
    current = state.context.content_hash
    changed: dict[str, str] = {}
    for task_id, task in sorted(state.tasks.items()):
        at = store.context_hash_at(task.context_revision, head=revision)
        if at is None:
            changed[task_id] = "unknown"  # missing provenance is never reported as valid evidence
        elif at == current:
            changed[task_id] = "unchanged"
        else:
            changed[task_id] = "changed"
    state_dict = state.to_dict()
    return {
        "revision": revision,
        "context_hash": current,
        "state": state_dict,
        "tasks": state_dict["tasks"],
        "overlaps": compute_overlaps(state.tasks),
        "context_changed_tasks": changed,
    }


def cmd_status(args: argparse.Namespace) -> dict:
    return _status_payload(GitStore(store=Path(args.store), remote=args.remote))


def cmd_context_set(args: argparse.Namespace) -> dict:
    store = GitStore(store=Path(args.store), remote=args.remote)
    raw = read_regular_file_bounded(Path(args.context), what="context file")
    obj = parse_json_bytes(raw, what="context file")
    if not isinstance(obj, dict) or set(obj) != _CONTEXT_FILE_KEYS:
        raise ValidationError(
            "context file must be a JSON object with exactly the keys goal, decisions, interfaces."
        )
    context = build_context(goal=obj["goal"], decisions=obj["decisions"], interfaces=obj["interfaces"])
    revision = store.update_context(context, expected_revision=args.expected_revision)
    return {"command": "context-set", "revision": revision, "context_hash": context.content_hash}


def cmd_task_set(args: argparse.Namespace) -> dict:
    store = GitStore(store=Path(args.store), remote=args.remote)
    task = build_task(
        task_id=args.task_id,
        owner=args.owner,
        goal=args.goal,
        scopes=list(args.scope),
        status=args.status,
        context_revision=args.context_revision if args.context_revision else args.expected_revision,
    )
    revision = store.upsert_task(task, expected_revision=args.expected_revision)
    return {"command": "task-set", "revision": revision, "task_id": task.id}


def _write_binding_artifact(target: Path, payload: bytes) -> None:
    """Write the bounded artifact ATOMICALLY, always: a private, exclusively
    created temp file in the SAME directory (mkstemp, 0600, O_EXCL — never a
    symlink clobber), one fsync, then os.replace. Transient temp visibility
    under exact-file ignore rules is acceptable; a torn write, a leftover
    temp or a corrupted previous artifact is not — the temp is always
    removed, even when the replace itself fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def cmd_bind(args: argparse.Namespace) -> dict:
    project = Path(args.project)
    store = GitStore(store=Path(args.store), remote=args.remote)
    revision, state = store.fetch_state()
    if args.task_id not in state.tasks:
        raise ValidationError("the selected task does not exist in the shared session state.")
    if store.store_path == Path(store.remote):
        raise ValidationError("the store and the hub must be two different bare repositories.")
    toplevel = GitStore.project_toplevel(project)
    GitStore.ensure_outside_project(store.store_path, toplevel, what="store")
    GitStore.ensure_outside_project(Path(store.remote), toplevel, what="hub")
    head = GitStore.project_head(project)
    if head != state.base_commit:
        raise ValidationError(
            "the project's committed HEAD does not match the session base commit; "
            "refusing to bind a stale snapshot."
        )
    try:
        target = resolve_within_workspace(project, ARTIFACT_RELPATH, reject_symlinks=True)
    except WorkspaceBoundaryError:
        raise ValidationError(
            "the binding artifact path must not traverse symlinks (including .imece) or leave the project."
        ) from None
    rel = target.relative_to(toplevel).as_posix()
    if not GitStore.path_is_ignored(toplevel, rel):
        raise ValidationError(f"add {ARTIFACT_RELPATH} to ignore rules")
    snapshot = SharedSnapshot(
        revision=revision, context_hash=state.context.content_hash, state=state, task_id=args.task_id
    )
    payload = canonical_json_bytes(snapshot.to_dict())
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValidationError(f"the binding artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit.")
    _write_binding_artifact(target, payload)
    return {
        "command": "bind",
        "bound": rel,
        "revision": revision,
        "task_id": args.task_id,
        "context_hash": snapshot.context_hash,
    }


# -- code-proposal + candidate commands ------------------------------------


def _placement_guard(store: GitStore, project: Path) -> Path:
    """The store and hub must live outside the source repository toplevel."""
    toplevel = GitStore.project_toplevel(project)
    GitStore.ensure_outside_project(store.store_path, toplevel, what="store")
    GitStore.ensure_outside_project(Path(store.remote), toplevel, what="hub")
    return toplevel


def cmd_proposal_publish(args: argparse.Namespace) -> dict:
    """Capture then publish one code proposal (two explicit steps: the
    publish re-fetches and revalidates the expected revision token, so a
    stale session fails cleanly before anything is pushed). The output is a
    metadata receipt: revisions, selected changed PATHS and the advisory
    out-of-scope warning — never proposal file contents."""
    store = GitStore(store=Path(args.store), remote=args.remote)
    _placement_guard(store, Path(args.project))
    proposal = capture_proposal(
        store, Path(args.project), args.proposal_id, args.task_id,
        list(args.path), expected_revision=args.expected_revision,
    )
    child, proposal_commit = publish_proposal(
        store, proposal, expected_revision=args.expected_revision
    )
    warnings = (
        [f"out_of_scope: {', '.join(proposal.out_of_scope_paths)}"]
        if proposal.out_of_scope_paths else []
    )
    return {
        "command": "proposal-publish",
        "revision": child,
        "proposal_commit": proposal_commit,
        "proposal_id": proposal.proposal_id,
        "task_id": proposal.task_id,
        "owner": proposal.owner,
        "session_id": proposal.session_id,
        "base_commit": proposal.base_commit,
        "context_revision": proposal.context_revision,
        "context_hash": proposal.context_hash,
        "changed_paths": [entry.path for entry in proposal.files],
        "out_of_scope_paths": list(proposal.out_of_scope_paths),
        "warnings": warnings,
    }


def cmd_proposal_list(args: argparse.Namespace) -> dict:
    """Print metadata receipts of every published proposal (id, task, owner,
    session/base/context provenance, file count) — no file contents. NOTE:
    the transfer is not metadata-only: each bounded artifact is fully
    fetched so its recorded provenance can be validated before the receipt
    is produced."""
    store = GitStore(store=Path(args.store), remote=args.remote)
    receipts = list_proposals(store)
    return {
        "command": "proposal-list",
        "proposals": [
            {
                "proposal_id": receipt.proposal_id,
                "commit": receipt.commit,
                "task_id": receipt.task_id,
                "owner": receipt.owner,
                "session_id": receipt.session_id,
                "base_commit": receipt.base_commit,
                "context_revision": receipt.context_revision,
                "context_hash": receipt.context_hash,
                "file_count": receipt.file_count,
            }
            for receipt in receipts
        ],
        "notes": [
            "metadata receipts only (no proposal file contents are returned); "
            "each bounded artifact is currently fully transferred to produce and "
            "validate these receipts",
        ],
    }


def cmd_candidate(args: argparse.Namespace) -> dict:
    """Materialize a NEW dedicated candidate directory outside source/hub/
    store from the explicitly selected published proposals plus the
    committed baseline; verification runs only with `--verify`."""
    from collab_runtime.candidates import assemble_candidate

    store = GitStore(store=Path(args.store), remote=args.remote)
    _placement_guard(store, Path(args.project))
    return assemble_candidate(
        store, Path(args.project), list(args.proposal), args.output,
        expected_revision=args.expected_revision, verify=bool(args.verify),
    )


# -- argument parser / entry point ----------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m collab_runtime",
        description=(
            "Offline collaboration runtime (local bare hubs, no network): "
            "metadata session commands plus explicit code-proposal/candidate steps."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    hub = sub.add_parser("hub-init", help="create a new, empty, dedicated bare hub repository")
    hub.add_argument("--path", required=True, help="empty or non-existent directory for the bare hub")

    init = sub.add_parser("init", help="create a session from the project's committed HEAD + goal")
    for key in ("store", "remote", "project", "session-id", "target-version", "goal"):
        init.add_argument(f"--{key}", required=True)

    join = sub.add_parser("join", help="create a client store and join the existing session")
    for key in ("store", "remote", "project"):
        join.add_argument(f"--{key}", required=True)

    for name, help_text in (("status", "print session metadata as JSON"), ("sync", "sync and print session metadata as JSON")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--store", required=True)
        cmd.add_argument("--remote", required=True)

    ctx = sub.add_parser("context-set", help="replace the shared context from an explicit JSON file")
    ctx.add_argument("--store", required=True)
    ctx.add_argument("--remote", required=True)
    ctx.add_argument("--context", required=True, help="regular JSON file with exactly goal/decisions/interfaces")
    ctx.add_argument("--expected-revision", required=True, help="current session revision (compare-and-swap)")

    task = sub.add_parser("task-set", help="add or update one task")
    task.add_argument("--store", required=True)
    task.add_argument("--remote", required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--owner", required=True)
    task.add_argument("--goal", required=True)
    task.add_argument("--scope", action="append", default=[], help="repo-relative scope; repeatable")
    task.add_argument("--status", required=True, choices=tuple(ALL_STATUSES))
    task.add_argument("--expected-revision", required=True, help="current session revision (compare-and-swap)")
    task.add_argument("--context-revision", default=None, help="prior context revision (defaults to expected)")

    bind = sub.add_parser("bind", help="write the git-ignored shared binding artifact")
    for key in ("store", "remote", "project", "task-id"):
        bind.add_argument(f"--{key}", required=True)

    publish = sub.add_parser(
        "proposal-publish",
        help="capture and publish one selected-changes code proposal (explicit raw-code step)",
    )
    for key in ("store", "remote", "project", "proposal-id", "task-id"):
        publish.add_argument(f"--{key}", required=True)
    publish.add_argument(
        "--path", action="append", required=True,
        help="repo-relative FILE path; repeatable (at least one required)",
    )
    publish.add_argument("--expected-revision", required=True, help="current session revision (compare-and-swap)")

    listing = sub.add_parser(
        "proposal-list", help="print proposal metadata receipts as JSON (no file contents)"
    )
    listing.add_argument("--store", required=True)
    listing.add_argument("--remote", required=True)

    cand = sub.add_parser(
        "candidate",
        help="materialize a NEW dedicated candidate directory from explicitly selected proposals",
    )
    for key in ("store", "remote", "project", "output", "expected-revision"):
        cand.add_argument(f"--{key}", required=True)
    cand.add_argument("--proposal", action="append", required=True, help="published proposal id; repeatable")
    cand.add_argument(
        "--verify", action="store_true",
        help="EXPLICITLY authorize executing the candidate's project checks (not a sandbox)",
    )

    return parser


_COMMANDS = {
    "hub-init": cmd_hub_init,
    "init": cmd_init,
    "join": cmd_join,
    "status": cmd_status,
    "sync": cmd_status,
    "context-set": cmd_context_set,
    "task-set": cmd_task_set,
    "bind": cmd_bind,
    "proposal-publish": cmd_proposal_publish,
    "proposal-list": cmd_proposal_list,
    "candidate": cmd_candidate,
}

_CANDIDATE_FAILURE_STATUSES = frozenset({"fail", "timeout", "error", "invalidated"})


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = _COMMANDS[args.command](args)
    except CandidateConflictError as exc:
        # structured conflict receipt: same shape as a real candidate receipt
        # (verification as an object; candidate_dir null — nothing created),
        # with conflict_paths retained for backwards compatibility.
        _emit({
            "command": "candidate",
            "candidate_dir": None,
            "conflicts": exc.conflict_paths,
            "conflict_paths": exc.conflict_paths,
            "verification": {
                "status": "not_run", "plan_id": None, "checks": [], "changed_content": False,
            },
        })
        print(exc.message, file=sys.stderr)
        return exc.exit_code
    except CollabError as exc:
        print(exc.message, file=sys.stderr)
        return exc.exit_code
    except OSError:
        print(
            "a filesystem error occurred; the command did not finish; "
            "inspect session status before retrying.",
            file=sys.stderr,
        )
        return _FS_EXIT_CODE
    except Exception:
        print(
            "an unexpected internal error occurred; the command did not finish; "
            "inspect session status before retrying.",
            file=sys.stderr,
        )
        return _INTERNAL_EXIT_CODE
    _emit(payload)
    if args.command == "candidate":
        status = payload.get("verification", {}).get("status")
        if status in _CANDIDATE_FAILURE_STATUSES:
            return _CANDIDATE_VERIFICATION_EXIT
    return 0
