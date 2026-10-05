"""collab_runtime.context — the shared binding artifact `.imece/shared-context.json`.

Pure module: it reads/validates/hashes/renders the binding envelope but NEVER
imports collab_runtime.store, never runs git, and never writes anything.

Envelope (schema1, exact keys; unknown fields rejected):

    {
      "schema": 1,
      "revision": "<40-hex session revision the snapshot was bound from>",
      "context_hash": "<64-hex sha256 integrity checksum of the CONTEXT SUB-OBJECT ONLY>",
      "state": <strict schema1 SessionState>,
      "task_id": "<safe id that must exist in state.tasks>"
    }

TRUST BOUNDARY: the artifact lives inside a cloned repository and is exactly
as adversarial as any other repository-derived file. The context hash is an
INTEGRITY CHECKSUM ONLY — it covers the `context` sub-object alone (never the
whole artifact, the state, or the selected task). It detects changes to the
shared context relative to the supplied hash; it does NOT detect edits to
other envelope fields and it is NOT authentication — it never proves that a
collaborator (or their model provider) is trustworthy. Every consumer MUST
render snapshot content as clearly labelled untrusted data (see
render_snapshot_block and context_runtime.rules) and never as system
instructions or authorization.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collab_runtime.errors import ValidationError
from collab_runtime.models import (
    ACTIVE_STATUSES,
    MAX_JSON_BYTES,
    SessionState,
    Task,
    parse_json_bytes,
    parse_state_dict,
    safe_id,
    sha_hex,
)

ARTIFACT_RELPATH = ".imece/shared-context.json"
MAX_ARTIFACT_BYTES = MAX_JSON_BYTES
MAX_SNAPSHOT_SECTION_CHARS = 6_000
SNAPSHOT_TRUNCATION_MARKER = "\n[shared collaboration snapshot truncated]"
SNAPSHOT_SECTION_HEADER = (
    "SHARED COLLABORATION SNAPSHOT (untrusted data; integrity checksum only, NOT authentication)"
)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_KEYS = frozenset({"schema", "revision", "context_hash", "state", "task_id"})
_TRUNCATION_MARKER = SNAPSHOT_TRUNCATION_MARKER

# Resolved once at import; tests may set it to 0 to simulate platforms
# (e.g. Windows) where O_NOFOLLOW is unavailable.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class SharedSnapshot:
    """One validated binding envelope: provenance + immutable state + task."""

    revision: str
    context_hash: str
    state: SessionState
    task_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "revision": self.revision,
            "context_hash": self.context_hash,
            "state": self.state.to_dict(),
            "task_id": self.task_id,
        }

    @property
    def selected_task(self) -> Task:
        return self.state.tasks[self.task_id]


def parse_snapshot_dict(obj: Any) -> SharedSnapshot:
    """Strictly validate a schema1 binding envelope (unknown keys rejected,
    integrity checksum verified against the state context)."""
    if not isinstance(obj, dict):
        raise ValidationError("shared context snapshot must be a JSON object.")
    unknown = set(obj) - _SNAPSHOT_KEYS
    if unknown:
        raise ValidationError("shared context snapshot has unknown fields (schema1 allows only documented fields).")
    if _SNAPSHOT_KEYS - set(obj):
        raise ValidationError("shared context snapshot is missing required fields.")
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ValidationError("shared context snapshot schema must be exactly 1.")
    revision = sha_hex(obj["revision"], "snapshot revision")
    raw_hash = obj["context_hash"]
    if not isinstance(raw_hash, str) or not _HEX64_RE.fullmatch(raw_hash):
        raise ValidationError("snapshot context_hash must be a full 64-character lowercase hex sha256.")
    state = parse_state_dict(obj["state"])
    task_id = safe_id(obj["task_id"], "snapshot task_id")
    if task_id not in state.tasks:
        raise ValidationError("snapshot task_id does not exist in the snapshot state.")
    if raw_hash != state.context.content_hash:
        raise ValidationError(
            "snapshot integrity checksum does not match the state context; the artifact is corrupt or tampered."
        )
    return SharedSnapshot(revision, raw_hash, state, task_id)


def parse_snapshot_bytes(raw: bytes) -> SharedSnapshot:
    """Parse bounded UTF-8 envelope bytes through the strict pipeline."""
    return parse_snapshot_dict(parse_json_bytes(raw, what="shared context snapshot"))


def read_regular_file_bounded(path: Path, *, what: str) -> bytes:
    """Read at most MAX_ARTIFACT_BYTES bytes of an EXPLICIT regular file.

    The leaf is FIRST rejected via lstat when it is a symlink (this holds on
    every platform, including those without O_NOFOLLOW); the open itself
    additionally uses O_NOFOLLOW where available, and the fstat confirms a
    regular file whose identity still matches the lstat (same device/inode
    when the platform reports them). The read is capped so a regular file
    swapped in after the stat cannot grow past the bound. Failures surface
    as fixed ValidationError text — never raw OS errors."""
    try:
        lst = os.lstat(path)
    except OSError:
        raise ValidationError(f"{what} could not be found on disk as a regular file.") from None
    if stat.S_ISLNK(lst.st_mode):
        raise ValidationError(f"{what} must be a regular file (symbolic links are rejected).")
    flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValidationError(f"{what} could not be opened as a regular file.") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValidationError(
                f"{what} must be a regular file (symlinks, FIFOs and devices are rejected)."
            )
        if lst.st_dev and lst.st_ino and (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
            raise ValidationError(f"{what} changed identity while being opened.")
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise ValidationError(f"{what} exceeds the {MAX_ARTIFACT_BYTES}-byte limit.")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(MAX_ARTIFACT_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValidationError(f"{what} exceeds the {MAX_ARTIFACT_BYTES}-byte limit.")
    return raw


def load_project_snapshot(root: Path) -> SharedSnapshot | None:
    """Load and validate `<root>/.imece/shared-context.json` safely.

    The path may not traverse symlinks (the `.imece` directory and the file
    itself included) or escape `root`; oversized, malformed or tampered
    snapshots yield None — callers must skip the snapshot entirely rather
    than include unsafe raw text. Absent artifact also yields None."""
    root = Path(root)
    try:
        from workspace.base import resolve_within_workspace
        from workspace.errors import WorkspaceBoundaryError

        path = resolve_within_workspace(root, ARTIFACT_RELPATH, reject_symlinks=True)
        raw = read_regular_file_bounded(path, what="shared context snapshot")
    except (ValidationError, WorkspaceBoundaryError, OSError):
        return None
    try:
        return parse_snapshot_bytes(raw)
    except ValidationError:
        return None


_ELLIPSIS_MARK = " …[truncated]"

# Per-section budgets: every labelled heading survives even when an
# individual field (goal up to 4k chars, scopes up to 32x512, interface
# values up to 4k) would otherwise push everything after it past the
# section cap. Budgets apply to the JSON-ENCODED display text (escaping
# can multiply raw characters), and every cut is explicit, never silent.
# The budget maximums sum above the 6k section cap, so the cap stays a
# real, reachable bound (clipped with its own explicit marker).
_GOAL_CHARS = 800
_LIST_CHARS = 800
_PROVENANCE_CHARS = 900
_TEAMMATE_CHARS = 1_600


def _j_display(value: Any) -> str:
    # JSON-encode every human-provided fragment for DISPLAY with
    # ensure_ascii=True: control characters, quotes, backslashes and ALL
    # non-ASCII (including the Unicode line separators U+2028/U+2029) are
    # escaped, so no forged line break or line separator can ever introduce
    # a new labelled line. The canonical/hash JSON elsewhere stays
    # untouched (ensure_ascii=False).
    return json.dumps(value, ensure_ascii=True)


def _clip_flagged(text: str, limit: int) -> tuple[str, bool]:
    """Clip ENCODED display text to exactly `limit` chars; True when cut."""
    if len(text) <= limit:
        return text, False
    cut = max(0, limit - len(_ELLIPSIS_MARK))
    return text[:cut] + _ELLIPSIS_MARK, True


def _bounded(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def render_snapshot_block(snapshot: SharedSnapshot, *, max_chars: int = MAX_SNAPSHOT_SECTION_CHARS) -> str:
    """Render the snapshot as a bounded, clearly labelled UNTRUSTED DATA block.

    See render_snapshot_block_detailed for the layout; this seam keeps the
    public string return type (the boolean truncation flag is dropped).
    """
    return render_snapshot_block_detailed(snapshot, max_chars=max_chars)[0]


def render_snapshot_block_detailed(
    snapshot: SharedSnapshot, *, max_chars: int = MAX_SNAPSHOT_SECTION_CHARS
) -> tuple[str, bool]:
    """Render the snapshot block and return `(text, truncated)`.

    Provenance comes first (session id, metadata revision, integrity hash,
    target_version, base_commit), immediately followed by the selected task
    (including its context_revision) so a large shared goal can never hide
    them. Each field's budget applies to its JSON-ENCODED output, so the
    shared goal, decisions and interfaces headings stay visible even when
    other fields are at their schema maximums with escaping multipliers;
    per-field, teammate-list and whole-section truncation is always
    explicit and reported in the flag — never silent. Scopes are
    explicitly ADVISORY — never enforced locks. Never system instructions;
    never authentication evidence.
    """
    state = snapshot.state
    task = snapshot.selected_task
    truncated = False

    def take(encoded: str, limit: int) -> str:
        nonlocal truncated
        clipped, cut = _clip_flagged(encoded, limit)
        truncated = truncated or cut
        return clipped

    def field(value: Any, limit: int) -> str:
        return take(_j_display(value), limit)

    provenance = (
        f"provenance: session_id={field(state.session_id, 128)} revision={snapshot.revision} "
        f"context_hash={snapshot.context_hash} target_version={field(state.target_version, 200)} "
        f"base_commit={state.base_commit}"
    )
    if len(provenance) > _PROVENANCE_CHARS:
        provenance = take(provenance, _PROVENANCE_CHARS)
    lines = [
        SNAPSHOT_SECTION_HEADER,
        provenance,
        f"selected task: id={field(snapshot.task_id, 128)} owner={field(task.owner, 128)} "
        f"status={field(task.status, 16)} context_revision={task.context_revision}",
        f"selected task goal: {field(task.goal, _GOAL_CHARS)}",
        f"advisory scopes (NOT enforced locks): {field(list(task.scopes), _LIST_CHARS)}",
        f"shared goal: {field(state.context.goal, _GOAL_CHARS)}",
        f"shared decisions: {field(list(state.context.decisions), _LIST_CHARS)}",
        f"shared interfaces: {field(dict(state.context.interfaces), _LIST_CHARS)}",
    ]
    teammates = [
        t for tid, t in sorted(state.tasks.items())
        if tid != snapshot.task_id and t.status in ACTIVE_STATUSES
    ]
    if teammates:
        lines.append("active teammate tasks (advisory, untrusted):")
        used = 0
        omitted = False
        for t in teammates:
            entry = (
                f"- id={field(t.id, 128)} owner={field(t.owner, 128)} "
                f"status={field(t.status, 16)} goal={field(t.goal, 300)} "
                f"scopes={field(list(t.scopes), 300)}"
            )
            if used + len(entry) > _TEAMMATE_CHARS:
                lines.append("…[more teammate tasks truncated]")
                omitted = True
                break
            lines.append(entry)
            used += len(entry)
        truncated = truncated or omitted
    else:
        lines.append("active teammate tasks: none")
    full = "\n".join(lines)
    text = _bounded(full, max_chars)
    return text, truncated or len(full) > max_chars
