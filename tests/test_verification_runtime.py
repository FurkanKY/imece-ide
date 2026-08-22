import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runtime import ProcessRequest, ProcessResult  # noqa: E402
from process_runtime.errors import ProcessSpawnError  # noqa: E402
from verification_runtime import (  # noqa: E402
    VerificationCheck,
    VerificationCheckResult,
    VerificationCheckCompleted,
    VerificationCheckFailed,
    VerificationCheckStarted,
    VerificationCompleted,
    VerificationInputError,
    VerificationPlan,
    VerificationRunner,
    VerificationStarted,
    VerificationStatus,
    classify,
)
from verification_runtime.errors import VerificationRecordingError  # noqa: E402


def result(*, exit_code=0, timed_out=False):
    return ProcessResult(
        argv=(sys.executable,), cwd=".", exit_code=exit_code, timed_out=timed_out,
        duration_ms=1, stdout="out", stderr="err", stdout_truncated=False,
        stderr_truncated=False, stdout_bytes=3, stderr_bytes=3,
    )


def check(check_id="check-a", *, timeout_ms=100):
    return VerificationCheck(
        check_id=check_id,
        name=f"Name {check_id}",
        request=ProcessRequest((sys.executable, "-c", "pass"), timeout_ms=timeout_ms),
        pass_exit_codes=(0,),
        error_exit_codes=(2, 3),
    )


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, workspace, request):
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingSink:
    def __init__(self, fail_on=None):
        self.events = []
        self.fail_on = fail_on

    def emit(self, event):
        self.events.append(event)
        if self.fail_on is not None and isinstance(event, self.fail_on):
            raise RuntimeError("recording failed")


def test_classifier_is_explicit_and_deterministic():
    item = check()
    assert classify(item, result(exit_code=0)) is VerificationStatus.PASS
    assert classify(item, result(exit_code=1)) is VerificationStatus.FAIL
    assert classify(item, result(exit_code=2)) is VerificationStatus.ERROR
    assert classify(item, result(exit_code=3)) is VerificationStatus.ERROR
    assert classify(item, result(exit_code=7)) is VerificationStatus.FAIL
    assert classify(item, result(exit_code=-9)) is VerificationStatus.ERROR
    assert classify(item, result(exit_code=None)) is VerificationStatus.ERROR
    assert classify(item, result(exit_code=0, timed_out=True)) is VerificationStatus.TIMEOUT


def test_plan_and_check_validation_is_bounded_and_immutable():
    with pytest.raises(VerificationInputError):
        VerificationPlan("plan", ())
    with pytest.raises(VerificationInputError):
        VerificationPlan("plan", (check("same"), check("same")))
    with pytest.raises(VerificationInputError):
        VerificationCheck("bad id", "name", check().request)
    with pytest.raises(VerificationInputError):
        VerificationCheck("bad", "name", check().request, pass_exit_codes=(True,))
    with pytest.raises(VerificationInputError):
        VerificationCheck("bad", "name", check().request, pass_exit_codes=(0,), error_exit_codes=(0,))
    with pytest.raises(VerificationInputError):
        VerificationPlan("plan", tuple(check(str(i), timeout_ms=120_001) for i in range(16)))
    with pytest.raises(VerificationInputError):
        VerificationPlan("plan", tuple(check(str(i)) for i in range(17)))

    checks = [check("b"), check("a")]
    plan = VerificationPlan("plan", checks)
    checks.append(check("c"))
    assert plan.checks == (checks[0], checks[1])
    normalized = VerificationCheck(
        "codes", "codes", check().request, pass_exit_codes=(3, 0, 3), error_exit_codes=(9, 8)
    )
    assert normalized.pass_exit_codes == (0, 3)
    assert normalized.error_exit_codes == (8, 9)


def test_check_result_enforces_process_and_infrastructure_error_forms():
    process = result(exit_code=7)
    timed = result(exit_code=0, timed_out=True)
    assert VerificationCheckResult("p", "p", VerificationStatus.PASS, process).process_result is process
    assert VerificationCheckResult("f", "f", VerificationStatus.FAIL, process).status is VerificationStatus.FAIL
    assert VerificationCheckResult("t", "t", VerificationStatus.TIMEOUT, timed).status is VerificationStatus.TIMEOUT
    derived_error = VerificationCheckResult("e", "e", VerificationStatus.ERROR, process)
    assert derived_error.error_type is None
    infrastructure = VerificationCheckResult(
        "i", "i", VerificationStatus.ERROR,
        error_type="ProcessSpawnError", error_message="missing executable",
    )
    assert infrastructure.process_result is None

    invalid = [
        dict(status=VerificationStatus.PASS, process_result=None),
        dict(status=VerificationStatus.PASS, process_result=process, error_type="bad"),
        dict(status=VerificationStatus.TIMEOUT, process_result=process),
        dict(status=VerificationStatus.TIMEOUT, process_result=timed, error_message="bad"),
        dict(status=VerificationStatus.ERROR),
        dict(status=VerificationStatus.ERROR, process_result=timed),
        dict(status=VerificationStatus.ERROR, process_result=process, error_type="bad"),
        dict(status=VerificationStatus.ERROR, error_type=None, error_message="missing type"),
        dict(status=VerificationStatus.ERROR, error_type="x" * 257, error_message="message"),
        dict(status=VerificationStatus.ERROR, error_type="bad\x00type", error_message="message"),
    ]
    for values in invalid:
        with pytest.raises(VerificationInputError):
            VerificationCheckResult("bad", "bad", **values)
    sanitized = VerificationCheckResult(
        "sanitized", "sanitized", VerificationStatus.ERROR,
        error_type="Error", error_message="bad\x00message",
    )
    assert sanitized.error_message == "badmessage"


def test_runner_executes_all_checks_in_order_and_derives_pass():
    runner = FakeRunner([result(), result()])
    sink = RecordingSink()
    plan = VerificationPlan("plan", (check("a"), check("b")))
    report = VerificationRunner(runner, sink).run(object(), plan, verification_id="ver_test")
    assert [request for request in runner.calls] == [plan.checks[0].request, plan.checks[1].request]
    assert report.status is VerificationStatus.PASS
    assert (report.passed, report.failed, report.timed_out, report.errors, report.total) == (2, 0, 0, 0, 2)
    assert [type(event) for event in sink.events] == [
        VerificationStarted,
        VerificationCheckStarted,
        VerificationCheckCompleted,
        VerificationCheckStarted,
        VerificationCheckCompleted,
        VerificationCompleted,
    ]


def test_runner_is_not_fail_fast_and_error_precedes_timeout():
    runner = FakeRunner([result(), result(exit_code=1), result(exit_code=0, timed_out=True), ProcessSpawnError("bad")])
    plan = VerificationPlan("plan", tuple(check(str(i)) for i in range(4)))
    report = VerificationRunner(runner).run(object(), plan, verification_id="ver_mix")
    assert len(runner.calls) == 4
    assert report.status is VerificationStatus.ERROR
    assert (report.passed, report.failed, report.timed_out, report.errors) == (1, 1, 1, 1)
    error_result = report.results[-1]
    assert error_result.process_result is None
    assert error_result.error_type == "ProcessSpawnError"
    assert "traceback" not in (error_result.error_message or "").lower()


def test_recording_failure_before_side_effect_blocks_runner():
    runner = FakeRunner([result()])
    sink = RecordingSink(fail_on=VerificationCheckStarted)
    with pytest.raises(VerificationRecordingError):
        VerificationRunner(runner, sink).run(
            object(), VerificationPlan("plan", (check(),)), verification_id="ver_blocked"
        )
    assert runner.calls == []


def test_recording_failure_after_side_effect_does_not_retry_or_complete():
    runner = FakeRunner([result()])
    sink = RecordingSink(fail_on=VerificationCheckCompleted)
    with pytest.raises(VerificationRecordingError):
        VerificationRunner(runner, sink).run(
            object(), VerificationPlan("plan", (check(),)), verification_id="ver_after"
        )
    assert len(runner.calls) == 1
    assert not any(isinstance(event, VerificationCompleted) for event in sink.events)


def test_real_process_runner_pass_fail_and_timeout(tmp_path):
    from process_runtime import ProcessRunner  # noqa: E402
    from workspace.local import LocalWorkspace  # noqa: E402

    workspace = LocalWorkspace(tmp_path)
    plan = VerificationPlan("real", (
        VerificationCheck("pass", "pass", ProcessRequest((sys.executable, "-c", "print('ok')"))),
        VerificationCheck("fail", "fail", ProcessRequest((sys.executable, "-c", "import sys; sys.exit(7)"))),
        VerificationCheck("timeout", "timeout", ProcessRequest((sys.executable, "-c", "import time; time.sleep(2)"), timeout_ms=100)),
    ))
    report = VerificationRunner(ProcessRunner()).run(workspace, plan, verification_id="ver_real")
    assert [item.status for item in report.results] == [
        VerificationStatus.PASS, VerificationStatus.FAIL, VerificationStatus.TIMEOUT,
    ]
    assert report.status is VerificationStatus.TIMEOUT
