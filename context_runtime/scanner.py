"""Deterministic, Workspace-backed repository scanning."""

from __future__ import annotations

import hashlib
import json
from itertools import islice
from pathlib import PurePosixPath
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from context_runtime.models import (
    RepositoryDiagnostics,
    RepositoryFile,
    RepositoryIndex,
    RepositorySymbol,
)
from context_runtime.symbols import PythonAstSymbolExtractor
from workspace.errors import WorkspaceError

_EXCLUDED_DIRS = frozenset({
    ".git", ".imece", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "coverage", "target",
})
_BINARY_SUFFIXES = frozenset({
    ".7z", ".bmp", ".class", ".dll", ".dylib", ".exe", ".gif", ".gz", ".ico", ".jar",
    ".jpeg", ".jpg", ".lock", ".mov", ".mp3", ".mp4", ".o", ".pdf", ".png", ".pyc",
    ".so", ".tar", ".ttf", ".woff", ".woff2", ".zip",
})
_LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rs": "rust", ".go": "go", ".java": "java", ".md": "markdown",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sh": "shell", ".bash": "shell",
}


def detect_language(path: str) -> str:
    return _LANGUAGES.get(PurePosixPath(path).suffix.casefold(), "text")


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Private build snapshot: index metadata and exact texts from the same reads."""

    index: RepositoryIndex
    content_by_path: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_by_path", MappingProxyType(dict(self.content_by_path)))


class RepositoryScanner:
    """Ephemeral scanner; every scan observes the workspace's current state."""

    def __init__(self, *, max_files: int = 2_000, max_file_chars: int = 250_000, max_total_chars: int = 4_000_000) -> None:
        if any(type(value) is not int or value <= 0 for value in (max_files, max_file_chars, max_total_chars)):
            raise ValueError("RepositoryScanner limits must be positive integers.")
        self._max_files = max_files
        self._max_file_chars = max_file_chars
        self._max_total_chars = max_total_chars
        self._python = PythonAstSymbolExtractor()

    def scan(self, workspace) -> RepositoryIndex:
        return self.scan_snapshot(workspace).index

    def scan_snapshot(self, workspace) -> RepositorySnapshot:
        files: list[RepositoryFile] = []
        symbols: list[RepositorySymbol] = []
        content_by_path: dict[str, str] = {}
        skipped_unreadable = skipped_binary = skipped_oversize = symbol_parse_failures = 0
        total_processed = 0
        bounded_paths = list(islice(workspace.iter_files(".", excluded_dirs=_EXCLUDED_DIRS), self._max_files + 1))
        file_limit_reached = len(bounded_paths) > self._max_files
        paths = sorted(bounded_paths[:self._max_files])
        considered = len(paths)
        for path in paths:
            suffix = PurePosixPath(path).suffix.casefold()
            if suffix in _BINARY_SUFFIXES:
                skipped_binary += 1
                continue
            try:
                content = workspace.read_text(path)
            except (UnicodeError, OSError, WorkspaceError):
                skipped_unreadable += 1
                continue
            if "\x00" in content:
                skipped_binary += 1
                continue
            if len(content) > self._max_file_chars or total_processed + len(content) > self._max_total_chars:
                skipped_oversize += 1
                continue
            total_processed += len(content)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            language = detect_language(path)
            files.append(RepositoryFile(path, language, len(content), len(content.splitlines()), digest))
            content_by_path[path] = content
            if language == "python":
                try:
                    symbols.extend(self._python.extract(path, content))
                except SyntaxError:
                    symbol_parse_failures += 1
        diagnostics = RepositoryDiagnostics(
            skipped_unreadable=skipped_unreadable,
            skipped_binary=skipped_binary,
            skipped_oversize=skipped_oversize,
            symbol_parse_failures=symbol_parse_failures,
            files_considered=considered,
            symbols_found=len(symbols),
            file_limit_reached=file_limit_reached,
        )
        files_tuple = tuple(sorted(files, key=lambda item: item.path))
        fingerprint_payload = [(item.path, item.content_sha256) for item in files_tuple]
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        index = RepositoryIndex(
            files=files_tuple,
            symbols=tuple(sorted(symbols, key=lambda item: (item.path, item.start_line, item.qualified_name))),
            fingerprint=fingerprint,
            diagnostics=diagnostics,
        )
        return RepositorySnapshot(index=index, content_by_path=content_by_path)
