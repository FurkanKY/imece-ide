"""Provider-independent, current-workspace repository intelligence."""

from context_runtime.engine import ContextEngine, render_context_pack
from context_runtime.errors import ContextRuntimeError, ContextScanError, ContextValidationError
from context_runtime.models import (
    ContextBudget, ContextPack, ContextSegment, RepositoryDiagnostics, RepositoryFile,
    RepositoryIndex, RepositoryMap, RepositorySymbol,
)
from context_runtime.rules import (
    MAX_PROJECT_RULES_CHARS, ProjectRules, load_project_rules, render_bounded_rules_block,
)
from context_runtime.scanner import RepositoryScanner, detect_language
from context_runtime.symbols import PythonAstSymbolExtractor, SymbolExtractor

__all__ = [
    "ContextBudget", "ContextEngine", "ContextPack", "ContextRuntimeError", "ContextScanError",
    "ContextSegment", "ContextValidationError", "MAX_PROJECT_RULES_CHARS", "PythonAstSymbolExtractor",
    "ProjectRules", "RepositoryDiagnostics", "RepositoryFile", "RepositoryIndex", "RepositoryMap",
    "RepositoryScanner", "RepositorySymbol", "SymbolExtractor", "detect_language", "load_project_rules",
    "render_bounded_rules_block", "render_context_pack",
]
