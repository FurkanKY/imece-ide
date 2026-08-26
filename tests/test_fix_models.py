import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fix_runtime.errors import FixLoopInputError  # noqa: E402
from fix_runtime.models import (  # noqa: E402
    DEFAULT_MAX_FIX_ATTEMPTS,
    MAX_MAX_FIX_ATTEMPTS,
    MIN_MAX_FIX_ATTEMPTS,
    FixLoopReport,
    FixLoopRequest,
    FixLoopStatus,
    FixTrigger,
    FixTriggerKind,
    FixWorkerRequest,
    new_fix_attempt_id,
    new_fix_execution_id,
    new_fix_loop_id,
    validate_fix_loop_id,
)
from process_runtime import ProcessResult  # noqa: E402
from review_runtime.models import ReviewReport, ReviewVerdict  # noqa: E402
from verification_runtime import VerificationCheck, VerificationPlan  # noqa: E402
from verification_runtime.models import VerificationCheckResult, VerificationReport, VerificationStatus  # noqa: E402
from process_runtime import ProcessRequest  # noqa: E402


def _process_result(exit_code=0):
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=exit_code, timed_out=False, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _timed_out_process_result():
    return ProcessResult(
        argv=("true",), cwd=".", exit_code=-1, timed_out=True, duration_ms=1,
        stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, stdout_bytes=0, stderr_bytes=0,
    )


def _verification(status, verification_id="ver-1"):
    if status is VerificationStatus.TIMEOUT:
        result = VerificationCheckResult("c1", "Check", status, _timed_out_process_result())
    elif status is VerificationStatus.ERROR:
        result = VerificationCheckResult("c1", "Check", status, error_type="Boom", error_message="infra error")
    else:
        result = VerificationCheckResult("c1", "Check", status, _process_result(0 if status == VerificationStatus.PASS else 1))
    return VerificationReport(
        verification_id=verification_id, plan_id="plan-1", results=(result,), duration_ms=1,
    )


def _review(verdict, verification_id="ver-1", verification_status="pass", findings=()):
    return ReviewReport(
        review_id="rev-1", verdict=verdict, summary="s", findings=findings,
        repository_fingerprint="a" * 64, diff_sha256="b" * 64,
        verification_id=verification_id, verification_status=verification_status,
    )


def _finding():
    from review_runtime.models import ReviewFinding, ReviewSeverity
    return (ReviewFinding(ReviewSeverity.MAJOR, "bug"),)


# ---------------- FixTrigger ----------------


def test_verification_fail_trigger_valid():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    assert trigger.review_report is None


def test_verification_fail_trigger_rejects_non_fail_status():
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.PASS))


def test_verification_fail_trigger_rejects_review_report():
    with pytest.raises(FixLoopInputError):
        FixTrigger(
            kind=FixTriggerKind.VERIFICATION_FAIL,
            verification_report=_verification(VerificationStatus.FAIL),
            review_report=_review(ReviewVerdict.NEEDS_FIX, findings=_finding()),
        )


def test_review_needs_fix_trigger_valid():
    verification = _verification(VerificationStatus.PASS)
    review = _review(ReviewVerdict.NEEDS_FIX, verification_id=verification.verification_id, findings=_finding())
    trigger = FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=verification, review_report=review)
    assert trigger.review_report is review


def test_review_needs_fix_trigger_requires_pass_verification():
    verification = _verification(VerificationStatus.FAIL)
    review = _review(ReviewVerdict.NEEDS_FIX, verification_id=verification.verification_id, findings=_finding())
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=verification, review_report=review)


def test_review_needs_fix_trigger_requires_review_report():
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=_verification(VerificationStatus.PASS))


def test_review_needs_fix_trigger_requires_needs_fix_verdict():
    verification = _verification(VerificationStatus.PASS)
    review = _review(ReviewVerdict.APPROVED, verification_id=verification.verification_id)
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=verification, review_report=review)


def test_review_needs_fix_trigger_requires_matching_verification_id():
    verification = _verification(VerificationStatus.PASS, verification_id="ver-1")
    review = _review(ReviewVerdict.NEEDS_FIX, verification_id="ver-DIFFERENT", findings=_finding())
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=verification, review_report=review)


def test_review_needs_fix_trigger_requires_verification_status_pass_string():
    verification = _verification(VerificationStatus.PASS)
    review = _review(
        ReviewVerdict.NEEDS_FIX, verification_id=verification.verification_id,
        verification_status="fail", findings=_finding(),
    )
    with pytest.raises(FixLoopInputError):
        FixTrigger(kind=FixTriggerKind.REVIEW_NEEDS_FIX, verification_report=verification, review_report=review)


def test_timeout_and_error_are_not_valid_initial_triggers():
    for status in (VerificationStatus.TIMEOUT, VerificationStatus.ERROR):
        with pytest.raises(FixLoopInputError):
            FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(status))


# ---------------- FixWorkerRequest / FixLoopRequest ----------------


def _valid_verification_plan():
    return VerificationPlan("plan-1", (VerificationCheck("c1", "Check", ProcessRequest(("true",))),))


def test_fix_worker_request_valid():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    request = FixWorkerRequest(task="fix it", trigger=trigger, attempt_index=1, rendered_input="ORIGINAL USER TASK\nfix it")
    assert request.plan is None
    assert request.rendered_input == "ORIGINAL USER TASK\nfix it"


def test_fix_worker_request_rejects_zero_attempt_index():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixWorkerRequest(task="fix it", trigger=trigger, attempt_index=0, rendered_input="x")


def test_fix_worker_request_rejects_empty_task():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixWorkerRequest(task="   ", trigger=trigger, attempt_index=1, rendered_input="x")


def test_fix_worker_request_rejects_empty_rendered_input():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixWorkerRequest(task="fix it", trigger=trigger, attempt_index=1, rendered_input="   ")


def test_fix_worker_request_rejects_oversized_rendered_input():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixWorkerRequest(task="fix it", trigger=trigger, attempt_index=1, rendered_input="x" * 100_000)


def test_fix_loop_request_default_max_attempts():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    request = FixLoopRequest(task="t", trigger=trigger, verification_plan=_valid_verification_plan())
    assert request.max_fix_attempts == DEFAULT_MAX_FIX_ATTEMPTS == 2


@pytest.mark.parametrize("value", [0, -1, 6, True, False, 1.5, "2"])
def test_fix_loop_request_rejects_invalid_max_fix_attempts(value):
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixLoopRequest(task="t", trigger=trigger, verification_plan=_valid_verification_plan(), max_fix_attempts=value)


@pytest.mark.parametrize("value", [MIN_MAX_FIX_ATTEMPTS, MAX_MAX_FIX_ATTEMPTS])
def test_fix_loop_request_accepts_boundary_max_fix_attempts(value):
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    request = FixLoopRequest(task="t", trigger=trigger, verification_plan=_valid_verification_plan(), max_fix_attempts=value)
    assert request.max_fix_attempts == value


def test_fix_loop_request_rejects_oversized_task():
    trigger = FixTrigger(kind=FixTriggerKind.VERIFICATION_FAIL, verification_report=_verification(VerificationStatus.FAIL))
    with pytest.raises(FixLoopInputError):
        FixLoopRequest(task="x" * 32_001, trigger=trigger, verification_plan=_valid_verification_plan())


# ---------------- FixLoopReport ----------------


def test_fix_loop_report_valid():
    report = FixLoopReport(
        fix_loop_id=new_fix_loop_id(), status=FixLoopStatus.COMPLETED, attempts_used=1, reason="reviewed",
    )
    assert report.status is FixLoopStatus.COMPLETED


def test_fix_loop_report_rejects_negative_attempts_used():
    with pytest.raises(FixLoopInputError):
        FixLoopReport(fix_loop_id=new_fix_loop_id(), status=FixLoopStatus.COMPLETED, attempts_used=-1, reason="x")


def test_fix_loop_report_rejects_empty_reason():
    with pytest.raises(FixLoopInputError):
        FixLoopReport(fix_loop_id=new_fix_loop_id(), status=FixLoopStatus.COMPLETED, attempts_used=1, reason="")


# ---------------- IDs ----------------


def test_id_generators_are_fresh_and_prefixed():
    assert new_fix_loop_id().startswith("fix_")
    assert new_fix_attempt_id().startswith("fixatt_")
    assert new_fix_execution_id().startswith("exec_fix_")
    assert new_fix_loop_id() != new_fix_loop_id()
    assert new_fix_execution_id() != new_fix_execution_id()


def test_validate_fix_loop_id_rejects_invalid():
    with pytest.raises(FixLoopInputError):
        validate_fix_loop_id("")
    with pytest.raises(FixLoopInputError):
        validate_fix_loop_id("has a space")
    with pytest.raises(FixLoopInputError):
        validate_fix_loop_id("x" * 129)
