"""collab_runtime.proposals — bounded CODE-PROPOSAL artifacts (v1).

A proposal captures USER-SELECTED cumulative file changes of one source
checkout relative to the session's immutable `base_commit`, tied to a VALID
authoritative bound context (the `.imece/shared-context.json` binding
artifact, see collab_runtime.context). Proposals are published as PRIVATE
IMMUTABLE git refs on the local hub — `refs/heads/imece-proposals/<safe-id>`
— one root commit whose tree contains ONLY `proposal.json`. Proposal commits
have NO parents: no source commit history and no source objects are linked
into the hub, and nothing beyond the USER-SELECTED file contents is ever
transported (the proposal artifact carries the selected raw bytes; all other
working-tree content stays local). The source checkout is only ever READ
(hardened `--no-optional-locks` / `--literal-pathspecs` / `core.fsmonitor=`
plumbing with the store's scrubbed environment and literal pathspecs; never
add/commit/index/filter commands).

Wire schema1 (exact keys; unknown fields, wrong types, duplicate keys,
floats/NaN, unpaired surrogates and every limit violation are rejected):

    {
      "schema": 1,
      "proposal_id": "<safe id>", "task_id": "<safe id>", "owner": "<safe id>",
      "session_id": "<safe id>", "base_commit": "<40-hex sha>",
      "context_revision": "<40-hex sha>", "context_hash": "<64-hex sha256>",
      "files": [   # strictly sorted by path, no duplicates, 1..MAX_FILES
        {"path": "<normalized repo-relative FILE path>",
         "before_oid": "<40-hex sha>|null", "before_mode": "<git mode>|null",
         "after_base64": "<canonical base64>|null", "after_mode": "<git mode>|null"}
      ]
    }

`before_*` both null => addition; `after_*` both null => deletion; both
present => modification (including mode-only changes, which carry the
identical content). Only modes 100644/100755 exist; contents must be UTF-8
text without NUL bytes (binary/symlink/gitlink changes are unsupported in
v1; arbitrary embedded secrets cannot be guaranteed absent and no redaction
ever modifies code).

Limits: <=MAX_FILES changed files, <=MAX_FILE_BYTES per file,
<=MAX_PROPOSAL_JSON_BYTES per proposal artifact (the 64KiB metadata limit
deliberately does NOT apply to proposals), paths <=512 chars.

Path policy: paths are exact normalized repo-relative FILE paths (no
directory expansion, no automatic all-changes capture). `.git`, `.imece`,
credential filenames (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`,
`id_ed25519`, `credentials.json`), traversal, absolute/drive/glob paths and
leaf-or-ancestor symlinks are rejected. Task scopes are ADVISORY: selected
paths outside the task scope are REPORTED on the captured proposal, never
silently accepted or rejected. Git-ignored current paths are rejected unless
tracked in the baseline. `project_root` must BE the repository toplevel
(monorepo subdirectories are explicitly unsupported in v1).

Transport: publication is one `git push --atomic` of literal SHAs carrying
the proposal root together with a CHILD metadata session commit (state
unchanged, message naming the proposal id/hash). A concurrent session move
rejects the whole push, so no orphan proposal ref can exist; the local
mirror is CAS-restored on failure while the hub stays authoritative. Reads
fetch the explicit ref into a unique temporary ref (sizechecked before
read), list receipts only (metadata, never code), and never configure
origin or gather source refs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collab_runtime.context import SharedSnapshot, load_project_snapshot, parse_snapshot_dict
from collab_runtime.errors import (
    CollabError,
    GitOperationError,
    ProjectStateError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (
    MAX_SCOPE_CHARS,
    SessionState,
    canonical_json_bytes,
    parse_state_dict,
    safe_id,
    sha_hex,
)
from collab_runtime.store import (
    MAX_PROPOSAL_JSON_BYTES,
    PROPOSAL_REF_PREFIX,
    PROPOSAL_STATE_PATH,
    SESSION_BRANCH,
    GitStore,
    _git_env,
    _run_git,
)
from workspace.base import resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError

PROPOSAL_SCHEMA = 1
MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_PROPOSALS = 256
FILE_MODES = frozenset({"100644", "100755"})

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_CREDENTIAL_LEAVES = frozenset({"id_rsa", "id_ed25519", "credentials.json"})
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_PROPOSAL_KEYS = frozenset({
    "schema", "proposal_id", "task_id", "owner", "session_id",
    "base_commit", "context_revision", "context_hash", "files",
})
_FILE_KEYS = frozenset({"path", "before_oid", "before_mode", "after_base64", "after_mode"})


# ---------------------------------------------------------------- paths ----


def _path_bad_chars(value: str) -> bool:
    return any(
        ord(ch) < 32 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in value
    )


def proposal_file_path(raw: Any, *, what: str = "proposal path") -> str:
    """Validate one explicitly selected repo-relative FILE path.

    Normalized (backslashes folded, no empty/'.' parts), traversal/absolute/
    drive/glob rejected, `.git` and `.imece` rejected at ANY component,
    directory scopes (trailing '/') rejected, and credential filenames
    (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`,
    `credentials.json`) always rejected — a proposal can never carry them,
    whatever the filesystem says. Symlink checks happen at capture time.
    """
    if not isinstance(raw, str):
        raise ValidationError(f"{what} must be a string.")
    if _path_bad_chars(raw):
        raise ValidationError(f"{what} must not contain control characters or surrogates.")
    if len(raw) > MAX_SCOPE_CHARS:
        raise ValidationError(f"{what} exceeds the {MAX_SCOPE_CHARS}-character limit.")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(normalized):
        raise ValidationError(f"{what} must be relative (absolute and drive paths rejected).")
    if any(ch in normalized for ch in "*?["):
        raise ValidationError(f"{what} must not contain glob characters (*, ?, [).")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise ValidationError(f"{what} must name a file.")
    if any(p == ".." for p in parts):
        raise ValidationError(f"{what} must not contain '..' (traversal rejected).")
    if any(p.casefold() in (".git", ".imece") for p in parts):
        raise ValidationError(f"{what} must not reference .git or .imece.")
    if normalized.endswith("/"):
        raise ValidationError(f"{what} must be an exact FILE path (directory scopes unsupported).")
    leaf = parts[-1].casefold()
    if leaf == ".env" or leaf.startswith(".env."):
        raise ValidationError(f"{what} looks like an environment/credential file (rejected).")
    if leaf in _CREDENTIAL_LEAVES or leaf.endswith(".pem") or leaf.endswith(".key"):
        raise ValidationError(f"{what} looks like a private key or credential file (rejected).")
    return "/".join(parts)


# -------------------------------------------------- source checkout reads ----


def _source_git(args: list[str], *, cwd: Path, check: bool = True):
    """Read-only git against the SOURCE checkout: `--no-optional-locks` and
    `--literal-pathspecs` (explicitly literal source path queries) plus
    `core.fsmonitor=` (and the store's scrubbed environment and disabled
    hooks) harden every read; no command here can take a lock, run a filter
    or write the index. Every path argument is treated as an exact literal
    name, never as pathspec magic."""
    return _run_git(
        ["--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=", *args],
        cwd=cwd, env=_git_env(), check=check,
    )


def _require_source_toplevel(project_root: Path | str) -> Path:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ProjectStateError("project path does not exist or is not a directory.")
    cp = _source_git(["rev-parse", "--show-toplevel"], cwd=root, check=False)
    if cp.returncode != 0:
        raise ProjectStateError("project is not a git repository.")
    top = Path(cp.stdout.decode("utf-8", "replace").strip()).resolve()
    if top != root:
        raise ValidationError(
            "project_root must be the repository toplevel; monorepo subdirectories are unsupported in v1."
        )
    return root


def _source_head(root: Path) -> str:
    cp = _source_git(["rev-parse", "--verify", "HEAD"], cwd=root, check=False)
    if cp.returncode != 0:
        raise ProjectStateError("project is not a git repository with a committed HEAD.")
    try:
        return sha_hex(cp.stdout.decode("utf-8", "replace").strip(), "project HEAD")
    except ValidationError:
        raise GitOperationError("the project returned an unexpected HEAD value.") from None


def _require_base_ancestor(root: Path, base_commit: str) -> None:
    """The session base must be an ancestor of the project HEAD so that both
    dirty work and later committed work can be captured cumulatively."""
    cp = _source_git(["merge-base", "--is-ancestor", base_commit, "HEAD"], cwd=root, check=False)
    if cp.returncode != 0:
        raise StaleRevisionError(
            "session base_commit is not an ancestor of the project HEAD; the checkout diverged from the session."
        )


def _baseline_entry(root: Path, base_commit: str, path: str) -> tuple[str, str] | None:
    """(mode, blob sha) of `path` in the base tree via literal pathspec
    `ls-tree`, or None when absent. Symlink/gitlink/directory baselines are
    unsupported in v1."""
    cp = _source_git(["ls-tree", "-z", base_commit, "--", path], cwd=root, check=False)
    if cp.returncode != 0:
        raise GitOperationError("the session base could not be read from the project repository.")
    entries = [line for line in cp.stdout.split(b"\0") if line.strip()]
    if not entries:
        return None
    if len(entries) > 1:
        raise GitOperationError("the base tree returned an ambiguous entry.")
    left, _, name = entries[0].partition(b"\t")
    if name != path.encode("utf-8"):
        raise GitOperationError("the base tree returned an unexpected path.")
    try:
        mode, otype, oid = left.decode("ascii").split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        raise GitOperationError("the base tree returned an unexpected entry.") from None
    if otype != "blob" or mode not in FILE_MODES:
        raise ValidationError(
            "the baseline is not a regular file (symlink/gitlink/directory changes are unsupported in v1)."
        )
    return mode, oid


def _baseline_blob(root: Path, base_commit: str, path: str, oid: str) -> bytes | None:
    """Raw baseline blob bytes, sizechecked BEFORE reading; None marks a
    baseline over the per-file limit (the caller decides whether that is a
    deletion or an unsupported oversized modification). The blob's git SHA1
    is verified against its tree entry — no filters are ever invoked."""
    object_path = f"{base_commit}:{path}"
    cp = _source_git(["cat-file", "-s", object_path], cwd=root, check=False)
    if cp.returncode != 0:
        raise GitOperationError("the baseline object could not be read from the project repository.")
    size = cp.stdout.strip()
    if not size.isdigit():
        raise GitOperationError("the baseline object size could not be read.")
    if int(size) > MAX_FILE_BYTES:
        return None
    content = _source_git(["cat-file", "blob", object_path], cwd=root).stdout
    if hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest() != oid:
        raise GitOperationError("the baseline object does not match its tree entry.")
    return content


def _source_path_is_ignored(root: Path, path: str) -> bool:
    """Plain `git check-ignore` (never --no-index): tracked paths and rule
    misses report as NOT ignored. Read-only. Its arguments are PATHNAMES
    (not pathspecs) and the command rejects --literal-pathspecs, so this
    runner keeps every other hardening flag but that one."""
    cp = _run_git(
        ["--no-optional-locks", "-c", "core.fsmonitor=", "check-ignore", "-q", "--", path],
        cwd=root, env=_git_env(), check=False,
    )
    return cp.returncode == 0


def _current_file(root: Path, path: str) -> tuple[bytes, str] | None:
    """Bounded nofollow read of the CURRENT regular file at `path`.

    The path is resolved through the workspace boundary resolver (leaf and
    ancestor symlinks, traversal and .git escapes rejected), then read with
    O_NOFOLLOW and an fstat identity check; FIFOs, devices, directories and
    oversize files are rejected. ONLY a missing path (FileNotFoundError)
    yields None (a deletion candidate); access or I/O errors are hard
    ValidationError failures — never silently treated as deletions. Like
    the binding artifact reader, ancestor components are verified without
    following; a hostile local process racing the open is out of scope for
    v1.
    """
    try:
        resolved = resolve_within_workspace(root, path, reject_symlinks=True)
    except (WorkspaceBoundaryError, OSError):
        raise ValidationError(
            "the selected path escapes the project or crosses a symbolic link (rejected)."
        ) from None
    try:
        lst = os.lstat(resolved)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValidationError(
            "the selected path could not be read (access or I/O error); it is never treated as deleted."
        ) from None
    if stat.S_ISLNK(lst.st_mode):
        raise ValidationError("the selected path must be a regular file (symbolic links are rejected).")
    flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise ValidationError("the selected path could not be opened as a regular file.") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValidationError(
                "the selected path must be a regular file (symlinks, FIFOs, directories and devices are rejected)."
            )
        if lst.st_dev and lst.st_ino and (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
            raise ValidationError("the selected file changed identity while being opened.")
        if st.st_size > MAX_FILE_BYTES:
            raise ValidationError(f"the selected file exceeds the {MAX_FILE_BYTES}-byte limit.")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(MAX_FILE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError(f"the selected file exceeds the {MAX_FILE_BYTES}-byte limit.")
    return data, ("100755" if st.st_mode & 0o111 else "100644")


def _require_text(content: bytes, *, what: str) -> None:
    if b"\x00" in content:
        raise ValidationError(f"{what} contains NUL bytes; binary file changes are unsupported in v1.")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{what} is not valid UTF-8; binary file changes are unsupported in v1.") from None


# ------------------------------------------------------------ strict models ----


@dataclass(frozen=True)
class ProposalFile:
    path: str
    before_oid: str | None
    before_mode: str | None
    after_base64: str | None
    after_mode: str | None

    @property
    def after_bytes(self) -> bytes | None:
        """Decoded (already validated) content for non-deletion entries."""
        return None if self.after_base64 is None else base64.b64decode(self.after_base64)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    task_id: str
    owner: str
    session_id: str
    base_commit: str
    context_revision: str
    context_hash: str
    files: tuple[ProposalFile, ...]
    out_of_scope_paths: tuple[str, ...] = ()  # capture-time advisory, never serialized

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROPOSAL_SCHEMA,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "base_commit": self.base_commit,
            "context_revision": self.context_revision,
            "context_hash": self.context_hash,
            "files": [
                {
                    "path": entry.path,
                    "before_oid": entry.before_oid,
                    "before_mode": entry.before_mode,
                    "after_base64": entry.after_base64,
                    "after_mode": entry.after_mode,
                }
                for entry in self.files
            ],
        }


@dataclass(frozen=True)
class ProposalReceipt:
    """Metadata-only listing entry (id, task, owner, context provenance) —
    never the proposal's code content."""

    proposal_id: str
    commit: str
    task_id: str
    owner: str
    session_id: str
    base_commit: str
    context_revision: str
    context_hash: str
    file_count: int


MAX_B64_CHARS = 4 * ((MAX_FILE_BYTES + 2) // 3)  # canonical base64 bound for MAX_FILE_BYTES


def _decode_b64(text: Any, *, what: str) -> bytes:
    if not isinstance(text, str):
        raise ValidationError(f"{what} must be a string.")
    if len(text) % 4:
        raise ValidationError(f"{what} is not canonical base64.")
    if len(text) > MAX_B64_CHARS:
        raise ValidationError(f"{what} exceeds the {MAX_FILE_BYTES}-byte limit.")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError(f"{what} is not valid base64.") from None
    if base64.b64encode(data).decode("ascii") != text:
        raise ValidationError(f"{what} is not canonical base64.")
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError(f"{what} exceeds the {MAX_FILE_BYTES}-byte limit.")
    _require_text(data, what=what)
    return data


def _require_mode(value: Any, what: str) -> str:
    if not isinstance(value, str) or value not in FILE_MODES:
        raise ValidationError(f"{what} must be one of: 100644, 100755.")
    return value


def _exact_keys(obj: Any, allowed: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError(f"{what} must be a JSON object.")
    if set(obj) - allowed:
        raise ValidationError(f"{what} has unknown fields (schema1 allows only documented fields).")
    if allowed - set(obj):
        raise ValidationError(f"{what} is missing required fields.")
    return obj


def _parse_bounded_json(raw: bytes, *, what: str, max_bytes: int) -> Any:
    """Strict bounded JSON for proposal artifacts. Like models.parse_json_bytes
    but with the proposal size bound (the 64KiB metadata limit cannot be
    reused) and a parse_float hook so no float value can ever enter a proposal."""

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValidationError(f"{what} contains duplicate object keys.")
            out[key] = value
        return out

    def _reject_constant(name: str) -> None:
        raise ValidationError(f"{what} contains the non-standard JSON constant {name}.")

    def _reject_float(text: str) -> float:
        raise ValidationError(f"{what} must not contain float values.")

    if len(raw) > max_bytes:
        raise ValidationError(f"{what} exceeds the {max_bytes}-byte limit.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{what} is not valid UTF-8.") from None
    try:
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_reject_constant, parse_float=_reject_float
        )
    except json.JSONDecodeError:
        raise ValidationError(f"{what} is not valid JSON.") from None
    except ValueError:
        # e.g. integer literals beyond the interpreter's int<->str conversion limit
        raise ValidationError(f"{what} contains an unparseable number (over-limit literals rejected).") from None
    except RecursionError:
        raise ValidationError(f"{what} is nested too deeply.") from None
    try:
        canonical_json_bytes(value)  # rejects unpaired surrogates / non-encodable content
    except UnicodeEncodeError:
        raise ValidationError(f"{what} contains unpaired Unicode surrogate characters.") from None
    except ValueError:
        # huge integers can parse but cannot re-serialize; never leak raw ValueError
        raise ValidationError(f"{what} contains an unrepresentable integer (over the serialization limit).") from None
    except RecursionError:
        raise ValidationError(f"{what} is nested too deeply.") from None
    return value


def parse_proposal_dict(obj: Any) -> Proposal:
    """Strictly validate a schema1 proposal dict (unknown keys, wrong types,
    duplicate/unsorted file paths, bad base64/modes and every limit are
    rejected)."""
    _exact_keys(obj, _PROPOSAL_KEYS, "proposal artifact")
    if type(obj["schema"]) is not int or obj["schema"] != PROPOSAL_SCHEMA:
        raise ValidationError("proposal artifact schema must be exactly 1.")
    raw_files = obj["files"]
    if not isinstance(raw_files, list):
        raise ValidationError("proposal files must be a list.")
    if not 1 <= len(raw_files) <= MAX_FILES:
        raise ValidationError(f"a proposal must contain between 1 and {MAX_FILES} changed files.")
    files: list[ProposalFile] = []
    previous = ""
    for index, raw in enumerate(raw_files):
        entry = _exact_keys(raw, _FILE_KEYS, f"proposal file entry {index}")
        path = proposal_file_path(entry["path"])
        if path <= previous:
            raise ValidationError("proposal files must be strictly sorted by path without duplicates.")
        previous = path
        before_oid, before_mode = entry["before_oid"], entry["before_mode"]
        after_base64, after_mode = entry["after_base64"], entry["after_mode"]
        if (before_oid is None) != (before_mode is None):
            raise ValidationError("before_oid and before_mode must be both present or both absent.")
        if (after_base64 is None) != (after_mode is None):
            raise ValidationError("after_base64 and after_mode must be both present or both absent.")
        if before_oid is None and after_base64 is None:
            raise ValidationError("a file entry must be an addition, a modification or a deletion.")
        if before_oid is not None:
            before_oid = sha_hex(before_oid, "file before_oid")
            before_mode = _require_mode(before_mode, "file before_mode")
        if after_base64 is not None:
            _decode_b64(after_base64, what=f"file content of {path}")
            after_mode = _require_mode(after_mode, "file after_mode")
        files.append(ProposalFile(path, before_oid, before_mode, after_base64, after_mode))
    return Proposal(
        safe_id(obj["proposal_id"], "proposal_id"),
        safe_id(obj["task_id"], "task_id"),
        safe_id(obj["owner"], "owner"),
        safe_id(obj["session_id"], "session_id"),
        sha_hex(obj["base_commit"], "base_commit"),
        sha_hex(obj["context_revision"], "context_revision"),
        _context_hash(obj["context_hash"]),
        tuple(files),
    )


def _context_hash(value: Any) -> str:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise ValidationError("context_hash must be a full 64-character lowercase hex sha256.")
    return value


def parse_proposal_bytes(raw: bytes) -> Proposal:
    """Parse bounded UTF-8 proposal artifact bytes through the strict pipeline."""
    return parse_proposal_dict(_parse_bounded_json(
        raw, what="proposal artifact", max_bytes=MAX_PROPOSAL_JSON_BYTES,
    ))


def proposal_bytes(proposal: Proposal) -> bytes:
    """Canonical wire bytes of a proposal (advisory fields are not serialized)."""
    return canonical_json_bytes(proposal.to_dict())


# --------------------------------------------------------- binding checks ----


def _require_expected_revision(expected: str, head: str) -> None:
    """`expected_revision` is a compare-and-swap token: it must equal the
    live hub head exactly (same contract as the metadata publication API).
    An OLD binding stays usable without a rebind while the shared context
    and the selected task identity are unchanged — other tasks and status
    changes may move the head, but the token itself is never exempted."""
    if expected != head:
        raise StaleRevisionError(
            f"expected_revision is no longer current (the hub is at {head[:12]}); sync and retry."
        )


def _validate_binding(
    store: GitStore, root: Path, head: str, current: SessionState, task_id: str, expected: str,
    supplied: SharedSnapshot | None = None,
) -> SharedSnapshot:
    """The binding artifact must be a VALID authoritative snapshot of this
    session: wholesale state equality against the published history (not
    just the integrity checksum), matching session/base, the CURRENT shared
    context, and a task whose owner/goal/scopes were never superseded.
    Returns the validated binding; its revision/hash become the proposal's
    recorded provenance (what the agent actually saw)."""
    if supplied is None:
        binding = load_project_snapshot(root)
    else:
        if not isinstance(supplied, SharedSnapshot):
            raise ValidationError("binding must be a validated shared snapshot.")
        binding = parse_snapshot_dict(supplied.to_dict())
    if binding is None:
        raise ValidationError(
            "no valid bound shared-context artifact was found in the project; bind the task first."
        )
    if binding.task_id != task_id:
        raise ValidationError("the bound artifact is for a different task; capture only the bound task's changes.")
    _require_expected_revision(expected, head)
    if not store.is_ancestor(binding.revision, head):
        raise ValidationError("the bound artifact revision is not part of this session's published history.")
    if binding.state.to_dict() != store._read_state(binding.revision).to_dict():
        raise ValidationError(
            "the bound artifact does not match the published session state; it is tampered or foreign."
        )
    if binding.state.session_id != current.session_id or binding.state.base_commit != current.base_commit:
        raise ValidationError("the bound artifact belongs to a different session or base commit.")
    if binding.context_hash != current.context_hash:
        raise StaleRevisionError("the shared context changed after binding; rebind the task before proposing.")
    if task_id not in current.tasks:
        raise ValidationError("the bound task no longer exists in the session.")
    bound, live = binding.state.tasks[task_id], current.tasks[task_id]
    if (bound.owner, bound.goal, bound.scopes) != (live.owner, live.goal, live.scopes):
        raise StaleRevisionError(
            "the bound task was superseded (owner, goal or scopes changed); rebind before proposing."
        )
    return binding


# ---------------------------------------------------------------- capture ----


def _in_scope(path: str, scopes: tuple[str, ...]) -> bool:
    return any(s == path or (s.endswith("/") and path.startswith(s)) for s in scopes)


def _capture_entries(
    root: Path, base_commit: str, scopes: tuple[str, ...], paths: list[str]
) -> tuple[list[ProposalFile], list[str]]:
    """Diff each explicitly selected path against the base tree. Unchanged
    selections are omitted; git-ignored current paths without a tracked
    baseline are rejected (deps/ignored files are never auto-uploaded)."""
    files: list[ProposalFile] = []
    out_of_scope: list[str] = []
    for path in sorted(paths):
        if not _in_scope(path, scopes):
            out_of_scope.append(path)
        baseline = _baseline_entry(root, base_commit, path)
        if baseline is None and _source_path_is_ignored(root, path):
            raise ValidationError(
                "the selected path is git-ignored with no committed baseline; ignored files are not captured."
            )
        current = _current_file(root, path)
        if current is None:
            if baseline is None:
                continue  # absent before and now: nothing to capture
            files.append(ProposalFile(path, baseline[1], baseline[0], None, None))
            continue
        content, mode = current
        _require_text(content, what=f"the selected file {path}")
        if baseline is None:
            files.append(ProposalFile(path, None, None, _encode_b64(content), mode))
            continue
        before_oid, before_mode = baseline[1], baseline[0]
        before_bytes = _baseline_blob(root, base_commit, path, before_oid)
        if before_bytes is None:
            raise ValidationError(
                f"the baseline content of {path} exceeds the {MAX_FILE_BYTES}-byte limit; "
                "oversized modifications are unsupported in v1."
            )
        _require_text(before_bytes, what=f"the baseline content of {path}")
        if before_bytes == content and before_mode == mode:
            continue  # unchanged selection
        files.append(ProposalFile(path, before_oid, before_mode, _encode_b64(content), mode))
    return files, out_of_scope


def _encode_b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def capture_proposal_from_session_state(
    project_root: Path | str,
    proposal_id: str,
    task_id: str,
    paths: list[str] | tuple[str, ...],
    *,
    session_state: SessionState,
    context_revision: str,
) -> Proposal:
    """Capture selected working-tree changes using already-validated live
    session state, without requiring a peer-side GitStore or metadata clone.

    The caller is responsible for obtaining ``session_state`` and its
    ``context_revision`` from the authenticated live metadata channel. This
    function validates their internal identity and performs only read-only
    source-repository inspection; it never invents or verifies history in a
    local collaboration store. The session base must be an ancestor of the
    source HEAD, matching the cumulative capture semantics of
    :func:`capture_proposal`.
    """
    if not isinstance(session_state, SessionState):
        raise ValidationError("capture requires validated live session state.")
    session_state = parse_state_dict(session_state.to_dict())
    identifier = safe_id(proposal_id, "proposal_id")
    selected_task = safe_id(task_id, "task_id")
    revision = sha_hex(context_revision, "context_revision")
    if selected_task not in session_state.tasks:
        raise ValidationError("the selected task does not exist in the live session.")
    if not session_state.tasks[selected_task].owner:
        raise ValidationError("the selected task has no valid owner.")
    if not session_state.context_hash:
        raise ValidationError("the live shared context is invalid.")
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValidationError("at least one explicitly selected path is required.")
    cleaned: list[str] = []
    for raw in paths:
        path = proposal_file_path(raw)
        if path not in cleaned:
            cleaned.append(path)
    if len(cleaned) > MAX_FILES:
        raise ValidationError(f"at most {MAX_FILES} changed files are allowed per proposal.")
    root = _require_source_toplevel(project_root)
    _source_head(root)
    _require_base_ancestor(root, session_state.base_commit)
    task = session_state.tasks[selected_task]
    files, out_of_scope = _capture_entries(root, session_state.base_commit, task.scopes, cleaned)
    if not files:
        raise ValidationError(
            "the proposal is empty: none of the selected paths changed relative to the session base."
        )
    proposal = Proposal(
        proposal_id=identifier, task_id=selected_task, owner=task.owner,
        session_id=session_state.session_id, base_commit=session_state.base_commit,
        context_revision=revision, context_hash=session_state.context_hash,
        files=tuple(files), out_of_scope_paths=tuple(out_of_scope),
    )
    parse_proposal_bytes(proposal_bytes(proposal))
    return proposal


def capture_proposal(
    store: GitStore,
    project_root: Path | str,
    proposal_id: str,
    task_id: str,
    paths: list[str] | tuple[str, ...],
    expected_revision: str,
    *, binding: SharedSnapshot | None = None,
) -> Proposal:
    """Capture the user-selected cumulative changes of one source checkout
    against the session base, tied to a valid authoritative bound context.

    Read-only for the project: only committed baseline objects (ls-tree/
    cat-file with size checks) and explicitly selected raw disk bytes are
    read. Returns the immutable Proposal (out-of-scope selections reported
    in `out_of_scope_paths`); publication is a separate atomic step."""
    identifier = safe_id(proposal_id, "proposal_id")
    selected_task = safe_id(task_id, "task_id")
    expected = sha_hex(expected_revision, "expected_revision")
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValidationError("at least one explicitly selected path is required.")
    cleaned: list[str] = []
    for raw in paths:
        path = proposal_file_path(raw)
        if path not in cleaned:
            cleaned.append(path)
    if len(cleaned) > MAX_FILES:
        raise ValidationError(f"at most {MAX_FILES} changed files are allowed per proposal.")
    root = _require_source_toplevel(project_root)
    _source_head(root)  # a committed HEAD is required for the cumulative base check
    # even direct API callers cannot point a store/hub into the source tree
    GitStore.ensure_outside_project(store.store_path, root, what="client store")
    GitStore.ensure_outside_project(Path(store.remote), root, what="collaboration hub")
    head, current = store.fetch_state()
    binding = _validate_binding(store, root, head, current, selected_task, expected, binding)
    _require_base_ancestor(root, current.base_commit)
    files, out_of_scope = _capture_entries(
        root, current.base_commit, current.tasks[selected_task].scopes, cleaned
    )
    if not files:
        raise ValidationError(
            "the proposal is empty: none of the selected paths changed relative to the session base."
        )
    proposal = Proposal(
        proposal_id=identifier,
        task_id=selected_task,
        owner=current.tasks[selected_task].owner,
        session_id=current.session_id,
        base_commit=current.base_commit,
        context_revision=binding.revision,  # what the agent/source actually saw
        context_hash=binding.context_hash,
        files=tuple(files),
        out_of_scope_paths=tuple(out_of_scope),
    )
    parse_proposal_bytes(proposal_bytes(proposal))  # re-validate through the strict parser
    return proposal


# -------------------------------------------------------------- publication ----


def _validate_recorded_provenance(
    store: GitStore, proposal: Proposal, head: str, current: SessionState, *,
    require_live_task_match: bool, error: type[CollabError] = ValidationError,
) -> None:
    """Recorded provenance must match the real published history: the
    recorded revision is an ancestor whose historical state reproduces the
    recorded context hash and contains the recorded task with the recorded
    owner. Publication additionally requires the historical owner/goal/scopes
    to still equal the live task's (superseded bindings can never be
    published); reads and listings stay historical — they tolerate later
    shared-context changes (the candidate layer rejects stale context) and
    never demand the historical context to equal the live one. Failures are
    raised as `error` (ValidationError for caller-built proposals,
    GitOperationError for corrupt hub content on read/list)."""
    if not store.is_ancestor(proposal.context_revision, head):
        raise error("the recorded context_revision is not part of this session's published history.")
    historic = store._read_state(proposal.context_revision)
    if historic.context.content_hash != proposal.context_hash:
        raise error("the recorded context_hash does not match the historical state at the recorded revision.")
    historic_task = historic.tasks.get(proposal.task_id)
    if historic_task is None or historic_task.owner != proposal.owner:
        raise error("the recorded task/owner does not match the historical session state.")
    if require_live_task_match:
        live_task = current.tasks.get(proposal.task_id)
        if live_task is None:
            raise ValidationError("the proposal's task no longer exists in the session.")
        if (historic_task.owner, historic_task.goal, historic_task.scopes) != (
            live_task.owner, live_task.goal, live_task.scopes
        ):
            raise StaleRevisionError(
                "the selected task was superseded (owner, goal or scopes changed); re-capture."
            )


def publish_proposal(store: GitStore, proposal: Proposal, *, expected_revision: str) -> tuple[str, str]:
    """Publish one captured proposal as a private immutable hub ref.

    The proposal root commit (tree = only proposal.json, no parents) is
    pushed together with a CHILD metadata session commit (state unchanged,
    message naming the proposal id/hash) in ONE atomic fast-forward push of
    literal SHAs: a concurrent session move rejects the whole push, so no
    orphan proposal ref can ever exist. On any failure only our own local
    mirror ref is CAS-restored; the hub stays authoritative. Duplicate ids
    are rejected before the push and a root ref can never be overwritten."""
    if not isinstance(proposal, Proposal):
        raise ValidationError("publish_proposal requires a captured Proposal.")
    expected = sha_hex(expected_revision, "expected_revision")
    validated = parse_proposal_dict(proposal.to_dict())  # hand-built dataclasses never ship
    payload = proposal_bytes(validated)
    if len(payload) > MAX_PROPOSAL_JSON_BYTES:
        raise ValidationError(f"proposal artifact exceeds the {MAX_PROPOSAL_JSON_BYTES}-byte limit.")
    head, current = store.fetch_state()
    _require_expected_revision(expected, head)
    if (validated.session_id, validated.base_commit) != (current.session_id, current.base_commit):
        raise ValidationError("the proposal belongs to a different session or base commit.")
    if validated.context_hash != current.context_hash:
        raise StaleRevisionError("the shared context changed since the proposal was captured; re-capture.")
    _validate_recorded_provenance(store, validated, head, current, require_live_task_match=True)
    if store.remote_proposal_head(validated.proposal_id) is not None:
        raise ValidationError("a proposal with this id already exists on the hub; choose a new id.")
    message = f"imece-collab: proposal {validated.proposal_id} {validated.context_hash}"
    child = store._commit(current, parent=head, message=message)
    root = store._commit_payload(payload, path=PROPOSAL_STATE_PATH, parent=None, message=message)
    if not store._cas_advance(child):
        raise StaleRevisionError("the local store moved concurrently; re-sync and retry.")
    try:
        store._atomic_push(
            [(child, SESSION_BRANCH), (root, store._proposal_ref(validated.proposal_id))],
            stale_message="publication rejected: the session moved on the hub; sync and retry.",
        )
    except CollabError:
        store._cas_ref(head, child)  # CAS-restore OUR mirror value only
        raise
    return child, root


# ------------------------------------------------------------------ reads ----


def list_proposals(store: GitStore) -> list[ProposalReceipt]:
    """Metadata receipts of every published proposal (id, task, owner,
    session/base/context provenance). The OUTPUT is metadata-only — no file
    contents are ever returned — but the TRANSFER is not: each artifact is
    fully fetched and its recorded provenance validated against the session
    history (old proposals stay readable after later shared-context
    changes; only the recorded provenance must match the history)."""
    head, current = store.fetch_state()
    cp = _run_git(
        ["ls-remote", store.remote, PROPOSAL_REF_PREFIX + "*"],
        cwd=store.store_path, check=False,
    )
    if cp.returncode != 0:
        raise GitOperationError("could not reach the collaboration hub.")
    refs: dict[str, str] = {}
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        sha, _, refname = line.strip().partition("\t")
        if not refname.startswith(PROPOSAL_REF_PREFIX):
            continue  # foreign refs are ignored, never trusted
        try:
            identifier = safe_id(refname[len(PROPOSAL_REF_PREFIX):], "proposal id")
            sha_hex(sha.strip(), "proposal revision")
        except ValidationError:
            continue
        if identifier in refs:
            raise GitOperationError("the hub returned duplicate proposal refs.")
        refs[identifier] = sha.strip()
    if len(refs) > MAX_PROPOSALS:
        raise GitOperationError(f"too many proposals are published on the hub (over {MAX_PROPOSALS}).")
    receipts: list[ProposalReceipt] = []
    for identifier, _sha in sorted(refs.items()):
        commit, payload = store.fetch_proposal_ref(identifier)
        try:
            parsed = parse_proposal_bytes(payload)
        except ValidationError:
            raise GitOperationError("a published proposal artifact is corrupt or oversized.") from None
        if parsed.proposal_id != identifier:
            raise GitOperationError("a published artifact does not match its proposal ref id.")
        if (parsed.session_id, parsed.base_commit) != (current.session_id, current.base_commit):
            raise GitOperationError("a published proposal does not belong to this session.")
        _validate_recorded_provenance(
            store, parsed, head, current, require_live_task_match=False, error=GitOperationError)
        receipts.append(ProposalReceipt(
            proposal_id=parsed.proposal_id,
            commit=commit,
            task_id=parsed.task_id,
            owner=parsed.owner,
            session_id=parsed.session_id,
            base_commit=parsed.base_commit,
            context_revision=parsed.context_revision,
            context_hash=parsed.context_hash,
            file_count=len(parsed.files),
        ))
    return receipts


def read_proposal(store: GitStore, proposal_id: str) -> Proposal:
    """Fetch and strictly parse one published proposal (explicit ref fetch
    into a unique temporary ref, bounded sizecheck-before-read). The
    embedded proposal_id must equal the requested immutable ref id, the
    session must match, and the recorded provenance must reproduce the real
    published history (historical tolerance: later shared-context changes
    do not invalidate old proposals)."""
    _commit_id, payload = store.fetch_proposal_ref(proposal_id)
    proposal = parse_proposal_bytes(payload)
    if proposal.proposal_id != safe_id(proposal_id, "proposal_id"):
        raise GitOperationError("the published artifact does not match its proposal ref id.")
    head, current = store.fetch_state()
    if (proposal.session_id, proposal.base_commit) != (current.session_id, current.base_commit):
        raise GitOperationError("the proposal does not belong to this session.")
    _validate_recorded_provenance(
        store, proposal, head, current, require_live_task_match=False, error=GitOperationError)
    return proposal
