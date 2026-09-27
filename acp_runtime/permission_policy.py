"""Per-role ACP permission policies.

AcpClientRuntime.run() always denied every `session/request_permission` call
(see acp_runtime/client.py history): the pipeline engine's own worktree
isolation was documented as "the safety boundary, not the CLI's own
prompts" (docs/ARCHITECTURE.md). In practice a real Claude Code ACP session
asks for permission before every edit/delete/move and before every shell
command, and a client that always cancels those requests means the agent
can never actually change anything -- the first real end-to-end run
produced `run.completed {"reason": "no_changes"}` for exactly this reason.

This module gives AcpClientRuntime an injectable policy instead of a single
hardcoded behaviour:

- `DenyAllAcpPermissionPolicy` -- the original behaviour. Used whenever no
  policy is supplied, so the Planner/Reviewer (and any other caller) keep
  denying every permission request unconditionally.
- `WorktreeEditAcpPermissionPolicy` -- used only for the Worker. The
  isolated Git worktree is *already* the safety boundary (nothing outside
  it is the user's real project), so this policy allows exactly the
  requests that stay inside that boundary and edit/delete/move a file --
  and rejects everything else (execute/fetch/read-outside-worktree/unknown
  kind/unresolvable path), never selecting "allow_always"/"allow-with-
  updates" (which would persist a permission rule in the user's own Claude
  settings, entirely outside our control).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

_EDIT_KINDS = frozenset({"edit", "delete", "move"})

# Preference order for the option kind selected when allowing/rejecting.
# "allow_always"/"reject_always" persist a rule in the agent CLI's own
# settings outside our control -- never select those. "allow_once" is
# selected for a permitted request; "reject_once" (falling back to
# "reject_always" only if that is literally the only rejecting option
# offered) for a denied one.
_ALLOW_KIND_PREFERENCE: tuple[str, ...] = ("allow_once",)
_REJECT_KIND_PREFERENCE: tuple[str, ...] = ("reject_once", "reject_always")

# Conservative fallback keys inspected in rawInput when a tool call arrives
# with no `locations` at all (some ACP agents only populate rawInput for
# certain tool kinds). Any of these whose value is a string, or a list of
# strings, is treated as a candidate path.
_RAW_INPUT_PATH_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "filePath",
    "abs_path",
    "absPath",
    "new_path",
    "newPath",
    "old_path",
    "oldPath",
)


@dataclass(frozen=True, slots=True)
class AcpPermissionDecision:
    """What the client should do about one `session/request_permission`.

    `outcome_kind` is "selected" (pick `option_id` from the offered
    options) or "cancelled" (send the cancelled/denied outcome, no option
    selected). `reason` is a short, human-readable explanation recorded
    alongside the canonical `permission.resolved` event -- never trusted
    input, always text this policy itself produced.
    """

    outcome_kind: str
    option_id: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.outcome_kind not in ("selected", "cancelled"):
            raise ValueError("AcpPermissionDecision.outcome_kind must be 'selected' or 'cancelled'")
        if self.outcome_kind == "selected" and not self.option_id:
            raise ValueError("AcpPermissionDecision(outcome_kind='selected') requires an option_id")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("AcpPermissionDecision.reason must be a non-empty string")


class AcpPermissionPolicy(Protocol):
    def decide(
        self,
        *,
        tool_call_id: str,
        title: str,
        kind: str | None,
        locations: Sequence[str],
        raw_input: Any,
        options: Sequence[Mapping[str, str]],
    ) -> AcpPermissionDecision:
        ...


def _select_option(options: Sequence[Mapping[str, str]], preference: Sequence[str]) -> str | None:
    by_kind: dict[str, str] = {}
    for option in options:
        option_kind = option.get("kind")
        option_id = option.get("option_id")
        if option_kind and option_id and option_kind not in by_kind:
            by_kind[option_kind] = option_id
    for kind in preference:
        if kind in by_kind:
            return by_kind[kind]
    return None


class DenyAllAcpPermissionPolicy:
    """The default, original behaviour: cancel every permission request."""

    def decide(
        self,
        *,
        tool_call_id: str,
        title: str,
        kind: str | None,
        locations: Sequence[str],
        raw_input: Any,
        options: Sequence[Mapping[str, str]],
    ) -> AcpPermissionDecision:
        return AcpPermissionDecision(
            outcome_kind="cancelled",
            option_id=None,
            reason="deny-all policy (default): the caller's own isolation is the safety boundary",
        )


def _raw_input_candidate_paths(raw_input: Any) -> list[str]:
    if not isinstance(raw_input, Mapping):
        return []
    candidates: list[str] = []
    for key in _RAW_INPUT_PATH_KEYS:
        if key not in raw_input:
            continue
        value = raw_input[key]
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, (list, tuple)):
            candidates.extend(item for item in value if isinstance(item, str))
    return candidates


def _is_inside(path: str, root: str) -> bool:
    """True iff `path` resolves (realpath, symlinks followed) to `root`
    itself or somewhere strictly inside it. Conservative: any failure to
    resolve is treated as outside."""
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError, TypeError):
        return False
    if resolved == root:
        return True
    return resolved.startswith(root + os.sep)


class WorktreeEditAcpPermissionPolicy:
    """Worker policy: allow an edit/delete/move whose every affected path
    resolves inside the isolated worktree root; reject everything else."""

    def __init__(self, worktree_root: str) -> None:
        if not isinstance(worktree_root, str) or not worktree_root:
            raise ValueError("WorktreeEditAcpPermissionPolicy.worktree_root must be a non-empty string")
        self._root = os.path.realpath(worktree_root)

    def decide(
        self,
        *,
        tool_call_id: str,
        title: str,
        kind: str | None,
        locations: Sequence[str],
        raw_input: Any,
        options: Sequence[Mapping[str, str]],
    ) -> AcpPermissionDecision:
        if kind not in _EDIT_KINDS:
            return self._reject(options, f"tool kind {kind!r} is not an edit/delete/move -- worker may only edit files")

        candidates: list[str] = [path for path in locations if isinstance(path, str) and path]
        if not candidates:
            candidates = _raw_input_candidate_paths(raw_input)
        if not candidates:
            return self._reject(options, "no resolvable path was offered with this permission request")

        for path in candidates:
            if not os.path.isabs(path):
                return self._reject(options, f"path {path!r} is not absolute")
            if not _is_inside(path, self._root):
                return self._reject(options, f"path {path!r} resolves outside the isolated worktree")

        option_id = _select_option(options, _ALLOW_KIND_PREFERENCE)
        if option_id is None:
            return self._reject(options, "no allow_once option was offered")
        return AcpPermissionDecision(
            outcome_kind="selected",
            option_id=option_id,
            reason="edit/delete/move confined to the isolated worktree",
        )

    @staticmethod
    def _reject(options: Sequence[Mapping[str, str]], reason: str) -> AcpPermissionDecision:
        option_id = _select_option(options, _REJECT_KIND_PREFERENCE)
        if option_id is not None:
            return AcpPermissionDecision(outcome_kind="selected", option_id=option_id, reason=reason)
        return AcpPermissionDecision(outcome_kind="cancelled", option_id=None, reason=reason)
