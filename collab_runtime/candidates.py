"""collab_runtime.candidates — bounded CANDIDATE assembly (v1).

One explicitly selected set of published proposals (1..MAX_SELECTED, ids
distinct) is combined with the source checkout's committed BASELINE at the
session's `base_commit` into a NEW dedicated materialized candidate directory.
This slice is the "combined candidate" step: it is an EXPLICIT, user-directed
operation — there is no implicit all-proposals mode, no code apply/publish,
and the output is never written back to the source checkout.

Safety properties (verified in tests):
- Preflight BEFORE creation: every proposal is fetched and strictly parsed,
  its recorded provenance is validated against the real published history
  (historical tolerance for the shared context, live owner/goal/scopes for
  each selected task), the bound snapshot in the checkout must be a VALID
  snapshot of the CURRENT session (integrity-checked, matching session/base
  and the current context), the session base must exist in the source's
  history, every proposal `before_oid`/`before_mode` must match the real
  committed baseline (a `before_oid: null` addition requires the baseline
  path to be ABSENT), and the snapshot must equal the CURRENT session
  context. Conflicts and every validation failure abort BEFORE `os.mkdir`:
  no partial output, no cleanup of anything pre-existing.
- Selection discipline: proposal ids must be distinct; TWO cumulative
  proposals for the SAME task are rejected (cumulative deletions/additions
  could be combined ambiguously — the user must choose the latest); at most
  MAX_SELECTED_PROPOSALS are combined per candidate.
- Baseline v1 reads ONLY committed git objects of the session base
  (`ls-tree -r -l -z`, then sizechecked `cat-file` of validated hex SHAs;
  each blob sha1 is re-verified against its tree entry and blob reads are
  deduplicated). It is hardened read-only plumbing (`--no-optional-locks`,
  `--literal-pathspecs`, `core.fsmonitor=`, disabled hooks, scrubbed env):
  no source index writes, no clean/process filters, no source checkout. The
  baseline is regular 100644/100755 files ONLY — a committed symbolic link
  or gitlink is an explicit safe error raised BEFORE output creation.
  Binary baseline files are allowed unchanged; proposal changes are UTF-8
  text only. Dirty/untracked/ignored source state is intentionally excluded
  — the candidate contains the committed baseline plus explicitly published
  proposal entries — and dirty source state or later commits never prevent
  assembly from the common base. Tree paths must decode as UTF-8 without
  control characters, pass the workspace path helpers (.git/traversal) AND
  stay canonical: normalization must never silently rename a baseline entry
  (backslash filenames, `a//b`, `./a` are rejected), and platform case
  collisions are rejected.
- Three-way text merge: multiple changes to the same baseline text file run
  real `git merge-file -p` over temp RAW bytes (argv list, no shell, store
  cwd, hooks disabled, scrubbed env; tempdir removed in all cases). Disjoint
  hunks merge; overlapping hunks become structured conflict paths.
  Conflicting add/add, delete/modify and unresolvable mode combinations are
  conflicts too. Identical edits are deduplicated; there is never
  last-writer-wins. On conflict NOTHING is materialized and verification is
  not_run (CLI exit 8).
- Final set validation before any filesystem write: directory/file prefix
  conflicts, platform case collisions (file vs file, file vs dir, dir vs
  dir) and the merged-output limits are enforced.
- Output placement: the candidate output must be a NON-EXISTENT path OUTSIDE
  the source checkout, the hub and the client store, whose ancestor chain
  contains no symbolic link and no non-directory, and whose parent must
  already exist (no nested creation in v1). A `.git` component anywhere in
  the path is rejected lexically (git metadata of any repository is never a
  candidate output). `os.mkdir(output, 0o700)` is exclusive and PRIVATE
  (generated code is never umask-readable), so a race-created path fails
  cleanly. User index/HEAD/branches/config stay untouched; no source
  history is copied, no extra ignored/env/credential snapshots are carried.
  Materialization writes raw bytes via exclusive O_CREAT|O_EXCL|O_NOFOLLOW
  (never a symlink) with executable modes preserved through workspace path
  helpers. Construction errors remove ONLY the directory created by this
  operation; once the output exists, verification failures/errors NEVER
  delete it. A CAS re-check of `expected_revision` happens immediately
  before output creation, so a session that advanced during preflight/
  merge is rejected stale with no output; the receipt is point-in-time.
- Verification (`--verify` is explicit authorization to EXECUTE the
  project's checks inside the candidate dir — not a process or filesystem
  sandbox claim; collaborators and source are trusted; the API demands an
  exact bool): detect_verification_plan from
  pipeline_runtime.verification_detect decides the plan; no option or no
  detected plan => not_run (never PASS); bad plan config or process failure
  => error. Candidate content is fingerprinted (relative path + mode +
  sha256, canonical-JSON records) before and after with the SAME rules:
  every ORIGINAL materialized path is always included — even cache-looking
  ones — and only NEWLY generated standard cache artifacts are excluded
  (directory names `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `.hypothesis`, `.eggs` at any depth WITHOUT original
  files; files ending `.pyc` that are not original). Traversal is bounded
  (an entry budget covering originals plus directory overhead plus bounded
  new entries, per-file/total byte limits, a depth bound, globally stopped
  on exhaustion; bounded enumeration before sorting) and never follows
  root/dir/leaf symlinks; regular files are opened O_NOFOLLOW|O_NONBLOCK
  and re-verified (regular + identity) before reads. The fingerprint
  returns an explicit COMPLETE flag: an incomplete BEFORE fingerprint
  errors WITHOUT executing any check, and an incomplete AFTER fingerprint
  (or any persistent change) marks the candidate NOT verified (status
  invalidated) even when both digests are equal. Fingerprints detect
  PERSISTENT changes only — transient edits restored in between snapshots
  are not detected — and no complete mutation monitoring is claimed.
  Receipts carry clearly-labelled verification.fingerprint_before/
  fingerprint_after and fingerprint_complete alongside the assembled
  content_fingerprint.
- The receipt carries session/base/context provenance, proposal ids, the
  candidate dir, a content fingerprint, conflict paths (always empty on
  success) and verification summaries (check ids, statuses, exit codes,
  timeouts) — never file contents, command stdout/stderr or any
  project-content log. Produced code stays user-inspectable local code;
  nothing is uploaded from the candidate.

Limits (honest, reported in the receipt notes): <=MAX_BASELINE_FILES
committed baseline entries, <=MAX_BASELINE_TOTAL total baseline bytes,
<=MAX_BASELINE_FILE per entry; proposal entries are already bounded by the
proposal artifact limits (<=MAX_FILES files, <=MAX_FILE_BYTES each); the
merged output re-checks the same bounds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collab_runtime.errors import (
    CollabError,
    GitOperationError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.context import SharedSnapshot, load_project_snapshot, parse_snapshot_dict
from collab_runtime.models import SessionState, safe_id, sha_hex
from collab_runtime.proposals import (
    FILE_MODES,
    Proposal,
    _require_base_ancestor,
    _require_source_toplevel,
    _source_head,
    _source_git,
    _validate_recorded_provenance,
    read_proposal,
)
from collab_runtime.store import GitStore, _run_git
from workspace.base import normalize_workspace_relative_path, resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError

MAX_SELECTED_PROPOSALS = 16
MAX_BASELINE_FILES = 5000
MAX_BASELINE_FILE = 8 * 1024 * 1024
MAX_BASELINE_TOTAL = 64 * 1024 * 1024
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Explicit fingerprint-excluded runtime cache artifacts (documented rules,
# never arbitrary source files).
_FP_IGNORE_DIR_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".eggs",
})


class CandidateConflictError(CollabError):
    """The selected proposals conflict; nothing was materialized."""

    exit_code = 8

    def __init__(self, conflict_paths: list[str]) -> None:
        super().__init__(
            "the selected proposals conflict on one or more paths; "
            "no candidate directory was created and verification was not run."
        )
        self.conflict_paths = sorted(conflict_paths)


# ------------------------------------------------------- output placement ----


def _validate_output_path(
    output_raw: Any, *, project: Path, hub: Path, store_path: Path
) -> Path:
    """The candidate output must be a NON-EXISTENT dedicated directory path
    outside the source checkout, hub and store, with a safe ancestor chain:
    no symbolic link and no non-directory between it and the root, and its
    parent must already exist. A `.git` component anywhere in the path is
    rejected LEXICALLY (git metadata of ANY repository is never a candidate
    output). Validated BEFORE anything is created; returns the resolved
    absolute safe output path."""
    if isinstance(output_raw, Path):
        output_raw = str(output_raw)
    if not isinstance(output_raw, str) or not output_raw.strip():
        raise ValidationError("the candidate output path must be a non-empty string.")
    if "\x00" in output_raw:
        raise ValidationError("the candidate output path must not contain NUL bytes.")
    if any(part.casefold() == ".git" for part in output_raw.replace("\\", "/").split("/")):
        raise ValidationError(
            "the candidate output path must not contain a .git component "
            "(git metadata is never a candidate output)."
        )
    target = Path(output_raw)
    if target.is_symlink() or target.exists():
        raise ValidationError(
            "the candidate output path already exists (file, directory or symlink); "
            "refusing to touch it — pick a new empty path."
        )
    for parent in target.parents:
        if parent.is_symlink():
            raise ValidationError(
                "the candidate output path traverses a symbolic link ancestor (unsafe); pick a real directory."
            )
        if parent.exists() and not parent.is_dir():
            raise ValidationError(
                "the candidate output path must live under existing directories "
                "(a non-directory ancestor blocks creation)."
            )
    if not target.parent.exists():
        raise ValidationError(
            "the candidate output's parent directory must already exist (no nested creation in v1)."
        )
    resolved = target.resolve(strict=False)
    for root, what in ((project, "source checkout"), (hub, "hub"), (store_path, "client store")):
        if resolved == root or root in resolved.parents:
            raise ValidationError(f"the candidate output must live outside the {what}.")
    return resolved


# --------------------------------------------------------------- baseline ----


@dataclass(frozen=True)
class _BaselineEntry:
    path: str
    mode: str
    oid: str
    size: int


def _decode_tree_path(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(
            "the committed baseline contains a path that is not valid UTF-8; baseline v1 rejects it."
        ) from None
    if any(ord(ch) < 32 or ord(ch) == 0x7F for ch in text):
        raise ValidationError(
            "the committed baseline contains a path with control characters; baseline v1 rejects it."
        )
    try:
        normalized = normalize_workspace_relative_path(text)
    except WorkspaceBoundaryError:
        raise ValidationError(
            "the committed baseline contains an unsafe path (.git/traversal/absolute); "
            "baseline v1 rejects it."
        ) from None
    if normalized != text:
        # normalization must never silently RENAME a baseline entry (a POSIX
        # filename with a backslash, 'a//b' or './a' would materialize under a
        # different path than it is tracked under)
        raise ValidationError(
            "the committed baseline contains a non-canonical path "
            "(separators/dot components are not canonical); baseline v1 rejects it."
        )
    return normalized


def _read_baseline_listing(toplevel: Path, base_commit: str) -> list[_BaselineEntry]:
    """Bounded, read-only listing of the committed base tree: regular files
    only, sizes validated against limits BEFORE any blob read, tree paths
    normalized and case-collisions rejected. Every non-regular entry
    (symbolic link, gitlink) is an explicit safe v1 rejection raised BEFORE
    output creation."""
    cp = _source_git(["ls-tree", "-r", "-l", "-z", base_commit], cwd=toplevel, check=False)
    if cp.returncode != 0:
        raise ValidationError("the session base tree could not be read from the source repository.")
    entries: list[_BaselineEntry] = []
    seen: dict[str, str] = {}
    casefolded: dict[str, str] = {}
    total = 0
    for line in cp.stdout.split(b"\0"):
        if not line.strip():
            continue
        left, _, raw_path = line.partition(b"\t")
        try:
            parts = left.decode("ascii").split(" ")
            mode, otype, oid, size_text = parts[0], parts[1], parts[2], parts[-1]
        except (UnicodeDecodeError, IndexError, ValueError):
            raise ValidationError("the base tree returned an unexpected entry.") from None
        if otype != "blob" or mode not in FILE_MODES:
            raise ValidationError(
                "the committed baseline contains a symbolic link, gitlink or non-regular entry; "
                "baseline v1 supports regular files (100644/100755) only — "
                "candidate rejected before output creation."
            )
        try:
            oid = sha_hex(oid, "baseline blob sha")
        except ValidationError:
            raise ValidationError("the base tree returned an unexpected object id.") from None
        if not size_text.isdigit():
            raise ValidationError("the base tree returned an unexpected size.")
        size = int(size_text)
        if size > MAX_BASELINE_FILE:
            raise ValidationError(
                f"the committed baseline exceeds the {MAX_BASELINE_FILE}-byte per-file limit; "
                "candidate rejected."
            )
        path = _decode_tree_path(raw_path)
        if path in seen:
            raise ValidationError("the base tree contains duplicate normalized paths.")
        seen[path] = oid
        key = path.casefold()
        if key in casefolded and casefolded[key] != path:
            raise ValidationError(
                "the committed baseline has a platform case collision (paths that differ "
                "only in case); candidate rejected before output creation."
            )
        casefolded[key] = path
        entries.append(_BaselineEntry(path, mode, oid, size))
        total += size
        if len(entries) > MAX_BASELINE_FILES or total > MAX_BASELINE_TOTAL:
            raise ValidationError(
                f"the committed baseline exceeds the {MAX_BASELINE_FILES}-file / "
                f"{MAX_BASELINE_TOTAL}-byte limit; candidate rejected."
            )
    return entries


def _read_baseline_blobs(
    toplevel: Path, entries: list[_BaselineEntry]
) -> dict[str, tuple[str, bytes]]:
    """(mode, raw bytes) per baseline path, read by validated hex SHA only,
    sizechecked against the tree entry before each read, sha1 re-verified,
    blob reads deduplicated."""
    blobs: dict[str, bytes] = {}
    out: dict[str, tuple[str, bytes]] = {}
    total = 0
    for entry in entries:
        if entry.oid not in blobs:
            cp = _source_git(["cat-file", "-s", entry.oid], cwd=toplevel, check=False)
            if cp.returncode != 0 or not cp.stdout.strip().isdigit():
                raise ValidationError("the baseline object could not be sizechecked.")
            if int(cp.stdout.strip()) != entry.size:
                raise ValidationError("the baseline object size does not match its tree entry.")
            raw = _source_git(["cat-file", "blob", entry.oid], cwd=toplevel).stdout
            if hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() != entry.oid:
                raise ValidationError("the baseline object does not match its tree entry.")
            blobs[entry.oid] = raw
        content = blobs[entry.oid]
        out[entry.path] = (entry.mode, content)
        total += len(content)
        if total > MAX_BASELINE_TOTAL:
            raise ValidationError("the committed baseline exceeds the total byte limit.")
    return out


def _is_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _verify_before_identities(
    listing: dict[str, _BaselineEntry], proposals: list[Proposal]
) -> None:
    """Every proposal before identity must match the real committed baseline:
    modifications/deletions need the exact (oid, mode); additions require the
    baseline path to be ABSENT. Corrupted before identities abort preflight
    (raised BEFORE output creation)."""
    for proposal in proposals:
        for entry in proposal.files:
            actual = listing.get(entry.path)
            if entry.before_oid is None:
                if actual is not None:
                    raise ValidationError(
                        f"the proposal claims an addition for {entry.path}, "
                        "but the committed baseline contains it."
                    )
                continue
            if actual is None or actual.oid != entry.before_oid or actual.mode != entry.before_mode:
                raise ValidationError(
                    f"the proposal's before identity for {entry.path} does not match "
                    "the committed baseline."
                )


# ------------------------------------------------------------------ merge ----


def _three_way_merge(
    store: GitStore, base: bytes, ours: bytes, theirs: bytes
) -> tuple[bytes, bool]:
    """Real `git merge-file -p` over temp RAW bytes (argv list, no shell,
    hooks disabled, scrubbed env; never a source or store write). Returns
    (merged bytes, conflict flag). Exit 1..127 is the conflict count (a CODE
    conflict); any other nonzero/abnormal exit is an operational failure
    raised as a typed GitOperationError — never mislabelled as a conflict."""
    tmpdir = Path(tempfile.mkdtemp(prefix="imece-merge-"))
    try:
        base_p, ours_p, theirs_p = tmpdir / "base", tmpdir / "ours", tmpdir / "theirs"
        base_p.write_bytes(base)
        ours_p.write_bytes(ours)
        theirs_p.write_bytes(theirs)
        cp = _run_git(
            # git merge-file order: <current> <base> <other> — ours is the
            # current text, the committed baseline is the merge base.
            ["merge-file", "-p", str(ours_p), str(base_p), str(theirs_p)],
            cwd=store.store_path, check=False,
        )
        if cp.returncode < 0 or cp.returncode >= 128:
            raise GitOperationError(
                "the three-way merge operation failed; no candidate directory was created."
            )
        if cp.returncode != 0:
            return b"", True
        return cp.stdout, False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _resolve_modes(
    baseline_mode: str | None, first_mode: str, next_mode: str
) -> tuple[str, bool]:
    if next_mode == first_mode:
        return first_mode, False
    if baseline_mode is not None and next_mode == baseline_mode:
        return first_mode, False
    if baseline_mode is not None and first_mode == baseline_mode:
        return next_mode, False
    return first_mode, True


def _merge_path_changes(
    store: GitStore, path: str, baseline: tuple[str, bytes] | None,
    changes: list[tuple[str, Any]],
) -> tuple[str, bytes] | None:
    """Deterministically merge the (proposal-id-sorted) changes of ONE path.
    Returns the materialized (mode, bytes) or None for a deletion.
    delete/modify, conflicting add/add and unresolvable modes are conflicts;
    impossible identity combinations were already rejected by the verified
    before identities (an addition and a modification/deletion of one path
    cannot both match the same baseline)."""
    deletions = [entry for _pid, entry in changes if entry.after_base64 is None]
    afters = [(pid, entry) for pid, entry in changes if entry.after_base64 is not None]
    if deletions and afters:
        raise CandidateConflictError([path])  # delete/modify
    if not afters:
        return None  # identical deletions only
    baseline_mode, baseline_bytes = baseline if baseline is not None else (None, b"")
    unique: list[tuple[bytes, str]] = []
    for _pid, entry in afters:  # identical edits are deduplicated, order stays id-sorted
        key = (entry.after_bytes, entry.after_mode)
        if key not in unique:
            unique.append(key)
    if len(unique) == 1:
        return unique[0][1], unique[0][0]
    if baseline is None:
        # Conflicting add/add with NO merge base: divergent additions conflict
        # even when one added file is EMPTY (an empty-base merge-file run
        # would otherwise silently accept a divergent addition).
        raise CandidateConflictError([path])
    if not _is_text(baseline_bytes):
        raise ValidationError(
            f"the committed baseline content of {path} is binary; "
            "text merging of a binary baseline is unsupported in v1."
        )
    ours = unique[0][0]
    resolved_mode = unique[0][1]
    for theirs_bytes, theirs_mode in unique[1:]:
        merged, conflict = _three_way_merge(store, baseline_bytes, ours, theirs_bytes)
        if conflict:
            raise CandidateConflictError([path])
        ours = merged
        resolved_mode, mode_conflict = _resolve_modes(baseline_mode, resolved_mode, theirs_mode)
        if mode_conflict:
            raise CandidateConflictError([path])
    return resolved_mode, ours


def _merge_changes(
    store: GitStore,
    baseline: dict[str, tuple[str, bytes]],
    proposals: list[Proposal],
) -> list[tuple[str, bytes, int]]:
    """Apply the deterministically merged changes to the baseline and return
    the sorted final (path, bytes, chmod mode) set. Multiple changes to one
    text path fold through real three-way merges; conflicts raise once with
    every conflicted path (nothing materialized)."""
    changes: dict[str, list[tuple[str, Any]]] = {}
    for proposal in proposals:  # proposals sorted by id; entries sorted by path
        for entry in proposal.files:
            changes.setdefault(entry.path, []).append((proposal.proposal_id, entry))
    conflicts: list[str] = []
    final: dict[str, tuple[str, bytes]] = dict(baseline)
    for path in sorted(changes):
        try:
            result = _merge_path_changes(
                store, path, final.get(path), changes[path]
            )
        except CandidateConflictError:
            conflicts.append(path)
            continue
        if result is None:
            final.pop(path, None)
        else:
            mode, content = result
            if len(content) > MAX_BASELINE_FILE:
                raise ValidationError(
                    f"the merged content of {path} exceeds the {MAX_BASELINE_FILE}-byte limit."
                )
            final[path] = (mode, content)
    if conflicts:
        raise CandidateConflictError(conflicts)
    return [
        (path, content, 0o755 if mode == "100755" else 0o644)
        for path, (mode, content) in sorted(final.items())
    ]


def _validate_final_set(files: list[tuple[str, bytes, int]]) -> None:
    """Directory/file prefix conflicts, platform case collisions and merged
    output limits, all BEFORE any filesystem write."""
    if len(files) > MAX_BASELINE_FILES:
        raise ValidationError(f"the merged candidate exceeds the {MAX_BASELINE_FILES}-file limit.")
    total = 0
    real_paths: set[str] = set()
    case_keys: dict[str, str] = {}
    for path, content, _mode in files:
        real_paths.add(path)
        total += len(content)
        if total > MAX_BASELINE_TOTAL:
            raise ValidationError(f"the merged candidate exceeds the {MAX_BASELINE_TOTAL}-byte limit.")
        key = path.casefold()
        if key in case_keys and case_keys[key] != path:
            raise ValidationError(
                "the merged candidate has a platform case collision; refusing ambiguous writes."
            )
        case_keys[key] = path
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if prefix in real_paths:
                raise ValidationError(
                    f"the merged candidate mixes the file and directory prefix {prefix}; "
                    "refusing ambiguous writes."
                )
            pkey = prefix.casefold()
            if pkey in case_keys and case_keys[pkey] != prefix:
                raise ValidationError(
                    "the merged candidate has a platform case collision in a directory prefix; "
                    "refusing ambiguous writes."
                )
            case_keys[pkey] = prefix


# ---------------------------------------------------------- materialization ----


def _exclusive_write(target: Path, content: bytes, mode_int: int) -> None:
    """Raw-byte exclusive create (O_CREAT|O_EXCL|O_NOFOLLOW, never a symlink)
    of one candidate file; nothing pre-existing is ever opened or overwritten."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(content)
        os.chmod(target, mode_int)
    finally:
        if fd >= 0:
            os.close(fd)


def _materialize(output: Path, files: list[tuple[str, bytes, int]]) -> None:
    """Create the candidate directory EXCLUSIVELY and write every file; any
    failure removes ONLY the directory created by this operation."""
    os.mkdir(output, 0o700)  # exclusive + PRIVATE generated code (never umask-derived)
    try:
        for rel, content, mode_int in files:
            target = resolve_within_workspace(output, rel, reject_symlinks=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            _exclusive_write(target, content, mode_int)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


# ------------------------------------------------------------- fingerprint ----

# Entry budget: the ORIGINAL materialized files PLUS their directory
# overhead PLUS bounded newly generated entries (every examined entry —
# file or directory — consumes budget; exhaustion stops the whole walk).
_FP_ENTRY_BUDGET = 2 * MAX_BASELINE_FILES
_FP_MAX_DEPTH = 64
_MAX_READ_CHUNK = 1 << 20
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _ancestor_prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _fingerprint_records(
    root: Path, original_paths: tuple[str, ...] = ()
) -> tuple[str, bool]:
    """Bounded, nofollow content fingerprint of the candidate directory.

    Returns (digest, complete): `complete` is False whenever ANY original
    or generated entry could not be honestly examined — unreadable/missing
    root or entries, a symbolic link where content was expected, oversize
    or over-limit content, entry/byte budget exhaustion or a depth overrun
    — so an equal digest with incomplete evidence can NEVER be mistaken for
    an unchanged candidate.

    Every ORIGINAL materialized path is ALWAYS included — even when it
    looks like a runtime cache (a `.pyc` name or a cache-directory
    component) — and a cache-named directory is only skipped when it
    contains no original file; ONLY newly generated standard cache
    artifacts are excluded. Budgets: _FP_ENTRY_BUDGET entries (originals +
    directory overhead + bounded new entries), MAX_BASELINE_FILE per read,
    MAX_BASELINE_TOTAL total, _FP_MAX_DEPTH deep. Enumeration is bounded
    with the REMAINING budget BEFORE sorting and the walk stops GLOBALLY
    once any budget is exhausted (no unbounded scandir, no reads after
    overrun, no RecursionError). Regular files are opened with
    O_NOFOLLOW|O_NONBLOCK and re-verified via fstat (regular + identity)
    before any read, guarding replaced FIFOs/symlinks. Records are encoded
    as canonical JSON (ensure_ascii) so newline/unicode filenames created
    by checks can never forge rows or break the encoding. Anomalies are
    markers — never uncaught exceptions, never a read through a symbolic
    link or from outside the candidate directory."""
    originals = frozenset(original_paths)
    original_dirs = frozenset(
        prefix for path in originals for prefix in _ancestor_prefixes(path)
    )
    records: list[bytes] = []
    state = {"entries": 0, "bytes": 0, "exhausted": False}
    complete = True

    def record(kind: str, rel: str, **extra: Any) -> None:
        nonlocal complete
        if kind == "marker":
            complete = False  # every anomaly makes the evidence incomplete
        records.append(json.dumps(
            {"kind": kind, "path": rel, **extra},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))

    def budget_left() -> int:
        return _FP_ENTRY_BUDGET - state["entries"]

    def read_regular(entry_path: Path, rel: str, lst: os.stat_result) -> None:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
        try:
            fd = os.open(entry_path, flags)
        except OSError:
            record("marker", rel, reason="unreadable")
            return
        try:
            try:
                fst = os.fstat(fd)
            except OSError:
                record("marker", rel, reason="unreadable")
                return
            if not stat.S_ISREG(fst.st_mode):
                # replaced by a FIFO/symlink/device after lstat (O_NONBLOCK
                # keeps a FIFO open from ever blocking)
                record("marker", rel, reason="not-regular")
                return
            if (fst.st_dev, fst.st_ino) != (lst.st_dev, lst.st_ino):
                record("marker", rel, reason="identity-changed")
                return
            chunks: list[bytes] = []
            remaining = MAX_BASELINE_FILE + 1
            while remaining > 0:
                if state["exhausted"]:
                    return
                try:
                    chunk = os.read(fd, min(remaining, _MAX_READ_CHUNK))
                except OSError:
                    record("marker", rel, reason="unreadable")
                    return
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if len(data) > MAX_BASELINE_FILE:
            record("marker", rel, reason="oversize")
            return
        state["bytes"] += len(data)
        if state["bytes"] > MAX_BASELINE_TOTAL:
            state["exhausted"] = True  # stop the WHOLE walk here
            record("marker", rel, reason="overrun-bytes")
            return
        record(
            "file", rel,
            mode=f"{stat.S_IMODE(fst.st_mode):04o}",
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def visit(current: Path, rel_prefix: str, depth: int) -> None:
        if state["exhausted"]:
            return
        if depth > _FP_MAX_DEPTH:
            record("marker", rel_prefix or ".", reason="depth-limit")
            return
        # bounded enumeration BEFORE sorting: never materialize an arbitrary
        # huge directory; one entry beyond the remaining budget stops it all
        taken: list[os.DirEntry] = []
        overshoot = False
        try:
            with os.scandir(current) as scanner:
                for entry in scanner:
                    if len(taken) > budget_left():
                        overshoot = True
                        break
                    taken.append(entry)
        except OSError:
            record("marker", rel_prefix or ".", reason="unreadable")
            return
        if overshoot:
            state["exhausted"] = True
            record("marker", rel_prefix or ".", reason="overrun-entries")
            return
        taken.sort(key=lambda e: e.name)
        for entry in taken:
            if state["exhausted"]:
                break
            # Git administrative markers are metadata, never verification
            # inputs (worktrees commonly have a regular `.git` pointer file).
            if entry.name == ".git":
                continue
            if budget_left() <= 0:
                state["exhausted"] = True
                record("marker", rel_prefix or ".", reason="overrun-entries")
                break
            rel = f"{rel_prefix}{entry.name}"
            state["entries"] += 1
            try:
                lst = entry.stat(follow_symlinks=False)
            except OSError:
                record("marker", rel, reason="unreadable")
                continue
            if stat.S_ISLNK(lst.st_mode):
                record("marker", rel, reason="symlink")  # never read through a link
                continue
            if stat.S_ISDIR(lst.st_mode):
                if (
                    entry.name.casefold() in _FP_IGNORE_DIR_NAMES
                    and rel not in original_dirs
                ):
                    continue  # NEWLY generated standard cache only
                visit(entry.path, rel + "/", depth + 1)
                continue
            if not stat.S_ISREG(lst.st_mode):
                record("marker", rel, reason="not-regular")
                continue
            if rel not in originals and entry.name.endswith(".pyc"):
                continue  # NEWLY generated bytecode only; originals always included
            if lst.st_size > MAX_BASELINE_FILE:
                record("marker", rel, reason="oversize")
                continue
            read_regular(entry.path, rel, lst)

    try:
        lst = os.lstat(root)
    except OSError:
        record("marker", ".", reason="root-unreadable")
    else:
        if stat.S_ISLNK(lst.st_mode):
            record("marker", ".", reason="root-symlink")  # no external read
        elif not stat.S_ISDIR(lst.st_mode):
            record("marker", ".", reason="root-missing")
        else:
            visit(root, "", 1)
    return hashlib.sha256(b"\n".join(records)).hexdigest(), complete


def _fingerprint(root: Path, original_paths: tuple[str, ...] = ()) -> str:
    """Digest-only wrapper around `_fingerprint_records` (kept for callers
    and tests that only need the digest; completion-aware callers must use
    `_fingerprint_records`)."""
    return _fingerprint_records(root, original_paths)[0]


# -------------------------------------------------------------- verification ----


def _run_verification(output: Path, original_paths: tuple[str, ...]) -> dict[str, Any]:
    """Execute the candidate's checks ONLY when a plan is detected (the
    caller supplies the explicit `--verify` authorization): no detected plan
    => not_run (never PASS); bad plan config or process failure => error.
    Summaries carry check ids, statuses, exit codes and timeouts ONLY —
    never stdout/stderr or project content. Candidate content is fingerprinted
    before and after with the SAME bounded nofollow rules (original
    materialized paths always included), and the clearly-labelled
    fingerprint_before/fingerprint_after values plus an explicit
    fingerprint_complete flag are returned with the outcome. Verification
    NEVER passes on incomplete fingerprint evidence: an INCOMPLETE before
    fingerprint errors WITHOUT executing any check; an incomplete after
    fingerprint (or any content/mode/symlink change) marks the candidate
    NOT verified (status invalidated) even when both digests are equal.
    Fingerprint anomalies never raise — they make the evidence incomplete
    instead, so a replaced root or an oversize file surfaces as an honest
    invalidation. Fingerprints detect PERSISTENT changes only; transient
    edits restored in between snapshots are not claimed as detected, and no
    complete mutation monitoring is ever claimed."""
    from pipeline_runtime.verification_detect import detect_verification_plan

    try:
        plan = detect_verification_plan(output)
    except Exception:
        return {"status": "error", "plan_id": None, "checks": [], "changed_content": False}
    if plan is None:
        return {"status": "not_run", "plan_id": None, "checks": [], "changed_content": False}
    from process_runtime import ProcessRunner
    from verification_runtime.runner import VerificationRunner
    from workspace.local import LocalWorkspace

    before, before_complete = _fingerprint_records(output, original_paths)
    if not before_complete:
        # incomplete BEFORE evidence: refuse to execute any check at all
        return {
            "status": "error", "plan_id": plan.plan_id, "checks": [],
            "changed_content": False,
            "fingerprint_before": before, "fingerprint_after": None,
            "fingerprint_complete": False,
        }
    try:
        report = VerificationRunner(ProcessRunner()).run(LocalWorkspace(output), plan)
    except Exception:
        after, after_complete = _fingerprint_records(output, original_paths)
        return {
            "status": "error", "plan_id": plan.plan_id, "checks": [],
            "changed_content": before != after,
            "fingerprint_before": before, "fingerprint_after": after,
            "fingerprint_complete": after_complete,
        }
    after, after_complete = _fingerprint_records(output, original_paths)
    changed = before != after
    checks = [
        {
            "check_id": result.check_id,
            "status": result.status.value,
            "exit_code": (
                result.process_result.exit_code if result.process_result is not None else None
            ),
            "timed_out": bool(
                result.process_result.timed_out if result.process_result is not None else False
            ),
        }
        for result in report.results
    ]
    if not (before_complete and after_complete):
        overall = "invalidated"  # incomplete evidence can never be a pass
    elif changed:
        overall = "invalidated"
    else:
        overall = report.status.value
    return {
        "status": overall,
        "plan_id": plan.plan_id,
        "checks": checks,
        "changed_content": changed,
        "fingerprint_before": before,
        "fingerprint_after": after,
        "fingerprint_complete": before_complete and after_complete,
    }


# ------------------------------------------------------------- assembly ----


def _load_selected(
    store: GitStore, proposal_ids: list[str], head: str, current: SessionState
) -> list[Proposal]:
    """Fetch, parse and validate every selected proposal: distinct ids, the
    MAX bound, live owner/goal/scopes for each recorded task, the CURRENT
    shared context, and never two cumulative proposals for the same task."""
    cleaned: list[str] = []
    for raw in proposal_ids:
        identifier = safe_id(raw, "proposal id")
        if identifier in cleaned:
            raise ValidationError("the selected proposal ids must be distinct.")
        cleaned.append(identifier)
    if not cleaned:
        raise ValidationError("at least one explicitly selected proposal is required.")
    if len(cleaned) > MAX_SELECTED_PROPOSALS:
        raise ValidationError(
            f"at most {MAX_SELECTED_PROPOSALS} proposals can be combined per candidate."
        )
    proposals: list[Proposal] = []
    by_task: dict[str, list[str]] = {}
    for identifier in sorted(cleaned):
        proposal = read_proposal(store, identifier)
        _validate_recorded_provenance(
            store, proposal, head, current, require_live_task_match=True
        )
        if proposal.context_hash != current.context_hash:
            raise StaleRevisionError(
                "the shared context changed after a selected proposal was recorded; re-capture."
            )
        proposals.append(proposal)
        by_task.setdefault(proposal.task_id, []).append(identifier)
    for task_id, ids in by_task.items():
        if len(ids) > 1:
            raise ValidationError(
                f"two cumulative proposals were selected for task {task_id} "
                f"({', '.join(sorted(ids))}); proposals are cumulative — choose the latest one."
            )
    return proposals


def _snapshot_valid(store: GitStore, toplevel: Path, head: str, current: SessionState,
                    supplied: SharedSnapshot | None = None) -> tuple[str, str]:
    """The bound snapshot in the checkout must be a VALID snapshot of the
    CURRENT session: integrity-checked against its own published revision,
    matching session/base and the current context. Returns (binding
    revision, current context hash)."""
    if supplied is None:
        snapshot = load_project_snapshot(toplevel)
    else:
        if not isinstance(supplied, SharedSnapshot):
            raise ValidationError("binding must be a validated shared snapshot.")
        snapshot = parse_snapshot_dict(supplied.to_dict())
    if snapshot is None:
        raise ValidationError(
            "no valid bound shared-context artifact was found in the project; bind the task first."
        )
    if not store.is_ancestor(snapshot.revision, head):
        raise ValidationError(
            "the bound snapshot revision is not part of this session's published history."
        )
    if snapshot.state.to_dict() != store._read_state(snapshot.revision).to_dict():
        raise ValidationError(
            "the bound snapshot does not match the published session state; it is tampered or foreign."
        )
    if (snapshot.state.session_id, snapshot.state.base_commit) != (
        current.session_id, current.base_commit
    ):
        raise ValidationError("the bound snapshot belongs to a different session or base commit.")
    if snapshot.context_hash != current.context_hash:
        raise StaleRevisionError(
            "the bound snapshot's context is stale; rebind the checkout before assembling a candidate."
        )
    return snapshot.revision, current.context_hash


def assemble_candidate(
    store: GitStore,
    project_root: Path | str,
    proposal_ids: list[str] | tuple[str, ...],
    output_path: Path | str,
    *,
    expected_revision: str,
    verify: bool = False,
    binding: SharedSnapshot | None = None,
) -> dict[str, Any]:
    """Materialize a NEW dedicated candidate directory from the chosen
    published proposals and the committed baseline of the session base.

    Every preflight, merge and final-set validation happens BEFORE the
    output is created; conflicts abort cleanly (nothing materialized,
    verification not_run). The hub head is RE-CHECKED against
    `expected_revision` immediately before the output is created, so a
    session that advanced during preflight/merge is rejected stale with no
    output (the receipt is point-in-time; it never silently rebases onto
    newer metadata or claims validity forever). `verify` must be EXACTLY a
    bool — explicit authorization; a truthy string never authorizes
    execution. Once the output exists, verification failures/errors never
    delete it (only construction errors clean up the owned directory).
    Returns the structured receipt — never file contents.
    `verification.status` is one of not_run/pass/fail/timeout/error/
    invalidated."""
    if type(verify) is not bool:
        raise ValidationError(
            "verify must be exactly true or false (verification requires explicit authorization)."
        )
    if not isinstance(proposal_ids, (list, tuple)) or not proposal_ids:
        raise ValidationError("at least one explicitly selected proposal is required.")
    expected = sha_hex(expected_revision, "expected_revision")
    # even direct API callers cannot point a store/hub into the source tree
    toplevel = _require_source_toplevel(project_root)
    _source_head(toplevel)
    GitStore.ensure_outside_project(store.store_path, toplevel, what="client store")
    GitStore.ensure_outside_project(Path(store.remote), toplevel, what="collaboration hub")
    hub = Path(store.remote).resolve()
    output = _validate_output_path(
        output_path, project=toplevel, hub=hub, store_path=store.store_path
    )
    head, current = store.fetch_state()
    if head != expected:
        raise StaleRevisionError(
            f"expected_revision is no longer current (the hub is at {head[:12]}); sync and retry."
        )
    binding_revision, context_hash = _snapshot_valid(store, toplevel, head, current, binding)
    _require_base_ancestor(toplevel, current.base_commit)
    proposals = _load_selected(store, list(proposal_ids), head, current)
    entries = _read_baseline_listing(toplevel, current.base_commit)
    _verify_before_identities({entry.path: entry for entry in entries}, proposals)
    baseline = _read_baseline_blobs(toplevel, entries)
    final = _merge_changes(store, baseline, proposals)
    _validate_final_set(final)
    # CAS re-check just BEFORE output creation: preflight/merge above may
    # have taken a while; a session that moved meanwhile is rejected stale
    # with NO output (never silently rebased onto newer metadata).
    fresh, _fresh_state = store.fetch_state()
    if fresh != head:
        raise StaleRevisionError(
            "the session advanced while the candidate was being assembled; "
            "no candidate directory was created; sync and retry."
        )
    _materialize(output, final)
    original_paths = tuple(path for path, _content, _mode in final)
    fingerprint = _fingerprint(output, original_paths)
    if verify:
        verification = _run_verification(output, original_paths)
    else:
        verification = {"status": "not_run", "plan_id": None, "checks": [], "changed_content": False}
    notes = [
        "baseline read from committed base git objects only; source dirty/untracked/ignored "
        "state excluded except explicitly published proposal entries",
        "the candidate dir is an isolated materialized output, not a linked git worktree; "
        "user index/HEAD/branches/config untouched; nothing published or uploaded from it",
        "verification executes trusted project checks in the candidate dir; it is not "
        "an OS or filesystem sandbox; fingerprints detect PERSISTENT changes only "
        "(transient edits restored between snapshots are not detected) and no "
        "complete mutation monitoring is claimed",
        "this receipt is point-in-time at the recorded session revision; the hub may advance "
        "afterwards and no validity claim is ever made beyond it",
        "content_fingerprint is the ASSEMBLED candidate fingerprint; when verification changed "
        "the candidate, compare verification.fingerprint_before/fingerprint_after",
    ]
    if verification.get("status") == "invalidated":
        notes.append(
            "verification changed the candidate or the fingerprint evidence was incomplete: "
            "the candidate is NOT verified "
            "(compare verification.fingerprint_before/fingerprint_after; "
            "fingerprint_complete is false when evidence was incomplete)"
        )
    return {
        "command": "candidate",
        "session_id": current.session_id,
        "session_revision": head,
        "binding_revision": binding_revision,
        "context_hash": context_hash,
        "base_commit": current.base_commit,
        "proposal_ids": sorted(proposal.proposal_id for proposal in proposals),
        "proposals": [
            {
                "proposal_id": proposal.proposal_id,
                "task_id": proposal.task_id,
                "owner": proposal.owner,
                "context_revision": proposal.context_revision,
                "paths": [entry.path for entry in proposal.files],
            }
            for proposal in proposals
        ],
        "candidate_dir": str(output),
        "file_count": len(final),
        "content_fingerprint": fingerprint,
        "conflicts": [],
        "verification": verification,
        "notes": notes,
    }
