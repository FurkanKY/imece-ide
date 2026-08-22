"""Deterministic lexical ranking for repository paths, symbols, and content."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from context_runtime.models import RepositoryFile, RepositoryIndex

MAX_QUERY_CHARS = 4_096
MAX_QUERY_TERMS = 32
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PATH_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\b")


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    terms: tuple[str, ...]
    path_references: tuple[str, ...]
    symbol_references: tuple[str, ...]


def query_analysis(query: str) -> QueryAnalysis:
    """Extract bounded code/path references without treating prose as exact symbols."""
    expanded = _CAMEL_RE.sub(" ", query).replace("_", " ").replace("-", " ").replace("/", " ").replace("\\", " ")
    terms = tuple(dict.fromkeys(word.casefold() for word in _WORD_RE.findall(expanded) if word))[:MAX_QUERY_TERMS]
    paths = tuple(dict.fromkeys(match.group(1).replace("\\", "/").casefold() for match in _PATH_REFERENCE_RE.finditer(query)))
    symbols: list[str] = []
    for match in _IDENTIFIER_RE.finditer(query):
        value = match.group(0)
        if "." in value or "_" in value or any(char.isupper() for char in value):
            symbols.append(value.casefold())
    return QueryAnalysis(terms, paths, tuple(dict.fromkeys(symbols)))


def query_terms(query: str) -> tuple[str, ...]:
    return query_analysis(query).terms


@dataclass(frozen=True, slots=True)
class RankedFile:
    file: RepositoryFile
    score: int
    reasons: tuple[str, ...]
    tier: int


def rank_files(index: RepositoryIndex, query: str, content_by_path: dict[str, str]) -> tuple[RankedFile, ...]:
    analysis = query_analysis(query)
    symbols_by_path: dict[str, list] = {}
    for symbol in index.symbols:
        symbols_by_path.setdefault(symbol.path, []).append(symbol)
    ranked: list[RankedFile] = []
    for file in index.files:
        path_folded = file.path.casefold()
        name = PurePosixPath(file.path).name.casefold()
        stem = PurePosixPath(file.path).stem.casefold()
        reasons: list[str] = []
        tier = 0
        if path_folded in analysis.path_references:
            tier = 5
            reasons.append("exact_path")
        symbol_matches = any(
            symbol.name.casefold() in analysis.symbol_references
            or symbol.qualified_name.casefold() in analysis.symbol_references
            for symbol in symbols_by_path.get(file.path, ())
        )
        if symbol_matches:
            tier = max(tier, 4)
            reasons.append("symbol")
        if analysis.terms and (name in analysis.terms or stem in analysis.terms):
            tier = max(tier, 3)
            reasons.append("filename")
        path_matches = sum(term in path_folded for term in analysis.terms)
        if path_matches:
            tier = max(tier, 2)
            reasons.append("path_term")
        content = content_by_path.get(file.path, "").casefold()
        content_matches = sum(content.count(term) for term in analysis.terms)
        if content_matches:
            tier = max(tier, 1)
            reasons.append("content")
        if tier:
            detail = min(999_999, path_matches * 1_000 + min(content_matches, 999))
            ranked.append(RankedFile(file, tier * 1_000_000 + detail, tuple(reasons), tier))
    return tuple(sorted(ranked, key=lambda item: (-item.tier, -item.score, item.file.path)))
