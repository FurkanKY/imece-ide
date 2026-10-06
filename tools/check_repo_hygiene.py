"""Check local Markdown file links against files tracked by Git."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|([^\s]+))")


def _markdown_links(text: str):
    """Yield (1-based line, destination), ignoring fenced code examples."""
    fence_char = None
    fence_length = 0
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = None, 0
            continue
        if fence_char is not None:
            continue
        for match in INLINE_LINK.finditer(line):
            yield number, match.group(1) or match.group(2)
        reference = REFERENCE.match(line)
        if reference:
            yield number, reference.group(1) or reference.group(2)


def _tracked_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return {item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item}


def check(root: Path, tracked_files: set[str]) -> list[str]:
    """Return actionable failures for local links in tracked Markdown files."""
    root = root.resolve()
    errors: list[str] = []
    for relative_doc in sorted(tracked_files):
        if Path(relative_doc).suffix.lower() != ".md":
            continue
        doc = root / relative_doc
        try:
            doc.resolve().relative_to(root)
        except ValueError:
            errors.append(f"{relative_doc}: tracked Markdown source resolves outside the repository")
            continue
        if not doc.is_file():
            errors.append(f"{relative_doc}: tracked Markdown source is missing or is not a file")
            continue
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            errors.append(f"{relative_doc}: cannot read tracked Markdown source: {error}")
            continue
        for line, raw_target in _markdown_links(text):
            location = f"{relative_doc}:{line}: {raw_target!r}"
            try:
                parsed = urlsplit(raw_target)
            except ValueError as error:
                errors.append(f"{location} has malformed URL syntax: {error}")
                continue
            if parsed.scheme or parsed.netloc or raw_target.startswith("#"):
                continue
            target_text = unquote(parsed.path)
            if not target_text:
                continue
            target = (doc.parent / target_text).resolve()
            try:
                relative_target = target.relative_to(root).as_posix()
            except ValueError:
                errors.append(f"{location} escapes the repository")
                continue
            if target.is_dir():
                prefix = "" if relative_target == "." else relative_target.rstrip("/") + "/"
                if relative_target == "." or any(path.startswith(prefix) for path in tracked_files):
                    continue
                errors.append(f"{location} targets directory {relative_target!r}, which has no tracked files")
                continue
            if relative_target not in tracked_files:
                status = "not tracked" if target.exists() else "missing or not tracked"
                errors.append(f"{location} targets {relative_target!r}, which is {status}")
            elif not target.is_file():
                errors.append(f"{location} targets {relative_target!r}, which does not exist as a file")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        errors = check(root, _tracked_paths(root))
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"repository hygiene check could not run: {error}", file=sys.stderr)
        return 2
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"Found {len(errors)} broken local Markdown link(s).", file=sys.stderr)
        return 1
    print("Tracked Markdown local file links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
