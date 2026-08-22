"""Immutable, provider-neutral repository context contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from context_runtime.errors import ContextValidationError
from workspace.base import normalize_workspace_relative_path
from workspace.errors import WorkspaceBoundaryError

_MAX_BUDGET = 200_000
_MAX_IDENTIFIER = 256
MIN_CONTEXT_TOTAL_CHARS = 128
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _non_empty(value: str, field: str, *, limit: int = _MAX_IDENTIFIER) -> None:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ContextValidationError(f"{field} must be a non-empty bounded NUL-free string.")


def _positive_int(value: int, field: str, *, limit: int = _MAX_BUDGET) -> None:
    if type(value) is not int or value <= 0 or value > limit:
        raise ContextValidationError(f"{field} must be a positive integer no greater than {limit}.")


def _repository_path(value: str, field: str) -> str:
    try:
        normalized = normalize_workspace_relative_path(value, allow_root=False)
    except WorkspaceBoundaryError as exc:
        raise ContextValidationError(f"{field} must be a normalized workspace-relative file path.") from exc
    if normalized != value:
        raise ContextValidationError(f"{field} must already be normalized with forward slashes.")
    return normalized


def _sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContextValidationError(f"{field} must be a lowercase SHA-256 hex digest.")


@dataclass(frozen=True, slots=True)
class ContextBudget:
    total_chars: int = 24_000
    map_chars: int = 6_000
    max_segment_chars: int = 6_000

    def __post_init__(self) -> None:
        _positive_int(self.total_chars, "ContextBudget.total_chars")
        if self.total_chars < MIN_CONTEXT_TOTAL_CHARS:
            raise ContextValidationError(
                f"ContextBudget.total_chars must be at least {MIN_CONTEXT_TOTAL_CHARS} for mandatory framing."
            )
        _positive_int(self.map_chars, "ContextBudget.map_chars")
        _positive_int(self.max_segment_chars, "ContextBudget.max_segment_chars")
        if self.map_chars > self.total_chars or self.max_segment_chars > self.total_chars:
            raise ContextValidationError("Context budget parts cannot exceed total_chars.")


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    language: str
    char_count: int
    line_count: int
    content_sha256: str

    def __post_init__(self) -> None:
        _repository_path(self.path, "RepositoryFile.path")
        _non_empty(self.language, "RepositoryFile.language")
        for field in ("char_count", "line_count"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ContextValidationError(f"RepositoryFile.{field} must be a non-negative integer.")
        _sha256(self.content_sha256, "RepositoryFile.content_sha256")


@dataclass(frozen=True, slots=True)
class RepositorySymbol:
    path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        for field in ("path", "name", "qualified_name", "kind"):
            _non_empty(getattr(self, field), f"RepositorySymbol.{field}", limit=1024)
        _repository_path(self.path, "RepositorySymbol.path")
        if type(self.start_line) is not int or type(self.end_line) is not int or self.start_line < 1 or self.end_line < self.start_line:
            raise ContextValidationError("RepositorySymbol line range is invalid.")


@dataclass(frozen=True, slots=True)
class RepositoryDiagnostics:
    skipped_unreadable: int = 0
    skipped_binary: int = 0
    skipped_oversize: int = 0
    symbol_parse_failures: int = 0
    files_considered: int = 0
    symbols_found: int = 0
    file_limit_reached: bool = False

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if field == "file_limit_reached":
                if type(value) is not bool:
                    raise ContextValidationError("RepositoryDiagnostics.file_limit_reached must be boolean.")
                continue
            if type(value) is not int or value < 0:
                raise ContextValidationError(f"RepositoryDiagnostics.{field} must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class RepositoryIndex:
    files: tuple[RepositoryFile, ...]
    symbols: tuple[RepositorySymbol, ...]
    fingerprint: str
    diagnostics: RepositoryDiagnostics

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if tuple(sorted(self.files, key=lambda item: item.path)) != self.files:
            raise ContextValidationError("RepositoryIndex.files must be path sorted.")
        if tuple(sorted(self.symbols, key=lambda item: (item.path, item.start_line, item.qualified_name))) != self.symbols:
            raise ContextValidationError("RepositoryIndex.symbols must be deterministically sorted.")
        if len({item.path for item in self.files}) != len(self.files):
            raise ContextValidationError("RepositoryIndex contains duplicate file paths.")
        _sha256(self.fingerprint, "RepositoryIndex.fingerprint")
        if not isinstance(self.diagnostics, RepositoryDiagnostics):
            raise ContextValidationError("RepositoryIndex.diagnostics must be RepositoryDiagnostics.")


@dataclass(frozen=True, slots=True)
class ContextSegment:
    path: str
    start_line: int
    end_line: int
    text: str
    score: int
    reasons: tuple[str, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        _repository_path(self.path, "ContextSegment.path")
        if type(self.start_line) is not int or type(self.end_line) is not int or self.start_line < 1 or self.end_line < self.start_line:
            raise ContextValidationError("ContextSegment line range is invalid.")
        if not isinstance(self.text, str) or "\x00" in self.text:
            raise ContextValidationError("ContextSegment.text must be NUL-free text.")
        if type(self.score) is not int:
            raise ContextValidationError("ContextSegment.score must be an integer.")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if not all(isinstance(reason, str) and reason for reason in self.reasons):
            raise ContextValidationError("ContextSegment.reasons must be non-empty strings.")
        _sha256(self.content_sha256, "ContextSegment.content_sha256")


@dataclass(frozen=True, slots=True)
class ContextPack:
    query: str
    repository_fingerprint: str
    repo_map: str
    segments: tuple[ContextSegment, ...]
    used_chars: int
    truncated: bool
    diagnostics: RepositoryDiagnostics
    rendered: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ContextValidationError("ContextPack.query must be a string.")
        _sha256(self.repository_fingerprint, "ContextPack.repository_fingerprint")
        if not isinstance(self.repo_map, str) or "\x00" in self.repo_map:
            raise ContextValidationError("ContextPack.repo_map must be NUL-free text.")
        object.__setattr__(self, "segments", tuple(self.segments))
        if type(self.used_chars) is not int or self.used_chars < 0:
            raise ContextValidationError("ContextPack.used_chars must be a non-negative integer.")
        if type(self.truncated) is not bool or not isinstance(self.diagnostics, RepositoryDiagnostics):
            raise ContextValidationError("ContextPack fields are invalid.")
        if not isinstance(self.rendered, str) or "\x00" in self.rendered:
            raise ContextValidationError("ContextPack.rendered must be NUL-free text.")

    @property
    def metadata(self) -> Mapping[str, int | bool | str]:
        return {
            "repository_fingerprint": self.repository_fingerprint,
            "files_considered": self.diagnostics.files_considered,
            "symbols_found": self.diagnostics.symbols_found,
            "segments_returned": len(self.segments),
            "used_chars": self.used_chars,
            "truncated": self.truncated,
            "skipped_unreadable": self.diagnostics.skipped_unreadable,
            "skipped_binary": self.diagnostics.skipped_binary,
            "skipped_oversize": self.diagnostics.skipped_oversize,
        }


@dataclass(frozen=True, slots=True)
class RepositoryMap:
    """Map-only result with accounting independent from ContextPack rendering."""

    query: str
    repository_fingerprint: str
    text: str
    used_chars: int
    truncated: bool
    diagnostics: RepositoryDiagnostics

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or "\x00" in self.query:
            raise ContextValidationError("RepositoryMap.query must be NUL-free text.")
        _sha256(self.repository_fingerprint, "RepositoryMap.repository_fingerprint")
        if not isinstance(self.text, str) or "\x00" in self.text:
            raise ContextValidationError("RepositoryMap.text must be NUL-free text.")
        if type(self.used_chars) is not int or self.used_chars != len(self.text):
            raise ContextValidationError("RepositoryMap.used_chars must equal the map text length.")
        if type(self.truncated) is not bool or not isinstance(self.diagnostics, RepositoryDiagnostics):
            raise ContextValidationError("RepositoryMap fields are invalid.")
