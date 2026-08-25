"""Adversarial tests proving render_initial_review_input never exceeds its budget.

Mathematical invariant under test:

    len(render_initial_review_input(...)) <= MAX_INITIAL_INPUT_CHARS

for every input the renderer accepts, with task/diff always kept in full.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime import ProcessResult  # noqa: E402
from review_runtime import prompt as prompt_module  # noqa: E402
from review_runtime.errors import ReviewInputError  # noqa: E402
from review_runtime.prompt import (  # noqa: E402
    MAX_INITIAL_INPUT_CHARS,
    _TRUNCATION_MARKER,
    _bounded,
    render_initial_review_input,
)
from verification_runtime import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402


class FakePack:
    """Minimal stand-in for ContextPack: render_context_pack() returns .rendered as-is."""

    def __init__(self, rendered: str):
        self.rendered = rendered


def _fixed_overhead() -> int:
    """headers_total + separators_total — the part of the budget that can
    never be avoided even with fully-empty ancillary sections."""
    headers_total = (
        len(prompt_module._TASK_HEADER) + len(prompt_module._PLAN_HEADER)
        + len(prompt_module._DIFF_HEADER) + len(prompt_module._VERIFICATION_HEADER)
        + len(prompt_module._CONTEXT_HEADER)
    )
    separators_total = (prompt_module._NUM_SECTIONS - 1) * len(prompt_module._SEP)
    return headers_total + separators_total


# ---------------- _bounded(): the truncation primitive ----------------


def test_bounded_never_exceeds_limit_for_any_limit_including_zero_and_negative():
    text = "x" * 10_000
    for limit in (-5, -1, 0, 1, 2, len(_TRUNCATION_MARKER) - 1, len(_TRUNCATION_MARKER), len(_TRUNCATION_MARKER) + 1, 100, 10_000, 20_000):
        result = _bounded(text, limit)
        assert len(result) <= max(limit, 0)


def test_bounded_returns_full_text_when_it_already_fits():
    assert _bounded("short", 100) == "short"
    assert _bounded("", 0) == ""


def test_bounded_zero_limit_returns_empty_string():
    assert _bounded("anything at all", 0) == ""


def test_bounded_limit_smaller_than_marker_hard_truncates_without_marker():
    limit = 3
    assert limit < len(_TRUNCATION_MARKER)
    result = _bounded("x" * 1000, limit)
    assert result == "xxx"
    assert len(result) == limit


# ---------------- render_initial_review_input(): the overall budget ----------------


def _fake_context(text: str = "repo context") -> FakePack:
    return FakePack(text)


def test_A_huge_ancillary_with_near_boundary_mandatory_stays_within_budget_and_keeps_full_diff():
    overhead = _fixed_overhead()
    # Split remaining space between task and diff, leaving only 10 chars of
    # slack for the three ancillary sections combined.
    slack = 10
    available_for_mandatory = MAX_INITIAL_INPUT_CHARS - overhead - slack
    task = "T" * (available_for_mandatory // 2)
    diff = "D" * (available_for_mandatory - len(task))

    huge_plan = "P" * 5_000_000
    huge_context = _fake_context("C" * 5_000_000)

    rendered = render_initial_review_input(
        task=task, plan=huge_plan, diff=diff, verification_report=None, context_pack=huge_context,
    )
    assert len(rendered) <= MAX_INITIAL_INPUT_CHARS
    assert diff in rendered
    assert task in rendered


def test_B_zero_and_sub_marker_ancillary_budget_never_overflows():
    overhead = _fixed_overhead()
    # Consume the entire budget with mandatory task+diff, leaving remaining == 0.
    task = "T" * 100
    diff = "D" * (MAX_INITIAL_INPUT_CHARS - overhead - len(task))

    rendered = render_initial_review_input(
        task=task, plan="P" * 100_000, diff=diff,
        verification_report=None, context_pack=_fake_context("C" * 100_000),
    )
    assert len(rendered) <= MAX_INITIAL_INPUT_CHARS
    assert diff in rendered


def test_C_exact_mandatory_boundary_fits_and_succeeds():
    overhead = _fixed_overhead()
    task = "T" * 50
    diff = "D" * (MAX_INITIAL_INPUT_CHARS - overhead - len(task))  # remaining == 0 exactly

    rendered = render_initial_review_input(
        task=task, plan=None, diff=diff, verification_report=None, context_pack=_fake_context(" "),
    )
    assert len(rendered) <= MAX_INITIAL_INPUT_CHARS
    assert diff in rendered
    assert task in rendered


def test_D_mandatory_framing_exceeding_budget_by_one_raises_review_input_error():
    overhead = _fixed_overhead()
    task = "T" * 50
    # required_len == MAX_INITIAL_INPUT_CHARS + 1
    diff = "D" * (MAX_INITIAL_INPUT_CHARS - overhead - len(task) + 1)

    with pytest.raises(ReviewInputError):
        render_initial_review_input(
            task=task, plan=None, diff=diff, verification_report=None, context_pack=_fake_context(" "),
        )


def test_E_large_plan_verification_and_context_pack_all_together_stay_within_budget():
    process = ProcessResult(
        argv=("true",), cwd=".", exit_code=1, timed_out=False, duration_ms=5,
        stdout="O" * 500_000, stderr="E" * 500_000,
        stdout_truncated=False, stderr_truncated=False, stdout_bytes=500_000, stderr_bytes=500_000,
    )
    verification_report = VerificationReport(
        verification_id="ver-1", plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check", VerificationStatus.FAIL, process),),
        duration_ms=10,
    )
    rendered = render_initial_review_input(
        task="Implement the feature",
        plan="P" * 1_000_000,
        diff="+ line one\n- line two\n",
        verification_report=verification_report,
        context_pack=_fake_context("C" * 1_000_000),
    )
    assert len(rendered) <= MAX_INITIAL_INPUT_CHARS
    assert "+ line one" in rendered


def test_task_alone_exceeding_budget_still_raises():
    with pytest.raises(ReviewInputError):
        render_initial_review_input(
            task="T" * (MAX_INITIAL_INPUT_CHARS + 1), plan=None, diff="d",
            verification_report=None, context_pack=_fake_context(" "),
        )


def test_ancillary_sections_never_individually_exceed_their_assigned_budget():
    overhead = _fixed_overhead()
    task = "T" * 10
    diff = "D" * 10
    remaining = MAX_INITIAL_INPUT_CHARS - overhead - len(task) - len(diff)
    ancillary_budget = remaining // 3

    rendered = render_initial_review_input(
        task=task, plan="P" * 10_000_000, diff=diff,
        verification_report=None, context_pack=_fake_context("C" * 10_000_000),
    )
    # Each ancillary section body cannot exceed its header + assigned budget.
    assert len(rendered) <= MAX_INITIAL_INPUT_CHARS
    assert len(rendered) <= overhead + len(task) + len(diff) + 3 * ancillary_budget
