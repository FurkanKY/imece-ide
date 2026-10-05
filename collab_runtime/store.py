"""collab_runtime.store — Git metadata transport for one collaboration session.

METADATA ONLY, LOCAL HUBS ONLY. One dedicated BARE git repository ("hub")
carries exactly one session on refs/heads/imece-session; the single file in
that branch's tree is session.json (schema1, see models). Each client keeps
its own bare "store" repo that fetches the authoritative hub head and
publishes child commits with ordinary fast-forward pushes (never force).
URL-like (ssh://, https://) and scp-like remotes are rejected: this slice
performs no network validation of any kind. No code, no working-tree content
and no project state is transported or modified: the source checkout is read
exactly once (`rev-parse HEAD`) and never written, re-configured or re-reffed.

Safety properties (verified in tests):
- argv-list git invocations only (no shell), 30s timeout; stdin is DEVNULL
  except when feeding trusted canonical JSON/tree bytes to plumbing.
- The subprocess environment is built from a small allowlist (PATH, HOME,
  TMPDIR/TEMP/TMP, USER/LOGNAME/USERNAME) plus ONLY our own controlled flags
  and identity: every ambient GIT_* override (GIT_DIR, GIT_WORK_TREE,
  GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, GIT_CONFIG*, GIT_TRACE,
  GIT_TEMPLATE_DIR, GIT_DEFAULT_HASH, ...) is dropped. Git never reads user
  or system config (GIT_CONFIG_GLOBAL/NOSYSTEM point at /dev/null), hubs are
  created with `--template=` so no user/system templates or sample hooks are
  borrowed, and hooks are disabled via `-c core.hooksPath=/dev/null`.
- Reads fetch the branch into a UNIQUE temporary ref, resolve the ACTUAL
  fetched SHA, read that immutable object, and delete the temporary ref. A
  concurrent hub update during the fetch is simply observed — the remote is
  authoritative; local refs are best-effort mirrors only.
- Publication = compare-and-swap the mirror to a freshly built child commit
  (hash-object/mktree/commit-tree, state re-validated through the strict
  parser) -> ordinary fast-forward push OF THAT LITERAL SHA (never force,
  never a shared ref another same-store operation could repoint). Any push
  failure CAS-restores (init: CAS-deletes) the mirror to its prior value, so
  a failed write is never mistaken for a committed one and a competing
  publication surfaces as StaleRevisionError. session_id, base_commit and
  target_version are immutable for in-place publication.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from collab_runtime.errors import (
    CollabError,
    GitOperationError,
    ProjectStateError,
    ReplayUnavailableError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (
    MAX_JSON_BYTES,
    MAX_TASKS,
    SessionState,
    SharedContext,
    Task,
    canonical_json_bytes,
    parse_json_bytes,
    parse_state_dict,
    safe_id,
    sha_hex,
)

SESSION_BRANCH = "refs/heads/imece-session"
STATE_PATH = "session.json"
ZERO_SHA = "0" * 40
# Proposal artifact transport (refs/heads/imece-proposals/<safe-id>, one root
# commit whose tree contains ONLY proposal.json). See collab_runtime.proposals.
PROPOSAL_REF_PREFIX = "refs/heads/imece-proposals/"
PROPOSAL_STATE_PATH = "proposal.json"
MAX_PROPOSAL_JSON_BYTES = 2 * 1024 * 1024
# Bounded state replay (see GitStore.replay_states): the recent FIRST-PARENT
# history window that may be replayed, and the maximum page size per call.
REPLAY_WINDOW = 64
MAX_REPLAY_PAGE = 32
_GIT_TIMEOUT = 30
_DEVNULL = os.devnull  # controlled, platform-correct null device for git config/hooks
_SCP_LIKE_RE = re.compile(r"^[^/\\]+:")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA_TOKEN_RE = re.compile(r"[0-9a-f]{40}")

_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Imece Collab",
    "GIT_AUTHOR_EMAIL": "imece-collab@local",
    "GIT_COMMITTER_NAME": "Imece Collab",
    "GIT_COMMITTER_EMAIL": "imece-collab@local",
}

_AMBIENT_ENV_KEYS = (
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "USERNAME",
    # Windows: git.exe needs these to locate its own support files and temp dir.
    "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT",
)

_CONTROLLED_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": _DEVNULL,
    "LC_ALL": "C.UTF-8",
}


def _is_windows() -> bool:
    return os.name == "nt"


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal allowlist env for local metadata git commands: no ambient
    GIT_* override survives; git sees only our controlled flags (plus
    identity via `extra`)."""
    env = {key: os.environ[key] for key in _AMBIENT_ENV_KEYS if key in os.environ}
    env.update(_CONTROLLED_ENV)
    if extra:
        env.update(extra)
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run git with an argv list (no shell), 30s timeout, hooks disabled."""
    argv = ["git", "-c", f"core.hooksPath={_DEVNULL}", *args]
    try:
        cp = subprocess.run(
            argv,
            cwd=str(cwd),
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            env=env if env is not None else _git_env(),
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitOperationError("git is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOperationError("a git operation timed out (30s limit).") from exc
    if check and cp.returncode != 0:
        raise GitOperationError("a git operation failed; the hub or store may be inaccessible.")
    return cp


def validate_remote(remote: str) -> str:
    """Local directory path of an existing bare repo ONLY. URL-like and
    scp-like remotes are rejected — no network validation exists here.
    Real absolute Windows drive paths (C:\\...) are classified as local
    paths on Windows, where they are indistinguishable from scp-like
    syntax without platform context; everywhere else they stay rejected."""
    if not isinstance(remote, str) or not remote.strip():
        raise ValidationError("hub remote must be a non-empty string.")
    scp_like = bool(_SCP_LIKE_RE.match(remote))
    if scp_like and _is_windows() and _WINDOWS_DRIVE_RE.match(remote):
        scp_like = False
    if "://" in remote or scp_like:
        raise ValidationError(
            "only local directory hub paths are supported; URL-like and scp-like remotes are rejected."
        )
    path = Path(remote).resolve()
    GitStore.require_bare(path, "hub remote")
    return str(path)


@dataclass(frozen=True)
class StateReplay:
    """One bounded page of session-state history replayed from the hub.

    `previous_state` is the state published at the replay cursor
    (`after_revision`); `states` holds at most `limit` STRICTLY NEWER states
    in chronological order as (revision, state) pairs; `has_more` reports
    whether published history continues beyond this page. Cursors older than
    the bounded replay window are never served — callers must fetch a
    fresh snapshot instead."""

    head_revision: str
    after_revision: str
    previous_state: SessionState
    states: tuple[tuple[str, SessionState], ...]
    has_more: bool


class GitStore:
    """A client-side bare metadata store bound to one local hub path."""

    def __init__(self, *, store: Path | str, remote: str):
        self._store = Path(store).resolve()
        self.require_bare(self._store, "store")
        self._remote = validate_remote(remote)
        if self._remote == str(self._store):
            raise ValidationError(
                "the client store and the hub must be distinct local repositories "
                "(sharing one path would let publication CAS the authoritative hub ref directly)."
            )

    @property
    def store_path(self) -> Path:
        return self._store

    @property
    def remote(self) -> str:
        return self._remote

    # -- repository placement helpers -------------------------------------

    @staticmethod
    def require_bare(path: Path, what: str) -> None:
        """`path` must BE a bare repository root itself — not merely a
        directory from which Git discovery would find one (e.g. an objects/
        or nested subdirectory of an existing bare repo)."""
        if not path.is_dir():
            raise ValidationError(f"{what} must be a directory.")
        cp = _run_git(["rev-parse", "--is-bare-repository"], cwd=path, check=False)
        if cp.returncode != 0 or cp.stdout.strip() != b"true":
            raise ValidationError(f"{what} must be a bare git repository.")
        cp = _run_git(["rev-parse", "--absolute-git-dir"], cwd=path, check=False)
        if cp.returncode != 0:
            raise ValidationError(f"{what} must be a bare git repository.")
        git_dir = Path(cp.stdout.decode("utf-8", "replace").strip())
        if git_dir.resolve() != path.resolve():
            raise ValidationError(
                f"{what} must be the bare repository root itself, not a directory inside one."
            )

    @staticmethod
    def create_bare(path: Path | str, *, what: str = "hub") -> Path:
        """Create an empty bare repository; refuses existing non-empty dirs.

        `--template=` avoids borrowing any user/system templates or hooks."""
        target = Path(path).resolve()
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValidationError(f"{what} path must be an empty or non-existent directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["init", "--bare", "-q", "--template=", str(target)], cwd=target.parent)
        GitStore.require_bare(target, what)
        return target

    @staticmethod
    def ensure_outside_project(path: Path | str, project: Path | str, *, what: str) -> Path:
        """Refuse hub/store paths inside (or equal to) the source checkout."""
        target = Path(path).resolve()
        root = Path(project).resolve()
        if target == root or root in target.parents:
            raise ValidationError(f"{what} must live outside the source project checkout.")
        return target

    @staticmethod
    def project_head(project: Path | str) -> str:
        """Read the committed HEAD SHA of a source checkout. Read-only: the
        project's index, refs, config and working tree are never touched."""
        root = Path(project).resolve()
        if not root.is_dir():
            raise ProjectStateError("project path does not exist or is not a directory.")
        cp = _run_git(["rev-parse", "--verify", "HEAD"], cwd=root, check=False)
        if cp.returncode != 0:
            raise ProjectStateError("project is not a git repository with a committed HEAD.")
        return cp.stdout.decode("utf-8", "replace").strip()

    @staticmethod
    def project_toplevel(project: Path | str) -> Path:
        """Resolve the ACTUAL repository toplevel containing `project` (which
        may be any subdirectory). Read-only."""
        root = Path(project).resolve()
        if not root.is_dir():
            raise ProjectStateError("project path does not exist or is not a directory.")
        cp = _run_git(["rev-parse", "--show-toplevel"], cwd=root, check=False)
        if cp.returncode != 0:
            raise ProjectStateError("project is not a git repository.")
        return Path(cp.stdout.decode("utf-8", "replace").strip()).resolve()

    @staticmethod
    def path_is_ignored(project: Path | str, rel: str) -> bool:
        """Plain `git check-ignore` (never --no-index) against the real
        repository: tracked paths and rule misses report as NOT ignored.
        Read-only."""
        cp = _run_git(["check-ignore", "-q", "--", rel], cwd=Path(project).resolve(), check=False)
        return cp.returncode == 0

    # -- reading the authoritative state -----------------------------------

    def remote_head(self) -> str | None:
        """Authoritative hub SHA for the session branch (None when absent)."""
        cp = _run_git(["ls-remote", self._remote, SESSION_BRANCH], cwd=self._store, check=False)
        if cp.returncode != 0:
            raise GitOperationError("could not reach the collaboration hub.")
        first = cp.stdout.decode("utf-8", "replace").split("\n", 1)[0].strip()
        if not first:
            return None
        try:
            return sha_hex(first.split("\t", 1)[0].strip(), "hub session revision")
        except ValidationError:
            raise GitOperationError("the hub returned an unexpected session ref.") from None

    def fetch_state(self) -> tuple[str, SessionState]:
        """Fetch the authoritative hub state; returns (fetched revision, state).

        The branch is fetched into a unique temporary ref, the ACTUAL fetched
        SHA is resolved and its immutable state object read, then the
        temporary ref is deleted. A concurrent hub update during the fetch is
        simply observed — the remote is authoritative; a stale earlier
        discovery never causes a spurious error.
        """
        tmp_ref = f"refs/imece-mirror/{uuid.uuid4().hex}"
        cp = _run_git(
            ["fetch", "--no-tags", self._remote, f"+{SESSION_BRANCH}:{tmp_ref}"],
            cwd=self._store, check=False,
        )
        try:
            if cp.returncode != 0:
                stderr = cp.stderr.decode("utf-8", "replace")
                if "couldn't find remote ref" in stderr:
                    raise SessionNotFoundError("no session exists on the hub yet; initialize one first.")
                raise GitOperationError("could not fetch the session from the collaboration hub.")
            fetched = self._rev_parse(tmp_ref)
            state = self._read_state(fetched)
        finally:
            _run_git(["update-ref", "-d", tmp_ref], cwd=self._store, check=False)
        self._refresh_mirror(fetched)
        return fetched, state

    def _rev_parse(self, ref: str) -> str:
        cp = _run_git(["rev-parse", "--verify", ref], cwd=self._store, check=False)
        if cp.returncode != 0:
            raise GitOperationError("the fetched session object could not be resolved.")
        sha = cp.stdout.decode("utf-8", "replace").strip()
        try:
            return sha_hex(sha, "fetched session revision")
        except ValidationError:
            raise GitOperationError("the hub returned an unexpected session revision.") from None

    def _local_head(self) -> str | None:
        cp = _run_git(["rev-parse", "--verify", SESSION_BRANCH], cwd=self._store, check=False)
        if cp.returncode != 0:
            return None
        return cp.stdout.decode("utf-8", "replace").strip()

    def _refresh_mirror(self, sha: str) -> None:
        """Best-effort CAS refresh of the mirror ref; never overwrites a
        concurrent same-store publication pointer."""
        current = self._local_head()
        self._cas_ref(sha, ZERO_SHA if current is None else current)

    def _read_state(self, commit: str) -> SessionState:
        cp = _run_git(["cat-file", "-s", f"{commit}:{STATE_PATH}"], cwd=self._store, check=False)
        if cp.returncode != 0:
            raise GitOperationError("the session state object is missing; the hub session is corrupt.")
        size = cp.stdout.strip()
        if not size.isdigit() or int(size) > MAX_JSON_BYTES:
            raise ValidationError(f"session state exceeds the {MAX_JSON_BYTES}-byte limit.")
        blob = _run_git(["cat-file", "blob", f"{commit}:{STATE_PATH}"], cwd=self._store).stdout
        return parse_state_dict(parse_json_bytes(blob, what="session state"))

    # -- publication (compare-and-swap, fast-forward only) -----------------

    def init_session(self, state: SessionState) -> str:
        """Create the session branch on the hub; one session per hub."""
        if self.remote_head() is not None:
            raise SessionAlreadyExistsError("a session already exists on this hub; join it instead.")
        commit = self._commit(state, parent=None, message="imece-collab: init session")
        if not self._cas_advance(commit):
            raise GitOperationError("local store conflict while creating the session branch.")
        try:
            self._push_commit(
                commit,
                stale_message="another client created the session concurrently; join instead.",
            )
        except CollabError:
            self._delete_ref(commit)  # CAS delete of OUR value; never clobbers another pointer
            raise
        return commit

    def publish(self, state: SessionState, *, expected_revision: str) -> str:
        """Publish a child state guarded by the authoritative hub revision.

        session_id, base_commit and target_version are immutable for in-place
        publication (an explicit migration API would be a separate operation).
        """
        expected = sha_hex(expected_revision, "expected_revision")
        head, current = self.fetch_state()
        if (state.session_id, state.base_commit, state.target_version) != (
            current.session_id, current.base_commit, current.target_version
        ):
            raise ValidationError(
                "session_id, base_commit and target_version are immutable; publication cannot change them."
            )
        if head != expected:
            raise StaleRevisionError(
                f"expected_revision is no longer current (the hub is at {head[:12]}); sync and retry."
            )
        commit = self._commit(state, parent=head, message="imece-collab: update session")
        if not self._cas_advance(commit):
            raise StaleRevisionError("the local store moved concurrently; re-sync and retry.")
        try:
            self._push_commit(
                commit,
                stale_message="publication rejected: the session moved on the hub; sync and retry.",
            )
        except CollabError:
            self._cas_ref(head, commit)  # CAS restore; never clobbers another operation's pointer
            raise
        return commit

    def update_context(self, context: SharedContext, *, expected_revision: str) -> str:
        """Replace the shared context; `expected_revision` is REQUIRED."""
        _head, current = self.fetch_state()
        return self.publish(current.with_context(context), expected_revision=expected_revision)

    def upsert_task(self, task: Task, *, expected_revision: str) -> str:
        """Add or update one task; `expected_revision` is REQUIRED. The
        task's context_revision must be a real revision of this session's
        published history (the head itself or one of its ancestors)."""
        head, current = self.fetch_state()
        if task.id not in current.tasks and len(current.tasks) >= MAX_TASKS:
            raise ValidationError(f"at most {MAX_TASKS} tasks are allowed.")
        if not self.is_ancestor(task.context_revision, head):
            raise ValidationError(
                "task context_revision must be an existing revision of this session's published history."
            )
        return self.publish(current.with_task(task), expected_revision=expected_revision)

    # -- historical context provenance --------------------------------------

    def is_ancestor(self, rev: str, head: str) -> bool:
        """True when `rev` equals `head` or is one of its ancestors. Any
        git failure (missing/garbage revision) counts as NOT ancestor."""
        cp = _run_git(["merge-base", "--is-ancestor", rev, head], cwd=self._store, check=False)
        return cp.returncode == 0

    def context_hash_at(self, revision: str, *, head: str) -> str | None:
        """Content hash of the session context at `revision`, ONLY when
        `revision` is the published `head` or a real ancestor of it.

        Both arguments are validated as full SHAs BEFORE any git call, and
        the state is read through the same bounded reader as the live state
        (an oversize/corrupt history blob is rejected, not streamed). Returns
        None for unreadable, oversized or foreign revisions — callers must
        treat missing provenance as unknown, never as valid evidence."""
        try:
            revision = sha_hex(revision, "task context_revision")
            head = sha_hex(head, "session head")
        except ValidationError:
            return None
        if not self.is_ancestor(revision, head):
            return None
        try:
            return self._read_state(revision).context.content_hash
        except CollabError:
            return None

    # -- bounded state replay ------------------------------------------------

    def replay_states(self, after_revision: str, *, limit: int = MAX_REPLAY_PAGE) -> StateReplay:
        """Replay one bounded page of this session's published state history.

        `after_revision` is the replay cursor: its own state is returned as
        `previous_state` and at most `limit` strictly NEWER states follow in
        chronological order (cursor == head yields an empty page). The cursor
        and limit are validated BEFORE any git call. One fetch pins the hub
        head and provides the head state; the recent FIRST-PARENT chain is
        enumerated with a bounded `rev-list` and merge/noncontiguous history
        is rejected as corrupt. A cursor outside the recent window (unknown,
        foreign or too old) raises ReplayUnavailableError whose fixed remedy
        is to fetch a new snapshot — deep history is never paged."""
        cursor = sha_hex(after_revision, "after_revision")
        if type(limit) is not int or not 1 <= limit <= MAX_REPLAY_PAGE:
            raise ValidationError(f"limit must be an integer between 1 and {MAX_REPLAY_PAGE}.")
        head, head_state = self.fetch_state()
        chain = self._first_parent_chain(head)
        # N transitions need N+1 revisions, including the oldest cursor.
        window = chain
        if cursor not in {commit for commit, _ in window}:
            raise ReplayUnavailableError(
                "the requested revision is not in the recent session history; fetch a new snapshot and retry."
            )
        chronological = [commit for commit, _ in reversed(window)]
        index = chronological.index(cursor)
        if cursor == head:
            return StateReplay(head, cursor, head_state, (), False)
        has_more = index + limit < len(chronological) - 1
        previous_state = self._read_state(cursor)
        states = tuple(
            (commit, head_state if commit == head else self._read_state(commit))
            for commit in chronological[index + 1 : index + 1 + limit]
        )
        identity = (head_state.session_id, head_state.base_commit, head_state.target_version)
        for state in (previous_state, *(state for _, state in states)):
            if (state.session_id, state.base_commit, state.target_version) != identity:
                raise ValidationError(
                    "replayed history changed session_id, base_commit or target_version; "
                    "the hub session is corrupt."
                )
        return StateReplay(head, cursor, previous_state, states, has_more)

    def _first_parent_chain(self, head: str) -> list[tuple[str, tuple[str, ...]]]:
        """Bounded enumeration of the recent FIRST-PARENT history of the
        pinned `head` (newest first): at most REPLAY_WINDOW+1 commits, one
        bounded rev-list. Merge commits and noncontiguous rev-list output
        are rejected as corrupt session history (fixed safe messages)."""
        cp = _run_git(
            ["rev-list", "--first-parent", f"--max-count={REPLAY_WINDOW + 1}", "--parents", head],
            cwd=self._store,
        )
        chain: list[tuple[str, tuple[str, ...]]] = []
        for line in cp.stdout.decode("utf-8", "replace").splitlines():
            tokens = line.split()
            if not tokens:
                continue
            if any(not _SHA_TOKEN_RE.fullmatch(token) for token in tokens):
                raise GitOperationError(
                    "the session history could not be enumerated (unexpected rev-list output)."
                )
            chain.append((tokens[0], tuple(tokens[1:])))
        if not chain or len(chain) > REPLAY_WINDOW + 1 or chain[0][0] != head:
            raise GitOperationError(
                "the session history could not be enumerated (unexpected rev-list output)."
            )
        for pos, (_commit, parents) in enumerate(chain):
            if len(parents) > 1:
                raise GitOperationError("the session history is corrupt (merge commits are not allowed).")
            if pos + 1 < len(chain) and (len(parents) != 1 or parents[0] != chain[pos + 1][0]):
                raise GitOperationError(
                    "the session history is corrupt (noncontiguous first-parent history)."
                )
        return chain

    def _commit(self, state: SessionState, *, parent: str | None, message: str) -> str:
        # Re-validate through the strict parser so directly-constructed
        # (builder-bypassing) dataclasses can never poison the remote.
        validated = parse_state_dict(state.to_dict())
        payload = canonical_json_bytes(validated.to_dict())
        if len(payload) > MAX_JSON_BYTES:
            raise ValidationError(f"session state exceeds the {MAX_JSON_BYTES}-byte limit.")
        return self._commit_payload(payload, path=STATE_PATH, parent=parent, message=message)

    def _commit_payload(self, payload: bytes, *, path: str, parent: str | None, message: str) -> str:
        """Build one immutable commit whose tree holds ONLY `path` -> `payload`
        (hash-object/mktree/commit-tree plumbing, no config/hook/filter
        execution; shared by the metadata session commit and proposal roots)."""
        env = _git_env(_IDENTITY_ENV)
        blob = _run_git(["hash-object", "-w", "--stdin"], cwd=self._store, stdin=payload, env=env).stdout.strip()
        tree = _run_git(
            ["mktree"], cwd=self._store, env=env,
            stdin=b"100644 blob %b\t%b\n" % (blob, path.encode()),
        ).stdout.strip()
        args = ["commit-tree", tree.decode("ascii")]
        if parent is not None:
            args += ["-p", parent]
        args += ["-m", message]
        commit = _run_git(args, cwd=self._store, env=env).stdout.decode("utf-8", "replace").strip()
        try:
            return sha_hex(commit, "created commit")
        except ValidationError:
            raise GitOperationError("git commit-tree returned an unexpected value.") from None

    def _push_commit(self, commit: str, *, stale_message: str) -> None:
        """Ordinary fast-forward push OF THIS LITERAL COMMIT: never force,
        and never a shared ref that a concurrent same-store operation could
        have repointed. Hook execution is already disabled globally."""
        cp = _run_git(
            ["push", self._remote, f"{commit}:{SESSION_BRANCH}"],
            cwd=self._store, check=False,
        )
        if cp.returncode == 0:
            return
        stderr = cp.stderr.decode("utf-8", "replace")
        if "[rejected]" in stderr or "non-fast-forward" in stderr:
            raise StaleRevisionError(stale_message)
        raise GitOperationError("could not publish to the collaboration hub (transport failure).")

    def _atomic_push(self, entries: list[tuple[str, str]], *, stale_message: str) -> None:
        """One atomic fast-forward-only push OF LITERAL SHAS (`--atomic`: the
        hub takes all listed refs or none, so a rejected session update can
        never orphan its companion proposal ref). Never force."""
        args = ["push", "--atomic", self._remote]
        for commit, ref in entries:
            args.append(f"{commit}:{ref}")
        cp = _run_git(args, cwd=self._store, check=False)
        if cp.returncode == 0:
            return
        stderr = cp.stderr.decode("utf-8", "replace")
        if "[rejected]" in stderr or "non-fast-forward" in stderr:
            raise StaleRevisionError(stale_message)
        raise GitOperationError("could not publish to the collaboration hub (transport failure).")

    # -- proposal artifact transport (private immutable refs) ---------------

    def _proposal_ref(self, proposal_id: str) -> str:
        """Validated literal refname for one proposal id (root refs can never
        be overwritten: a second ref creation with different content is a
        non-fast-forward rejection, and ids containing '..' or '.lock' are
        rejected before any git call)."""
        identifier = safe_id(proposal_id, "proposal_id")
        ref = PROPOSAL_REF_PREFIX + identifier
        cp = _run_git(["check-ref-format", ref], cwd=self._store, check=False)
        if cp.returncode != 0:
            raise ValidationError("proposal_id does not form a valid git refname.")
        return ref

    def remote_proposal_head(self, proposal_id: str) -> str | None:
        """Authoritative hub SHA for one proposal ref (None when absent)."""
        ref = self._proposal_ref(proposal_id)
        cp = _run_git(["ls-remote", self._remote, ref], cwd=self._store, check=False)
        if cp.returncode != 0:
            raise GitOperationError("could not reach the collaboration hub.")
        first = cp.stdout.decode("utf-8", "replace").split("\n", 1)[0].strip()
        if not first:
            return None
        try:
            return sha_hex(first.split("\t", 1)[0].strip(), "hub proposal revision")
        except ValidationError:
            raise GitOperationError("the hub returned an unexpected proposal ref.") from None

    def fetch_proposal_ref(self, proposal_id: str) -> tuple[str, bytes]:
        """Fetch one proposal ref into a UNIQUE temporary ref, validate its
        root commit structure (no parents; tree contains ONLY proposal.json)
        and return (commit SHA, bounded proposal.json bytes).

        The blob SIZE is verified against MAX_PROPOSAL_JSON_BYTES before any
        read (an oversize artifact is rejected, never streamed). The
        temporary ref is always deleted; the source checkout and origin
        configuration are never touched."""
        ref = self._proposal_ref(proposal_id)
        tmp_ref = f"refs/imece-mirror/{uuid.uuid4().hex}"
        cp = _run_git(
            ["fetch", "--no-tags", self._remote, f"+{ref}:{tmp_ref}"],
            cwd=self._store, check=False,
        )
        try:
            if cp.returncode != 0:
                stderr = cp.stderr.decode("utf-8", "replace")
                if "couldn't find remote ref" in stderr:
                    raise ValidationError("no proposal with that id is published on the hub.")
                raise GitOperationError("could not fetch the proposal from the collaboration hub.")
            commit = self._rev_parse(tmp_ref)
            cp_commit = _run_git(["cat-file", "-s", commit], cwd=self._store, check=False)
            if cp_commit.returncode != 0 or not cp_commit.stdout.strip().isdigit():
                raise GitOperationError("the proposal artifact commit could not be read.")
            if int(cp_commit.stdout.strip()) > MAX_JSON_BYTES:
                raise GitOperationError("the proposal artifact commit is corrupt (oversized commit object).")
            raw_commit = _run_git(["cat-file", "commit", commit], cwd=self._store).stdout
            headers = raw_commit.split(b"\n\n", 1)[0].split(b"\n")
            if any(h.startswith(b"parent ") for h in headers):
                raise GitOperationError(
                    "the proposal artifact commit is corrupt (it must have no source history)."
                )
            tree = next((h.split(b" ", 1)[1] for h in headers if h.startswith(b"tree ")), None)
            if tree is None:
                raise GitOperationError("the proposal artifact commit is corrupt (no tree).")
            listing = _run_git(["ls-tree", tree.decode("ascii")], cwd=self._store).stdout
            entries = [line.split(b"\t", 1) for line in listing.splitlines() if line.strip()]
            valid_tree = (
                len(entries) == 1
                and len(entries[0]) == 2
                and entries[0][1] == PROPOSAL_STATE_PATH.encode()
                and entries[0][0].startswith(b"100644 blob ")
            )
            if not valid_tree:
                raise GitOperationError("the proposal artifact tree must contain only proposal.json.")
            object_path = f"{commit}:{PROPOSAL_STATE_PATH}"
            cp_size = _run_git(["cat-file", "-s", object_path], cwd=self._store, check=False)
            if cp_size.returncode != 0:
                raise GitOperationError("the proposal artifact object is missing; the hub is corrupt.")
            size = cp_size.stdout.strip()
            if not size.isdigit() or int(size) > MAX_PROPOSAL_JSON_BYTES:
                raise ValidationError(f"proposal artifact exceeds the {MAX_PROPOSAL_JSON_BYTES}-byte limit.")
            payload = _run_git(["cat-file", "blob", object_path], cwd=self._store).stdout
        finally:
            _run_git(["update-ref", "-d", tmp_ref], cwd=self._store, check=False)
        return commit, payload

    def _cas_ref(self, new: str, old: str) -> bool:
        cp = _run_git(["update-ref", SESSION_BRANCH, new, old], cwd=self._store, check=False)
        return cp.returncode == 0

    def _cas_advance(self, new: str) -> bool:
        """Compare-and-swap the mirror to `new` based on its current value."""
        current = self._local_head()
        return self._cas_ref(new, ZERO_SHA if current is None else current)

    def _delete_ref(self, expected: str) -> None:
        """CAS delete: only removes the ref while it holds OUR value."""
        _run_git(["update-ref", "-d", SESSION_BRANCH, expected], cwd=self._store, check=False)
