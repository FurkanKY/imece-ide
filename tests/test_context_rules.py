"""Unit tests for context_runtime.rules.load_project_rules /
render_bounded_rules_block: discovery order, missing files, truncation,
symlink-escape rejection, and non-UTF-8 handling.
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_runtime.rules import (  # noqa: E402
    MAX_PROJECT_RULES_CHARS,
    RULES_SECTION_HEADER,
    ProjectRules,
    load_project_rules,
    render_bounded_rules_block,
)


def test_no_rule_files_returns_none(tmp_path):
    assert load_project_rules(tmp_path) is None


def test_single_file_is_read_and_hashed(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Use tabs, not spaces.", encoding="utf-8")
    rules = load_project_rules(tmp_path)
    assert rules is not None
    assert "AGENTS.md" in rules.text
    assert "Use tabs, not spaces." in rules.text
    assert rules.sources == ("AGENTS.md",)
    assert not rules.truncated
    expected_sha = hashlib.sha256(rules.text.encode("utf-8")).hexdigest()
    # No truncation happened, so the reported sha matches the exact rendered text.
    assert rules.sha256 == expected_sha


def test_precedence_order_and_concatenation(tmp_path):
    imece_dir = tmp_path / ".imece"
    imece_dir.mkdir()
    (imece_dir / "rules.md").write_text("rule A", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("rule B", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("rule C", encoding="utf-8")

    rules = load_project_rules(tmp_path)
    assert rules is not None
    assert rules.sources == (".imece/rules.md", "AGENTS.md", "CLAUDE.md")
    # Concatenation preserves the fixed precedence order.
    assert rules.text.index("rule A") < rules.text.index("rule B") < rules.text.index("rule C")


def test_missing_files_are_silently_skipped(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("only this one exists", encoding="utf-8")
    rules = load_project_rules(tmp_path)
    assert rules is not None
    assert rules.sources == ("CLAUDE.md",)


def test_no_recursion_into_subdirectories(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("nested, must not be found", encoding="utf-8")
    assert load_project_rules(tmp_path) is None


def test_truncation_is_explicit_and_bounded(tmp_path):
    (tmp_path / "AGENTS.md").write_text("X" * (MAX_PROJECT_RULES_CHARS * 2), encoding="utf-8")
    rules = load_project_rules(tmp_path)
    assert rules is not None
    assert rules.truncated is True
    assert len(rules.text) <= MAX_PROJECT_RULES_CHARS
    assert "truncated" in rules.text.lower()
    # sha256 identifies the exact on-disk (pre-truncation) content.
    assert rules.sha256 != hashlib.sha256(rules.text.encode("utf-8")).hexdigest()


def test_symlink_escaping_root_is_rejected(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.md"
    outside.write_text("secret instructions from outside the repo", encoding="utf-8")
    try:
        (tmp_path / "AGENTS.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported in this environment")
    rules = load_project_rules(tmp_path)
    # The symlink is rejected outright (never followed), so with no other
    # rule file present this must be None, and the outside content must
    # never appear anywhere.
    assert rules is None


def test_non_utf8_bytes_are_replaced_not_raised(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"valid text \xff\xfe more text")
    rules = load_project_rules(tmp_path)
    assert rules is not None
    assert "valid text" in rules.text
    assert "more text" in rules.text


# ---------------- render_bounded_rules_block ----------------


def test_render_bounded_rules_block_none_is_empty():
    assert render_bounded_rules_block(None, 10_000) == ""


def test_render_bounded_rules_block_zero_budget_is_empty():
    rules = ProjectRules(text="anything", sha256="x" * 64, truncated=False, sources=("AGENTS.md",))
    assert render_bounded_rules_block(rules, 0) == ""
    assert render_bounded_rules_block(rules, -5) == ""


def test_render_bounded_rules_block_contains_header_and_trust_language():
    rules = ProjectRules(text="Always write tests first.", sha256="x" * 64, truncated=False, sources=("AGENTS.md",))
    block = render_bounded_rules_block(rules, 10_000)
    assert RULES_SECTION_HEADER in block
    assert "Always write tests first." in block
    assert "untrusted" in block.lower()


def test_render_bounded_rules_block_never_exceeds_budget():
    rules = ProjectRules(text="Y" * 5_000, sha256="x" * 64, truncated=False, sources=("AGENTS.md",))
    for budget in (1, 10, 100, 1_000, 4_999, 5_000, 6_000):
        block = render_bounded_rules_block(rules, budget)
        assert len(block) <= budget
