"""Language-specific, non-executing symbol extractors."""

from __future__ import annotations

import ast
from typing import Protocol

from context_runtime.models import RepositorySymbol


class SymbolExtractor(Protocol):
    def extract(self, path: str, content: str) -> tuple[RepositorySymbol, ...]: ...


class PythonAstSymbolExtractor:
    """Extract module classes/functions and direct class methods using stdlib AST."""

    def extract(self, path: str, content: str) -> tuple[RepositorySymbol, ...]:
        module = ast.parse(content, filename=path)
        symbols: list[RepositorySymbol] = []
        for node in module.body:
            if isinstance(node, ast.ClassDef):
                symbols.append(self._symbol(path, node.name, node.name, "class", node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                        symbols.append(self._symbol(path, child.name, f"{node.name}.{child.name}", kind, child))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                symbols.append(self._symbol(path, node.name, node.name, kind, node))
        return tuple(sorted(symbols, key=lambda item: (item.path, item.start_line, item.qualified_name)))

    @staticmethod
    def _symbol(path: str, name: str, qualified_name: str, kind: str, node: ast.AST) -> RepositorySymbol:
        return RepositorySymbol(
            path=path,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            start_line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
        )
