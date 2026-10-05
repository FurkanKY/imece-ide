"""collab_runtime — offline team collaboration runtime (metadata + explicit code proposals).

One dedicated bare "hub" repository carries exactly one collaboration session
(branch refs/heads/imece-session, file session.json). Clients keep their own
bare stores, fetch the authoritative state, and publish child commits with
ordinary fast-forward pushes; competing publishers get StaleRevisionError.
The source checkout is only ever read for its committed HEAD, plus the one
explicitly written, git-ignored `.imece/shared-context.json` binding
artifact (see collab_runtime.context).

On top of the metadata layer, two EXPLICIT, user-directed code steps exist
(see collab_runtime.proposals and collab_runtime.candidates):
- `proposal-publish` captures USER-SELECTED changes as bounded UTF-8/1-64
  file artifacts and publishes them as private immutable hub refs
  (refs/heads/imece-proposals/<id>); the metadata session, source index/
  HEAD/branches and everything not explicitly selected stay untouched, and
  embedded secrets in selected files can still be shared (no redaction).
- `proposal-list` prints metadata receipts only (though it currently
  transfers full bounded artifacts to validate them), and the candidate
  assembly combines explicitly selected proposals with the committed
  baseline of the session base into a NEW dedicated output directory
  outside source/hub/store — a real three-way `git merge-file` merge,
  structured conflict paths with no materialized output on conflict, and
  explicit `--verify` authorization for executing project checks in that
  isolated directory (fingerprinted before/after; not an OS sandbox).

`collab_runtime.store` (git layer), `collab_runtime.cli`, `collab_runtime.
proposals` and `collab_runtime.candidates` are intentionally NOT imported
here so pure consumers of models/errors/snapshots stay import-light; import
them explicitly.
"""

from collab_runtime.context import (
    ARTIFACT_RELPATH,
    MAX_ARTIFACT_BYTES,
    MAX_SNAPSHOT_SECTION_CHARS,
    SharedSnapshot,
    load_project_snapshot,
    parse_snapshot_bytes,
    parse_snapshot_dict,
    read_regular_file_bounded,
    render_snapshot_block,
)
from collab_runtime.errors import (
    CollabError,
    GitOperationError,
    ProjectStateError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from collab_runtime.models import (
    ACTIVE_STATUSES,
    MAX_JSON_BYTES,
    MAX_TASKS,
    SessionState,
    SharedContext,
    Task,
    build_context,
    build_initial_state,
    build_task,
    canonical_json,
    compute_overlaps,
    context_hash_of,
    parse_json_bytes,
    parse_state_dict,
    task_conflicts,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ARTIFACT_RELPATH",
    "CollabError",
    "GitOperationError",
    "MAX_ARTIFACT_BYTES",
    "MAX_JSON_BYTES",
    "MAX_SNAPSHOT_SECTION_CHARS",
    "MAX_TASKS",
    "ProjectStateError",
    "SessionAlreadyExistsError",
    "SessionNotFoundError",
    "SessionState",
    "SharedContext",
    "SharedSnapshot",
    "StaleRevisionError",
    "Task",
    "ValidationError",
    "build_context",
    "build_initial_state",
    "build_task",
    "canonical_json",
    "compute_overlaps",
    "context_hash_of",
    "load_project_snapshot",
    "parse_json_bytes",
    "parse_snapshot_bytes",
    "parse_snapshot_dict",
    "parse_state_dict",
    "read_regular_file_bounded",
    "render_snapshot_block",
    "task_conflicts",
]