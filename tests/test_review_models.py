import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from review_runtime.errors import ReviewInputError  # noqa: E402
from review_runtime.models import (  # noqa: E402
    ReviewDecision,
    ReviewFinding,
    ReviewReport,
    ReviewRequest,
    ReviewSeverity,
    ReviewVerdict,
    new_review_id,
    validate_review_id,
)
from verification_runtime import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402

FP = "a" * 64


def _process_result(exit_code=0, timed_out=False):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=timed_out, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False,
        stdout_bytes=0, stderr_bytes=0,
    )


def _verification_report():
    return VerificationReport(
        verification_id="ver-1",
        plan_id="plan-1",
        results=(VerificationCheckResult("c1", "Check 1", VerificationStatus.PASS, _process_result()),),
        duration_ms=5,
    )


# ---------------- ReviewFinding ----------------


def test_finding_minimal_valid():
    finding = ReviewFinding(severity=ReviewSeverity.MAJOR, message="A bug")
    assert finding.path is None
    assert finding.start_line is None


def test_finding_rejects_empty_message():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MINOR, message="")


def test_finding_rejects_nul_message():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MINOR, message="bad\x00text")


def test_finding_rejects_oversized_message():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MINOR, message="x" * 2001)


def test_finding_with_full_line_range_requires_path():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", start_line=1, end_line=2)


def test_finding_rejects_one_sided_line_range():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="a.py", start_line=1)
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="a.py", end_line=2)


def test_finding_rejects_end_before_start():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="a.py", start_line=5, end_line=4)


def test_finding_accepts_equal_start_end():
    finding = ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="a.py", start_line=5, end_line=5)
    assert finding.start_line == finding.end_line == 5


def test_finding_rejects_absolute_path():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="/etc/passwd", start_line=1, end_line=1)


def test_finding_rejects_traversal_path():
    with pytest.raises(ReviewInputError):
        ReviewFinding(severity=ReviewSeverity.MAJOR, message="m", path="../x.py", start_line=1, end_line=1)


# ---------------- ReviewDecision ----------------


def test_decision_approved_requires_no_findings():
    ReviewDecision(verdict=ReviewVerdict.APPROVED, summary="ok", findings=())
    with pytest.raises(ReviewInputError):
        ReviewDecision(
            verdict=ReviewVerdict.APPROVED,
            summary="ok",
            findings=(ReviewFinding(ReviewSeverity.MINOR, "nit"),),
        )


def test_decision_needs_fix_requires_at_least_one_finding():
    with pytest.raises(ReviewInputError):
        ReviewDecision(verdict=ReviewVerdict.NEEDS_FIX, summary="bad", findings=())
    ReviewDecision(
        verdict=ReviewVerdict.NEEDS_FIX,
        summary="bad",
        findings=(ReviewFinding(ReviewSeverity.BLOCKER, "boom"),),
    )


def test_decision_rejects_too_many_findings():
    findings = tuple(ReviewFinding(ReviewSeverity.MINOR, f"issue {i}") for i in range(33))
    with pytest.raises(ReviewInputError):
        ReviewDecision(verdict=ReviewVerdict.NEEDS_FIX, summary="bad", findings=findings)


def test_decision_rejects_empty_summary():
    with pytest.raises(ReviewInputError):
        ReviewDecision(verdict=ReviewVerdict.APPROVED, summary="", findings=())


# ---------------- ReviewReport ----------------


def _report(**overrides):
    defaults = dict(
        review_id=new_review_id(),
        verdict=ReviewVerdict.APPROVED,
        summary="ok",
        findings=(),
        repository_fingerprint=FP,
        diff_sha256=FP,
        verification_id=None,
        verification_status=None,
    )
    defaults.update(overrides)
    return ReviewReport(**defaults)


def test_report_requires_valid_review_id():
    with pytest.raises(ReviewInputError):
        _report(review_id="not a valid id!!")


def test_report_requires_sha256_fingerprint_and_diff():
    with pytest.raises(ReviewInputError):
        _report(repository_fingerprint="not-hex")
    with pytest.raises(ReviewInputError):
        _report(diff_sha256="short")


def test_report_enforces_same_verdict_findings_invariant_as_decision():
    with pytest.raises(ReviewInputError):
        _report(
            verdict=ReviewVerdict.APPROVED,
            findings=(ReviewFinding(ReviewSeverity.MINOR, "x"),),
        )
    with pytest.raises(ReviewInputError):
        _report(verdict=ReviewVerdict.NEEDS_FIX, findings=())


def test_report_accepts_verification_provenance():
    report = _report(verification_id="ver-1", verification_status="pass")
    assert report.verification_id == "ver-1"
    assert report.verification_status == "pass"


def test_validate_review_id_accepts_generated_ids():
    review_id = new_review_id()
    assert validate_review_id(review_id) == review_id
    assert review_id.startswith("rev_")


def test_validate_review_id_rejects_invalid():
    with pytest.raises(ReviewInputError):
        validate_review_id("has a space")
    with pytest.raises(ReviewInputError):
        validate_review_id("")


# ---------------- ReviewRequest ----------------


def test_request_minimal_valid():
    request = ReviewRequest(task="do the thing", diff="+line\n")
    assert request.plan is None
    assert request.verification_report is None


def test_request_rejects_nul_in_task_diff_plan():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="bad\x00task", diff="d")
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="t", diff="bad\x00diff")
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="t", diff="d", plan="bad\x00plan")


def test_request_rejects_oversized_task():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="x" * 32_001, diff="d")


def test_request_rejects_oversized_plan():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="t", diff="d", plan="x" * 64_001)


def test_request_rejects_oversized_diff():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="t", diff="x" * 200_001)


def test_request_rejects_empty_task():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="   ", diff="d")


def test_request_allows_empty_diff():
    ReviewRequest(task="t", diff="")


def test_request_diff_sha256_matches_exact_accepted_diff():
    diff_text = "+added line\n-removed line\n"
    request = ReviewRequest(task="t", diff=diff_text)
    assert request.diff_sha256 == hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def test_request_accepts_verification_report_reference():
    report = _verification_report()
    request = ReviewRequest(task="t", diff="d", verification_report=report)
    assert request.verification_report is report


def test_request_rejects_non_verification_report_object():
    with pytest.raises(ReviewInputError):
        ReviewRequest(task="t", diff="d", verification_report={"not": "a report"})
