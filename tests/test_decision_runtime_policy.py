"""decision_runtime.policy — confidence banding and config loading."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.errors import DecisionInputError  # noqa: E402
from decision_runtime.policy import ConfidenceBand, DecisionPolicy  # noqa: E402


def test_default_thresholds_match_docs_guidance():
    policy = DecisionPolicy()
    assert policy.act_threshold == 0.9
    assert policy.escalate_threshold == 0.5


def test_band_boundaries():
    policy = DecisionPolicy(act_threshold=0.9, escalate_threshold=0.5)
    assert policy.band(0.95) is ConfidenceBand.ACT
    assert policy.band(0.9) is ConfidenceBand.ACT
    assert policy.band(0.89) is ConfidenceBand.CONFIRM
    assert policy.band(0.5) is ConfidenceBand.CONFIRM
    assert policy.band(0.49) is ConfidenceBand.ESCALATE
    assert policy.band(0.0) is ConfidenceBand.ESCALATE


def test_rejects_escalate_above_act():
    with pytest.raises(DecisionInputError):
        DecisionPolicy(act_threshold=0.5, escalate_threshold=0.9)


def test_rejects_out_of_range_thresholds():
    with pytest.raises(DecisionInputError):
        DecisionPolicy(act_threshold=1.5)
    with pytest.raises(DecisionInputError):
        DecisionPolicy(escalate_threshold=-0.1)


def test_from_config_defaults_when_missing():
    policy = DecisionPolicy.from_config(None, key="verification_failure_triage")
    assert policy == DecisionPolicy()
    policy = DecisionPolicy.from_config({}, key="verification_failure_triage")
    assert policy == DecisionPolicy()


def test_from_config_honors_partial_override():
    config = {"verification_failure_triage": {"act_threshold": 0.8}}
    policy = DecisionPolicy.from_config(config, key="verification_failure_triage")
    assert policy.act_threshold == 0.8
    assert policy.escalate_threshold == 0.5  # untouched default
