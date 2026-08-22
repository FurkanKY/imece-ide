"""Provider-independent deterministic verification contracts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from process_runtime.models import ProcessRequest, ProcessResult
from verification_runtime.errors import VerificationInputError

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_ID_LENGTH = 128
_MAX_NAME_LENGTH = 512
_MAX_CHECKS = 16
_MAX_TOTAL_TIMEOUT_MS = 1_800_000
_MAX_ERROR_TYPE = 256
_MAX_ERROR_MESSAGE = 2000


def _id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _ID_RE.fullmatch(value) is None
    ):
        raise VerificationInputError(f"{field} must be a bounded stable identifier")
    return value


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_NAME_LENGTH:
        raise VerificationInputError(f"{field} must be a non-empty bounded string")
    return value


def _exit_codes(value: Any, field: str, *, required: bool) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise VerificationInputError(f"{field} must be a sequence of integers")
    try:
        values = tuple(value)
    except TypeError as exc:
        raise VerificationInputError(f"{field} must be a sequence of integers") from exc
    if required and not values:
        raise VerificationInputError(f"{field} must not be empty")
    if any(type(code) is not int for code in values):
        raise VerificationInputError(f"{field} must contain real integers")
    return tuple(sorted(set(values)))


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    check_id: str
    name: str
    request: ProcessRequest
    pass_exit_codes: tuple[int, ...] = (0,)
    error_exit_codes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        check_id = _id(self.check_id, "VerificationCheck.check_id")
        name = _name(self.name, "VerificationCheck.name")
        if not isinstance(self.request, ProcessRequest):
            raise VerificationInputError("VerificationCheck.request must be ProcessRequest")
        passed = _exit_codes(self.pass_exit_codes, "pass_exit_codes", required=True)
        errors = _exit_codes(self.error_exit_codes, "error_exit_codes", required=False)
        if set(passed).intersection(errors):
            raise VerificationInputError("pass_exit_codes and error_exit_codes must not overlap")
        object.__setattr__(self, "check_id", check_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "pass_exit_codes", passed)
        object.__setattr__(self, "error_exit_codes", errors)


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    plan_id: str
    checks: tuple[VerificationCheck, ...]

    def __post_init__(self) -> None:
        plan_id = _id(self.plan_id, "VerificationPlan.plan_id")
        checks = tuple(self.checks)
        if not checks:
            raise VerificationInputError("VerificationPlan must contain at least one check")
        if len(checks) > _MAX_CHECKS:
            raise VerificationInputError(f"VerificationPlan exceeds {_MAX_CHECKS} checks")
        if any(not isinstance(check, VerificationCheck) for check in checks):
            raise VerificationInputError("VerificationPlan checks must be VerificationCheck values")
        ids = [check.check_id for check in checks]
        if len(ids) != len(set(ids)):
            raise VerificationInputError("VerificationPlan check_id values must be unique")
        total_timeout = sum(check.request.timeout_ms for check in checks)
        if total_timeout > _MAX_TOTAL_TIMEOUT_MS:
            raise VerificationInputError(
                f"VerificationPlan timeout budget exceeds {_MAX_TOTAL_TIMEOUT_MS} ms"
            )
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "checks", checks)


@dataclass(frozen=True, slots=True)
class VerificationCheckResult:
    check_id: str
    name: str
    status: VerificationStatus
    process_result: ProcessResult | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        _id(self.check_id, "VerificationCheckResult.check_id")
        _name(self.name, "VerificationCheckResult.name")
        if not isinstance(self.status, VerificationStatus):
            raise VerificationInputError("VerificationCheckResult.status is invalid")
        if self.status in {VerificationStatus.PASS, VerificationStatus.FAIL}:
            if not isinstance(self.process_result, ProcessResult) or self.process_result.timed_out:
                raise VerificationInputError("PASS/FAIL requires a non-timeout ProcessResult")
            if self.error_type is not None or self.error_message is not None:
                raise VerificationInputError("PASS/FAIL cannot contain infrastructure error fields")
        elif self.status is VerificationStatus.TIMEOUT:
            if not isinstance(self.process_result, ProcessResult) or not self.process_result.timed_out:
                raise VerificationInputError("TIMEOUT requires a timed-out ProcessResult")
            if self.error_type is not None or self.error_message is not None:
                raise VerificationInputError("TIMEOUT cannot contain infrastructure error fields")
        else:
            process_derived = isinstance(self.process_result, ProcessResult)
            infrastructure = self.process_result is None
            if process_derived:
                if self.process_result.timed_out:
                    raise VerificationInputError("ERROR ProcessResult must not be timed out")
                if self.error_type is not None or self.error_message is not None:
                    raise VerificationInputError("Process-derived ERROR cannot contain infrastructure fields")
            elif infrastructure:
                if (
                    not isinstance(self.error_type, str)
                    or not self.error_type
                    or len(self.error_type) > _MAX_ERROR_TYPE
                    or "\x00" in self.error_type
                    or not isinstance(self.error_message, str)
                    or len(self.error_message) > _MAX_ERROR_MESSAGE
                ):
                    raise VerificationInputError("Infrastructure ERROR requires bounded error fields")
                object.__setattr__(self, "error_message", self.error_message.replace("\x00", ""))
            else:  # pragma: no cover - the two branches above are exhaustive
                raise VerificationInputError("ERROR result has invalid evidence")


@dataclass(frozen=True, slots=True)
class VerificationReport:
    verification_id: str
    plan_id: str
    results: tuple[VerificationCheckResult, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        _id(self.verification_id, "VerificationReport.verification_id")
        _id(self.plan_id, "VerificationReport.plan_id")
        results = tuple(self.results)
        if not results:
            raise VerificationInputError("VerificationReport must contain at least one result")
        if any(not isinstance(result, VerificationCheckResult) for result in results):
            raise VerificationInputError("VerificationReport.results is invalid")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise VerificationInputError("VerificationReport.duration_ms must be non-negative int")
        object.__setattr__(self, "results", results)

    @property
    def status(self) -> VerificationStatus:
        statuses = {result.status for result in self.results}
        if VerificationStatus.ERROR in statuses:
            return VerificationStatus.ERROR
        if VerificationStatus.TIMEOUT in statuses:
            return VerificationStatus.TIMEOUT
        if VerificationStatus.FAIL in statuses:
            return VerificationStatus.FAIL
        return VerificationStatus.PASS

    @property
    def passed(self) -> int:
        return sum(result.status is VerificationStatus.PASS for result in self.results)

    @property
    def failed(self) -> int:
        return sum(result.status is VerificationStatus.FAIL for result in self.results)

    @property
    def timed_out(self) -> int:
        return sum(result.status is VerificationStatus.TIMEOUT for result in self.results)

    @property
    def errors(self) -> int:
        return sum(result.status is VerificationStatus.ERROR for result in self.results)

    @property
    def total(self) -> int:
        return len(self.results)


def new_verification_id() -> str:
    return f"ver_{uuid.uuid4()}"
