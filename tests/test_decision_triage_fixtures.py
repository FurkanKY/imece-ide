"""RuleDecisionBackend accuracy against labelled verification-failure fixtures.

tests/fixtures/verification_failures/*.json each describe one realistic
failing check (pytest/unittest/npm/jest/go test): syntax error, assertion,
import error, missing module, command not found, timeout, flaky, and
pre-existing-on-baseline, with an `expected_label` (one of the 6
failure_kind labels). No network, no real git worktree — the baseline
result is itself one of the pre-computed facts here (see docs/JEV-DESIGN.md
"Deterministic facts first (code): ... whether the same check also fails on
the baseline").
"""

import collections
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_runtime.policy import ConfidenceBand, DecisionPolicy  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    RuleDecisionBackend,
    TriageFacts,
    build_triage_spec,
    build_triage_state,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "verification_failures"

# The action table only ever "acts" on failure_kind classes that carry a
# strong deterministic signal in RuleDecisionBackend (design rule 1: a low-
# confidence guess must fall back to today's behaviour, never assert
# equality). code_bug/test_needs_update fixtures ARE still expected to
# come back as "code_bug" (the deterministic default guess), just with LOW
# confidence — see docs/JEV-DESIGN.md's action table row for that case.
_STRONG_SIGNAL_LABELS = {
    "missing_dependency", "environment_or_tooling", "flaky_or_timeout", "unrelated_preexisting",
}


def _load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            fixtures.append(json.load(fh))
    return fixtures


FIXTURES = _load_fixtures()


def test_fixture_directory_has_enough_labelled_cases():
    assert len(FIXTURES) >= 20
    tools = {fixture["tool"].split()[0] for fixture in FIXTURES}
    assert {"pytest", "unittest", "npm", "jest", "go"}.issubset(tools)
    labels = {fixture["expected_label"] for fixture in FIXTURES}
    assert labels == {
        "code_bug", "missing_dependency", "environment_or_tooling",
        "flaky_or_timeout", "unrelated_preexisting",
    }


def _classify(fixture: dict):
    facts = TriageFacts(
        check_id="check",
        command=(fixture["tool"].split()[0],),
        exit_code=fixture["exit_code"],
        timed_out=fixture["timed_out"],
        error_block=(fixture["stdout"] + "\n" + fixture["stderr"]),
        changed_paths=tuple(fixture["changed_paths"]),
        baseline_status=fixture["baseline_status"],
    )
    state = build_triage_state(facts)
    spec = build_triage_spec("fixture_decision")
    result = RuleDecisionBackend().decide(spec, state)
    return result


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_rule_backend_predicts_expected_label(fixture):
    result = _classify(fixture)
    predicted = result.answers["failure_kind"].choice
    assert predicted == fixture["expected_label"], (
        f"{fixture['name']}: expected {fixture['expected_label']!r}, got {predicted!r}"
    )


def test_strong_signal_classes_meet_the_confidence_bar():
    """docs/JEV-DESIGN.md success criteria: "≥ 90% precision on the stop-and-
    ask classes at the chosen threshold". This checks the rule backend's own
    ACT-band accuracy on the strong-signal classes with the default policy."""
    policy = DecisionPolicy()
    strong = [f for f in FIXTURES if f["expected_label"] in _STRONG_SIGNAL_LABELS]
    assert strong, "expected at least one strong-signal fixture"

    correct = 0
    acted = 0
    for fixture in strong:
        result = _classify(fixture)
        confidence = result.confidence_of("failure_kind")
        predicted = result.answers["failure_kind"].choice
        if policy.band(confidence) is ConfidenceBand.ACT:
            acted += 1
            if predicted == fixture["expected_label"]:
                correct += 1
    assert acted == len(strong), (
        "every strong-signal fixture is expected to reach the ACT confidence band"
    )
    accuracy = correct / acted
    assert accuracy >= 0.9, f"strong-signal accuracy {accuracy:.2%} below the 90% bar"


def test_weak_signal_default_guess_is_low_confidence():
    """code_bug fixtures with no strong signal are a DEFAULT GUESS — design
    rule 1 requires low confidence there so callers fall back to today's
    fix-loop behaviour rather than acting on a guess."""
    policy = DecisionPolicy()
    generic_code_bug = [
        f for f in FIXTURES
        if f["expected_label"] == "code_bug" and "myapp" not in f["name"] and "project_local" not in f["name"]
    ]
    assert generic_code_bug
    for fixture in generic_code_bug:
        result = _classify(fixture)
        confidence = result.confidence_of("failure_kind")
        assert policy.band(confidence) is not ConfidenceBand.ACT, (
            f"{fixture['name']}: default code_bug guess should not reach the ACT band "
            f"(confidence={confidence!r})"
        )


def test_confusion_summary():
    """Prints a small confusion summary (expected -> predicted counts) for
    visibility (run with `pytest -s` to see it) — not itself a pass/fail
    assertion beyond the ones above."""
    confusion = collections.Counter()
    for fixture in FIXTURES:
        result = _classify(fixture)
        predicted = result.answers["failure_kind"].choice
        confusion[(fixture["expected_label"], predicted)] += 1

    lines = ["RuleDecisionBackend confusion summary (expected -> predicted: count)"]
    for (expected, predicted), count in sorted(confusion.items()):
        marker = "" if expected == predicted else "  <-- MISS"
        lines.append(f"  {expected:22s} -> {predicted:22s} : {count}{marker}")
    total = len(FIXTURES)
    matches = sum(count for (expected, predicted), count in confusion.items() if expected == predicted)
    lines.append(f"overall accuracy: {matches}/{total} ({matches / total:.1%})")
    print("\n".join(lines))
