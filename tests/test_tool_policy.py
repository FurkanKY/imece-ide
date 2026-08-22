import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_runtime import PermissionEffect, PermissionRequest
from tool_runtime.errors import ToolPolicyError
from tool_runtime.policy import PermissionRule, PolicyEvaluator


def request(action="read", resource="file.txt"):
    return PermissionRequest(action, resource)


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([], PermissionEffect.ASK),
        ([PermissionRule("read", "file.txt", PermissionEffect.ALLOW)], PermissionEffect.ALLOW),
        ([PermissionRule("read", "file.txt", PermissionEffect.ASK)], PermissionEffect.ASK),
        ([PermissionRule("read", "file.txt", PermissionEffect.DENY)], PermissionEffect.DENY),
    ],
)
def test_single_request_effects(rules, expected):
    assert PolicyEvaluator(rules).evaluate([request()]).effect is expected


def test_deny_beats_overlapping_allow_and_ask_beats_allow():
    assert PolicyEvaluator(
        [PermissionRule("read", "*", PermissionEffect.ALLOW), PermissionRule("read", "file.txt", PermissionEffect.DENY)]
    ).evaluate([request()]).effect is PermissionEffect.DENY
    assert PolicyEvaluator(
        [PermissionRule("read", "*", PermissionEffect.ALLOW), PermissionRule("read", "file.txt", PermissionEffect.ASK)]
    ).evaluate([request()]).effect is PermissionEffect.ASK


def test_multiple_requests_aggregate_deny_then_ask_then_allow():
    evaluator = PolicyEvaluator([
        PermissionRule("read", "a", PermissionEffect.ALLOW),
        PermissionRule("edit", "b", PermissionEffect.ASK),
        PermissionRule("delete", "c", PermissionEffect.DENY),
    ])
    assert evaluator.evaluate([request("read", "a"), request("edit", "b")]).effect is PermissionEffect.ASK
    assert evaluator.evaluate([request("read", "a"), request("delete", "c")]).effect is PermissionEffect.DENY


def test_action_and_resource_both_participate_and_actions_are_open_strings():
    evaluator = PolicyEvaluator([PermissionRule("mcp_*", "github/issues/*", PermissionEffect.ALLOW)])
    assert evaluator.evaluate([request("mcp_github_issue_read", "github/issues/42")]).effect is PermissionEffect.ALLOW
    assert evaluator.evaluate([request("mcp_github_issue_read", "github/repos/42")]).effect is PermissionEffect.ASK


def test_empty_requests_fail_closed():
    with pytest.raises(ToolPolicyError):
        PolicyEvaluator().evaluate([])
