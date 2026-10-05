"""collab_runtime.models — strict schema1 session state, canonical JSON, validation.

Pure Python: no git, no subprocess, no filesystem access. collab_runtime.store
is the only module allowed to run git.

Schema 1 (exact keys; unknown fields rejected at every level):

    {
      "schema": 1,
      "session_id": "<safe id>",
      "target_version": "<bounded title>",
      "base_commit": "<40-hex sha>",
      "context": {
        "goal": "<bounded str>",
        "decisions": ["<bounded str>", ...],
        "interfaces": {"<safe id>": "<bounded str>", ...}
      },
      "tasks": {
        "<safe task id>": {
          "owner": "<safe id>",
          "goal": "<bounded str>",
          "scopes": ["<repo-relative file path or directory prefix ending in '/'>", ...],
          "status": "queued" | "running" | "waiting" | "done",
          "context_revision": "<40-hex sha of the context revision at task assignment>"
        }
      }
    }

Canonical JSON: json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False) encoded UTF-8. The immutable session revision is the git
commit SHA that published a state; the context content hash is sha256 over
the canonical JSON of the `context` sub-object only.

Hostile JSON (duplicate keys, NaN/Infinity, unpaired surrogates, pathological
nesting) and wrong JSON types (null/int/list where an object belongs) are
always reported as ValidationError — never as raw TypeError/RecursionError/
UnicodeEncodeError leaks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from collab_runtime.errors import ValidationError

MAX_JSON_BYTES = 64 * 1024
MAX_FRAMED_JSON_BYTES = MAX_JSON_BYTES + 256
MAX_TARGET_VERSION_CHARS = 200
MAX_GOAL_CHARS = 4_000
MAX_DECISIONS = 64
MAX_DECISION_CHARS = 2_000
MAX_INTERFACES = 64
MAX_INTERFACE_VALUE_CHARS = 4_000
MAX_TASKS = 256
MAX_SCOPES_PER_TASK = 32
MAX_SCOPE_CHARS = 512
MAX_TASK_GOAL_CHARS = 4_000

SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")

ACTIVE_STATUSES = ("queued", "running", "waiting")
ALL_STATUSES = ACTIVE_STATUSES + ("done",)

_STATE_KEYS = frozenset({"schema", "session_id", "target_version", "base_commit", "context", "tasks"})
_CONTEXT_KEYS = frozenset({"goal", "decisions", "interfaces"})
_TASK_KEYS = frozenset({"owner", "goal", "scopes", "status", "context_revision"})


def safe_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(
            f"{what} must be 1-128 chars of [A-Za-z0-9._-] starting with a letter or digit."
        )
    return value


def sha_hex(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ValidationError(f"{what} must be a full 40-character lowercase hex git SHA.")
    return value


def _bad_chars(value: str) -> bool:
    return any(
        (ord(ch) < 32 and ch != "\n") or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF
        for ch in value
    )


def _bounded_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string.")
    if _bad_chars(value):
        raise ValidationError(f"{what} must not contain control characters or surrogates.")
    if len(value) > limit:
        raise ValidationError(f"{what} exceeds the {limit}-character limit.")
    return value


def scope_path(value: Any) -> str:
    """Validate one task scope: a repo-relative file path or a directory
    prefix that MUST end with '/'. Traversal, absolute/drive paths, glob
    syntax and .git are rejected."""
    if not isinstance(value, str):
        raise ValidationError("task scope must be a string.")
    if "\x00" in value or _bad_chars(value):
        raise ValidationError("task scope must not contain control characters or surrogates.")
    if len(value) > MAX_SCOPE_CHARS:
        raise ValidationError(f"task scope exceeds the {MAX_SCOPE_CHARS}-character limit.")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValidationError("task scope must be relative (absolute paths rejected).")
    if _WINDOWS_DRIVE_RE.match(normalized):
        raise ValidationError("task scope must be relative (drive paths rejected).")
    if any(ch in normalized for ch in "*?["):
        raise ValidationError("task scope must not contain glob characters (*, ?, [).")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise ValidationError("task scope must name a file or directory.")
    if any(p == ".." for p in parts):
        raise ValidationError("task scope must not contain '..' (traversal rejected).")
    if any(p.casefold() == ".git" for p in parts):
        raise ValidationError("task scope must not reference .git.")
    cleaned = "/".join(parts)
    return cleaned + "/" if normalized.endswith("/") else cleaned


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def context_hash_of(context: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of a {"goal","decisions","interfaces"} dict."""
    return hashlib.sha256(canonical_json_bytes(dict(context))).hexdigest()


def parse_json_bytes(raw: bytes, *, what: str, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Decode bounded UTF-8 JSON with strict duplicate/constant/Unicode checks.

    The default cap remains 64 KiB. The optional cap accepts values through
    64 KiB + 256 for the snapshot client's small response envelope allowance;
    that client separately validates canonical state bytes against 64 KiB.
    Duplicate keys, non-standard constants, unpaired surrogates and pathological
    nesting remain rejected at every cap.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FRAMED_JSON_BYTES:
        raise ValidationError("the JSON byte limit is invalid.")
    if len(raw) > max_bytes:
        raise ValidationError(f"{what} exceeds the {max_bytes}-byte limit.")

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValidationError(f"{what} contains duplicate object keys.")
            out[key] = value
        return out

    def _reject_constant(name: str) -> None:
        raise ValidationError(f"{what} contains the non-standard JSON constant {name}.")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{what} is not valid UTF-8.") from None
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise ValidationError(f"{what} is not valid JSON.") from None
    except RecursionError:
        raise ValidationError(f"{what} is nested too deeply.") from None
    try:
        canonical_json_bytes(value)  # rejects unpaired surrogates / non-encodable content
    except UnicodeEncodeError:
        raise ValidationError(f"{what} contains unpaired Unicode surrogate characters.") from None
    except RecursionError:
        raise ValidationError(f"{what} is nested too deeply.") from None
    return value


@dataclass(frozen=True)
class SharedContext:
    goal: str
    decisions: tuple[str, ...]
    interfaces: tuple[tuple[str, str], ...]  # sorted by key

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "decisions": list(self.decisions),
            "interfaces": {key: value for key, value in self.interfaces},
        }

    @property
    def content_hash(self) -> str:
        return context_hash_of(self.to_dict())


@dataclass(frozen=True)
class Task:
    id: str
    owner: str
    goal: str
    scopes: tuple[str, ...]
    status: str
    context_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "goal": self.goal,
            "scopes": list(self.scopes),
            "status": self.status,
            "context_revision": self.context_revision,
        }


@dataclass(frozen=True)
class SessionState:
    session_id: str
    target_version: str
    base_commit: str
    context: SharedContext
    tasks: Mapping[str, Task]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "session_id": self.session_id,
            "target_version": self.target_version,
            "base_commit": self.base_commit,
            "context": self.context.to_dict(),
            "tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()},
        }

    @property
    def context_hash(self) -> str:
        return self.context.content_hash

    def with_context(self, context: SharedContext) -> "SessionState":
        return SessionState(self.session_id, self.target_version, self.base_commit, context, dict(self.tasks))

    def with_task(self, task: Task) -> "SessionState":
        tasks = dict(self.tasks)
        tasks[task.id] = task
        return SessionState(self.session_id, self.target_version, self.base_commit, self.context, tasks)


def _require_dict(obj: Any, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError(f"{what} must be a JSON object.")
    return obj


def _exact_keys(obj: Any, allowed: frozenset[str], what: str) -> None:
    _require_dict(obj, what)
    unknown = set(obj) - allowed
    if unknown:
        raise ValidationError(f"{what} has unknown fields (schema1 allows only documented fields).")
    if allowed - set(obj):
        raise ValidationError(f"{what} is missing required fields.")


def _parse_context(obj: Any) -> SharedContext:
    _exact_keys(obj, _CONTEXT_KEYS, "context")
    goal = _bounded_text(obj["goal"], "context.goal", MAX_GOAL_CHARS)
    raw_decisions = obj["decisions"]
    if not isinstance(raw_decisions, list):
        raise ValidationError("context.decisions must be a list.")
    if len(raw_decisions) > MAX_DECISIONS:
        raise ValidationError(f"at most {MAX_DECISIONS} decisions are allowed.")
    decisions = tuple(_bounded_text(d, "context decision", MAX_DECISION_CHARS) for d in raw_decisions)
    raw_interfaces = _require_dict(obj["interfaces"], "context.interfaces")
    if len(raw_interfaces) > MAX_INTERFACES:
        raise ValidationError(f"at most {MAX_INTERFACES} interfaces are allowed.")
    interfaces = tuple(
        sorted(
            (safe_id(key, "interface name"), _bounded_text(value, "interface value", MAX_INTERFACE_VALUE_CHARS))
            for key, value in raw_interfaces.items()
        )
    )
    return SharedContext(goal, decisions, interfaces)


def _parse_task(task_id: str, obj: Any) -> Task:
    _exact_keys(obj, _TASK_KEYS, "task entry")
    owner = safe_id(obj["owner"], "task owner")
    goal = _bounded_text(obj["goal"], "task goal", MAX_TASK_GOAL_CHARS)
    raw_scopes = obj["scopes"]
    if not isinstance(raw_scopes, list):
        raise ValidationError("task scopes must be a list.")
    if len(raw_scopes) > MAX_SCOPES_PER_TASK:
        raise ValidationError(f"at most {MAX_SCOPES_PER_TASK} scopes per task are allowed.")
    scopes: list[str] = []
    for raw in raw_scopes:
        scope = scope_path(raw)
        if scope not in scopes:
            scopes.append(scope)
    status = obj["status"]
    if not isinstance(status, str) or status not in ALL_STATUSES:
        raise ValidationError("task status must be one of: " + ", ".join(ALL_STATUSES) + ".")
    return Task(
        safe_id(task_id, "task id"), owner, goal, tuple(scopes), status,
        sha_hex(obj["context_revision"], "task context_revision"),
    )


def parse_state_dict(obj: Any) -> SessionState:
    """Strictly validate a schema1 state dict (unknown keys rejected)."""
    _exact_keys(obj, _STATE_KEYS, "session state")
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ValidationError("session state schema must be exactly 1.")
    session_id = safe_id(obj["session_id"], "session_id")
    target_version = _bounded_text(obj["target_version"], "target_version", MAX_TARGET_VERSION_CHARS)
    base_commit = sha_hex(obj["base_commit"], "base_commit")
    context = _parse_context(obj["context"])
    raw_tasks = _require_dict(obj["tasks"], "tasks")
    if len(raw_tasks) > MAX_TASKS:
        raise ValidationError(f"at most {MAX_TASKS} tasks are allowed.")
    tasks = {
        safe_id(task_id, "task id"): _parse_task(task_id, raw)
        for task_id, raw in raw_tasks.items()
    }
    return SessionState(session_id, target_version, base_commit, context, tasks)


def build_initial_state(*, session_id: str, target_version: str, base_commit: str) -> SessionState:
    """Validated empty session state for `init_session`."""
    return SessionState(
        safe_id(session_id, "session_id"),
        _bounded_text(target_version, "target_version", MAX_TARGET_VERSION_CHARS),
        sha_hex(base_commit, "base_commit"),
        SharedContext("", (), ()),
        {},
    )


def build_context(*, goal: str, decisions: list[str], interfaces: dict[str, str]) -> SharedContext:
    """Validated constructor: raw field types are checked, never coerced."""
    if not isinstance(decisions, list):
        raise ValidationError("context.decisions must be a list.")
    if not isinstance(interfaces, dict):
        raise ValidationError("context.interfaces must be a JSON object.")
    return _parse_context({"goal": goal, "decisions": decisions, "interfaces": interfaces})


def build_task(
    *, task_id: str, owner: str, goal: str, scopes: list[str], status: str, context_revision: str
) -> Task:
    """Validated constructor: raw field types are checked, never coerced."""
    if not isinstance(scopes, list):
        raise ValidationError("task scopes must be a list.")
    return _parse_task(task_id, {
        "owner": owner, "goal": goal, "scopes": scopes,
        "status": status, "context_revision": context_revision,
    })


def _scopes_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    return a.endswith("/") and b.startswith(a) or b.endswith("/") and a.startswith(b)


def _shared_scope(a: str, b: str) -> str:
    if a == b:
        return a
    if a.endswith("/") and b.startswith(a):
        return a
    return b


def compute_overlaps(tasks: Mapping[str, Task]) -> list[dict[str, Any]]:
    """Advisory overlap report among ACTIVE tasks (done excluded).

    Exact file matches and directory-prefix intersections only; never a lock.
    Returns [{"tasks": [a, b], "shared": [paths]}], deterministically sorted.
    """
    active = {tid: task for tid, task in sorted(tasks.items()) if task.status in ACTIVE_STATUSES}
    ids = list(active)
    out: list[dict[str, Any]] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            shared = sorted({
                _shared_scope(sa, sb)
                for sa in active[a].scopes
                for sb in active[b].scopes
                if _scopes_overlap(sa, sb)
            })
            if shared:
                out.append({"tasks": [a, b], "shared": shared})
    return out


def task_conflicts(tasks: Mapping[str, Task]) -> dict[str, list[str]]:
    """Advisory per-task map: task id -> other active task ids sharing a scope."""
    conflicts: dict[str, set[str]] = {tid: set() for tid in tasks}
    for pair in compute_overlaps(tasks):
        a, b = pair["tasks"]
        conflicts[a].add(b)
        conflicts[b].add(a)
    return {tid: sorted(others) for tid, others in sorted(conflicts.items())}
