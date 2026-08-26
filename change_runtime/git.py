"""GitWorktreeChangeProvider — read-only, index-safe, filter/hook-free change
capture for GitWorktreeWorkspace.

Baseline is always `workspace.snapshot.snapshot_commit`; the current side is
the workspace's current working tree. Every capture is therefore cumulative
(snapshot -> current), never a delta against only the previous capture.

Safety (see docs/superpowers/specs/2026-08-25-bounded-fix-loop-design.md):

    - subprocess argv only, shell=False, no bash -lc / cmd /c.
    - `git --no-optional-locks ...` on every invocation: git's normal `diff`/
      `status` machinery opportunistically rewrites the on-disk index with
      refreshed stat-cache data ("racy git" optimization) even for read
      commands; --no-optional-locks suppresses that so the index file is
      never touched.
    - no `git add`, `write-tree`, `commit-tree`, `update-index`, `checkout`,
      `stash`, `diff` (against the working tree): this module never stages,
      commits, or converts working-tree content through Git at all.
    - THIS IS THE KEY DESIGN CHOICE, not merely a flag: a normal
      `git diff <commit>` / `git status` working-tree comparison feeds the
      current on-disk file bytes through the repository's *clean*/*process*
      filter machinery (`.gitattributes` `filter.<driver>.clean` /
      `filter.<driver>.process`) before comparing them to the stored blob —
      `--no-ext-diff`/`--no-textconv` do NOT prevent this, they only affect
      *display* conversion. This module never runs any Git command that
      compares against the live working tree, so no repository/user
      configured clean or process filter is ever invoked, by construction
      (not by flag). The baseline side is read with `git ls-tree`/
      `git cat-file -p`, which only walk immutable Git objects and never
      touch the working tree or run filters. The current side is read
      directly from disk via plain Python file I/O — Git never sees it.
    - `-c core.fsmonitor=` on every invocation overrides (highest
      precedence, above any repository or global config file — including a
      malicious committed `.git/config`) any configured filesystem-monitor
      hook so it can never execute for this process's Git calls, even for
      the untracked-file-listing command (`git ls-files`) which would
      otherwise consult it as an optimization.
    - `git ls-tree`/`git cat-file -p`/`git ls-files` do not consult
      repository hooks (`.git/hooks/*`) at all — hooks fire around
      mutating/checkout-family commands, none of which this module ever
      runs.
    - `-c core.pager=cat` / GIT_TERMINAL_PROMPT=0: disables the pager and
      never blocks on a credential prompt.
    - untracked and changed files are rendered by this module's own
      deterministic renderer from directly-read filesystem bytes (never
      `git add -N` + diff, never `git diff --no-index`), so no git
      subprocess ever converts working-tree content.
    - symlinks are represented via os.readlink() only — their target is
      never opened, so content outside the workspace can never leak in.
    - untracked/changed regular files preserve the POSIX executable bit in
      their rendered mode (100755 vs 100644) so a chmod-only change is never
      silently collapsed into an identical textual representation/SHA.
    - blob-identity comparisons are Git repository object-format-neutral:
      the repository's actual storage hash algorithm (`sha1` or `sha256`) is
      queried once per capture via `git rev-parse --show-object-format=
      storage` and used to compute the current side's blob oid; an unknown
      format fails closed with ChangeCaptureError rather than guessing.
      That fast oid comparison is only ever an optimization to skip
      fetching the baseline blob — it is never the sole equality path: once
      baseline content IS fetched, a full raw-content equality check runs
      before anything is ever rendered, so a fast-path mismatch (or a wrong
      object-format read) can never by itself manufacture a false diff.
    - current-side reads never dereference a symlinked ANCESTOR directory
      component (only the leaf path itself may be a symlink, and even then
      its target is read via os.readlink() only, never opened): every
      current-side read goes through workspace.base.resolve_within_workspace
      with reject_symlinks=True, allow_final_symlink=True — the same
      boundary machinery the rest of the system already trusts for this,
      not a weaker parallel validator. A tracked descendant that would
      require traversing an ancestor symlink fails closed with
      ChangeCaptureError instead of reading through the link.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from change_runtime.errors import ChangeCaptureError, ChangeInputError
from change_runtime.models import WorkspaceChangeSet
from workspace.base import resolve_within_workspace
from workspace.errors import WorkspaceBoundaryError
from workspace.worktree import GitWorktreeWorkspace

_GIT_TIMEOUT = 30


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "PYTHONUTF8": "1",
        "LC_ALL": "C.UTF-8",
    }


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """Run a read-only, index-safe, filter/hook-free `git` invocation.

    argv only, shell=False. `-c core.fsmonitor=` and `--no-optional-locks`
    are prepended to every single invocation regardless of subcommand.
    """
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.pager=cat", "-c", "core.fsmonitor=", *args],
            cwd=str(cwd),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise ChangeCaptureError("git bulunamadı (PATH'te değil).") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChangeCaptureError(f"git {' '.join(args)} zaman aşımına uğradı.") from exc


def _git_ok(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    cp = _run_git(args, cwd=cwd)
    if cp.returncode != 0:
        stderr = cp.stderr.decode("utf-8", "replace").strip()
        raise ChangeCaptureError(f"git {' '.join(args)} başarısız: {stderr[:400]}")
    return cp


def _git_text(args: list[str], *, cwd: Path) -> str:
    return _git_ok(args, cwd=cwd).stdout.decode("utf-8", "replace")


def _git_paths(args: list[str], *, cwd: Path) -> list[str]:
    output = _git_text(args, cwd=cwd)
    if not output:
        return []
    return [tok for tok in output.split("\0") if tok]


def _ls_tree_baseline(baseline: str, *, cwd: Path) -> dict[str, tuple[str, str]]:
    """Baseline tracked paths -> (mode, blob_sha), restricted to `cwd`.

    Pure Git-object walk (no working-tree access, no filters, no hooks).
    Non-blob entries (submodules/gitlinks, mode 160000) are out of scope for
    this milestone and are skipped rather than mis-rendered as files.
    """
    output = _git_text(["ls-tree", "-r", "-z", baseline, "--", "."], cwd=cwd)
    entries: dict[str, tuple[str, str]] = {}
    if not output:
        return entries
    for record in output.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, obj_type, sha = meta.split(" ")
        if obj_type != "blob":
            continue
        entries[path] = (mode, sha)
    return entries


def _blob_bytes(sha: str, *, cwd: Path) -> bytes:
    """Raw bytes of an immutable Git blob object. Never touches the working
    tree; never runs a clean/process filter (those only apply when Git
    converts *working-tree* content, not when reading a stored object)."""
    return _git_ok(["cat-file", "-p", sha], cwd=cwd).stdout


_SUPPORTED_OBJECT_FORMATS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}


def _repo_object_format(root: Path) -> str:
    """The repository's actual Git object storage hash algorithm.

    Never assumed: modern Git supports `sha1` and `sha256` storage formats
    (`git init --object-format=sha256`). Queried once per capture via a
    safe, read-only, hardened `git rev-parse` call. An unrecognized value
    fails closed rather than silently guessing sha1.
    """
    value = _git_text(["rev-parse", "--show-object-format=storage"], cwd=root).strip()
    if value not in _SUPPORTED_OBJECT_FORMATS:
        raise ChangeCaptureError(f"Unsupported/unknown Git repository object format: {value!r}")
    return value


def _git_blob_oid(data: bytes, object_format: str) -> str:
    """Git's own content-addressing hash for a blob of `data`, using the
    repository's ACTUAL storage object format — never hardcoded to sha1.

    Used ONLY to cheaply detect "definitely unchanged" without fetching the
    baseline blob's bytes over a subprocess call — never used as a security
    hash, and never the sole equality path (see _sides_exactly_equal, which
    re-verifies raw content once the baseline blob has been fetched). The
    artifact-facing hash is WorkspaceChangeSet.diff_sha256 (SHA-256 over the
    rendered diff text), computed elsewhere.
    """
    hasher = _SUPPORTED_OBJECT_FORMATS.get(object_format)
    if hasher is None:
        raise ChangeCaptureError(f"Unsupported/unknown Git repository object format: {object_format!r}")
    header = f"blob {len(data)}\0".encode("ascii")
    return hasher(header + data).hexdigest()  # noqa: S324 - git object id compat, not security-sensitive


def _current_file_mode(path: Path) -> str:
    return "100755" if (path.stat().st_mode & 0o111) else "100644"


def _decode_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _rendered_lines(content: str) -> tuple[list[str], bool]:
    trailing = content.endswith("\n")
    lines = content.split("\n")
    if trailing:
        lines = lines[:-1]
    return lines, trailing


def _diff_hunk(old_lines: list[str], old_trailing: bool, new_lines: list[str], new_trailing: bool) -> list[str]:
    old_start = 0 if not old_lines else 1
    new_start = 0 if not new_lines else 1
    hunk = [f"@@ -{old_start},{len(old_lines)} +{new_start},{len(new_lines)} @@"]
    for line in old_lines:
        hunk.append(f"-{line}")
    if old_lines and not old_trailing:
        hunk.append("\\ No newline at end of file")
    for line in new_lines:
        hunk.append(f"+{line}")
    if new_lines and not new_trailing:
        hunk.append("\\ No newline at end of file")
    return hunk


def _render_text_change(path: str, mode_a: str | None, mode_b: str | None, content_a: str | None, content_b: str | None) -> str:
    lines = [f"diff --git a/{path} b/{path}"]
    if content_a is None:
        lines.append(f"new file mode {mode_b}")
        lines.append("--- /dev/null")
        lines.append(f"+++ b/{path}")
        old_lines, old_trailing = [], True
        new_lines, new_trailing = _rendered_lines(content_b)
    elif content_b is None:
        lines.append(f"deleted file mode {mode_a}")
        lines.append(f"--- a/{path}")
        lines.append("+++ /dev/null")
        old_lines, old_trailing = _rendered_lines(content_a)
        new_lines, new_trailing = [], True
    else:
        if mode_a != mode_b:
            lines.append(f"old mode {mode_a}")
            lines.append(f"new mode {mode_b}")
        lines.append(f"--- a/{path}")
        lines.append(f"+++ b/{path}")
        old_lines, old_trailing = _rendered_lines(content_a)
        new_lines, new_trailing = _rendered_lines(content_b)
    lines.extend(_diff_hunk(old_lines, old_trailing, new_lines, new_trailing))
    return "\n".join(lines)


def _render_binary_change(path: str, mode_a: str | None, mode_b: str | None, data_a: bytes | None, data_b: bytes | None) -> str:
    lines = [f"diff --git a/{path} b/{path}"]
    if data_a is None:
        lines.append(f"new file mode {mode_b}")
        lines.append(f"Binary file added: sha256={hashlib.sha256(data_b).hexdigest()} bytes={len(data_b)}")
    elif data_b is None:
        lines.append(f"deleted file mode {mode_a}")
        lines.append(f"Binary file removed: sha256={hashlib.sha256(data_a).hexdigest()} bytes={len(data_a)}")
    else:
        if mode_a != mode_b:
            lines.append(f"old mode {mode_a}")
            lines.append(f"new mode {mode_b}")
        lines.append(
            f"Binary file changed: sha256={hashlib.sha256(data_a).hexdigest()}"
            f"->{hashlib.sha256(data_b).hexdigest()} bytes={len(data_a)}->{len(data_b)}"
        )
    return "\n".join(lines)


def _render_symlink_change(path: str, target_a: str | None, target_b: str | None) -> str:
    lines = [f"diff --git a/{path} b/{path}"]
    if target_a is None:
        lines.append("new file mode 120000")
        lines.append(f"symlink target: {target_b}")
    elif target_b is None:
        lines.append("deleted file mode 120000")
        lines.append(f"symlink target removed: {target_a}")
    else:
        lines.append("symlink target changed")
        lines.append(f"old target: {target_a}")
        lines.append(f"new target: {target_b}")
    return "\n".join(lines)


class _Side:
    """One side (baseline or current) of a single path, read without git
    running any working-tree conversion (filters/hooks)."""

    __slots__ = ("kind", "mode", "text", "bytes_", "target")

    def __init__(self, *, kind: str | None, mode: str | None = None, text: str | None = None,
                 bytes_: bytes | None = None, target: str | None = None) -> None:
        self.kind = kind  # "file" | "symlink" | None (absent)
        self.mode = mode
        self.text = text
        self.bytes_ = bytes_
        self.target = target


def _current_side(root: Path, path: str) -> tuple[_Side, bytes | None]:
    """Reads current on-disk state directly (no Git subprocess). Returns the
    side plus the raw bytes used for blob-hash comparison (None for a missing
    path, or the target bytes for a symlink).

    Ancestor path components are never dereferenced through a symlink: the
    leaf itself may be a symlink (inspected via lstat/readlink, never
    opened), but a symlinked ANCESTOR directory fails closed instead of
    silently reading content from outside the workspace through it.
    """
    try:
        file_path = resolve_within_workspace(
            root, path, resolve_final=False, reject_symlinks=True, allow_final_symlink=True,
        )
    except WorkspaceBoundaryError as exc:
        raise ChangeCaptureError(
            f"Refusing to read {path!r}: an ancestor path component is a symlink "
            "(or otherwise escapes the workspace boundary)."
        ) from exc
    if file_path.is_symlink():
        target = os.readlink(file_path)
        return _Side(kind="symlink", mode="120000", target=target), target.encode("utf-8")
    if file_path.is_file():
        data = file_path.read_bytes()
        mode = _current_file_mode(file_path)
        text = _decode_text(data)
        return _Side(kind="file", mode=mode, text=text, bytes_=data), data
    return _Side(kind=None), None


def _baseline_side(root: Path, mode: str, sha: str) -> _Side:
    if mode == "120000":
        target = _blob_bytes(sha, cwd=root).decode("utf-8", errors="replace")
        return _Side(kind="symlink", mode=mode, target=target)
    data = _blob_bytes(sha, cwd=root)
    text = _decode_text(data)
    return _Side(kind="file", mode=mode, text=text, bytes_=data)


def _sides_exactly_equal(a: _Side, b: _Side) -> bool:
    """Defensive semantic equality seam, independent of any oid fast-path.

    Runs once both sides are fully materialized (real content in hand) and
    is the ONLY thing capture() trusts to decide "no diff for this path" —
    the cheap blob-oid comparison in capture() is purely an optimization to
    avoid fetching baseline content in the common case; it never gets the
    final say by itself.
    """
    if a.kind != b.kind or a.mode != b.mode:
        return False
    if a.kind == "symlink":
        return a.target == b.target
    if a.kind == "file":
        return a.bytes_ == b.bytes_
    return a.kind is None and b.kind is None


def _render_pair(path: str, a: _Side, b: _Side) -> str:
    if a.kind is None and b.kind is None:  # pragma: no cover - defensive, unreachable
        return ""
    if a.kind == "symlink" or b.kind == "symlink":
        if a.kind == "file" or b.kind == "file":
            # type change: render as delete-old-kind + add-new-kind.
            parts = []
            if a.kind is not None:
                parts.append(_render_one_sided(path, a, present=False))
            if b.kind is not None:
                parts.append(_render_one_sided(path, b, present=True))
            return "\n".join(p for p in parts if p)
        return _render_symlink_change(
            path,
            a.target if a.kind == "symlink" else None,
            b.target if b.kind == "symlink" else None,
        )
    # both "file" or one side absent.
    a_text = a.text if a.kind == "file" else None
    b_text = b.text if b.kind == "file" else None
    a_bytes = a.bytes_ if a.kind == "file" else None
    b_bytes = b.bytes_ if b.kind == "file" else None
    if (a.kind == "file" and a_text is None) or (b.kind == "file" and b_text is None):
        return _render_binary_change(path, a.mode, b.mode, a_bytes, b_bytes)
    return _render_text_change(path, a.mode, b.mode, a_text, b_text)


def _render_one_sided(path: str, side: _Side, *, present: bool) -> str:
    if side.kind == "symlink":
        return _render_symlink_change(path, side.target if not present else None, side.target if present else None)
    if side.text is None:
        return _render_binary_change(path, side.mode, side.mode, side.bytes_ if not present else None, side.bytes_ if present else None)
    return _render_text_change(path, side.mode, side.mode, side.text if not present else None, side.text if present else None)


class GitWorktreeChangeProvider:
    """ChangeProvider for GitWorktreeWorkspace: snapshot_commit -> current tree.

    Reads baseline content exclusively via `git ls-tree`/`git cat-file -p`
    (immutable object access — no working tree, no filters, no hooks) and
    current content exclusively via direct filesystem I/O (no Git at all).
    No Git command that compares against the live working tree is ever run.
    """

    def capture(self, workspace) -> WorkspaceChangeSet:
        if not isinstance(workspace, GitWorktreeWorkspace):
            raise ChangeInputError(
                "GitWorktreeChangeProvider requires a GitWorktreeWorkspace."
            )
        root = workspace.root
        baseline = workspace.snapshot.snapshot_commit
        object_format = _repo_object_format(root)

        tracked = _ls_tree_baseline(baseline, cwd=root)
        untracked = set(
            _git_paths(["ls-files", "--others", "--exclude-standard", "-z", "--", "."], cwd=root)
        )
        all_paths = sorted(set(tracked) | untracked)

        blocks: list[str] = []
        changed_paths: list[str] = []
        for path in all_paths:
            current, current_bytes = _current_side(root, path)
            baseline_entry = tracked.get(path)

            if baseline_entry is not None and current.kind is not None:
                mode_a, sha_a = baseline_entry
                if (
                    mode_a == current.mode
                    and current_bytes is not None
                    and _git_blob_oid(current_bytes, object_format) == sha_a
                ):
                    continue  # optimization only: skip fetching the baseline blob.
                baseline_side = _baseline_side(root, mode_a, sha_a)
                if _sides_exactly_equal(baseline_side, current):
                    continue  # defensive seam: the oid fast-path is never the sole equality check.
            elif baseline_entry is not None:
                mode_a, sha_a = baseline_entry
                baseline_side = _baseline_side(root, mode_a, sha_a)
            else:
                baseline_side = _Side(kind=None)

            block = _render_pair(path, baseline_side, current)
            if block:
                blocks.append(block)
                changed_paths.append(path)

        diff_text = "\n".join(blocks)
        return WorkspaceChangeSet(diff=diff_text, changed_paths=tuple(changed_paths))
