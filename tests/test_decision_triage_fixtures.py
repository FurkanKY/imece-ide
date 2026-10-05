"""Fixture-set integrity + RuleDecisionBackend accuracy on labelled fixtures.

tests/fixtures/verification_failures/*.json each describe one realistic failing
check (pytest/unittest/npm/jest/go test/shell). Schema v2 splits two kinds of
expectation ON PURPOSE (avoids validation circularity, docs/JEV-DESIGN.md
"Spike S1"):

  expected_rule_kind / expected_rule_action — what the deterministic rule
      backend is EXPECTED to say (pinned here, byte for byte);
  expected_gate_action — the pipeline action after the gate's
      MARK_PRE_EXISTING safety guard (decision_runtime.gate.
      _guard_pre_existing): for an inconclusive baseline ('error'/'timeout')
      the guard downgrades mark_pre_existing to continue_fix_loop;
  gold_kind / gold_action — the SEMANTIC truth. A rule that cannot know a
      semantic class (test_needs_update is unknowable from one output) shows
      up as a low-confidence default instead of being graded against itself.

origin is 'synthetic' (hand-written scenario) or 'real_capture' (an actual
tool capture made in a scratch repo under /tmp/opencode, with a reproducible
cmd + tool version in real_capture). No network, no real git worktree: the
baseline result is itself one of the pre-computed facts.
"""

import collections
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from decision_runtime.gate import _guard_pre_existing  # noqa: E402
from decision_runtime.policy import ConfidenceBand, DecisionPolicy  # noqa: E402
from decision_runtime.triage import (  # noqa: E402
    FailureKind,
    RuleDecisionBackend,
    build_triage_spec,
    build_triage_state,
    decide_triage_action,
)
from eval_dataset import fixture_to_facts  # noqa: E402  (tools/eval_dataset.py)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "verification_failures"

KINDS = {kind.value for kind in FailureKind}
ACTIONS = {"continue_fix_loop", "needs_user", "rerun_verification_once", "mark_pre_existing"}
LANGUAGES = {"python", "javascript", "go", "shell"}
ORIGINS = {"synthetic", "real_capture"}

# The action table only ever "acts" on failure_kind classes that carry a
# strong deterministic signal in RuleDecisionBackend (design rule 1: a low-
# confidence guess must fall back to today's behaviour, never assert
# equality). code_bug/test_needs_update fixtures ARE still expected to come
# back as "code_bug" (the deterministic default guess), just with LOW
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
BY_NAME = {fixture["name"]: fixture for fixture in FIXTURES}


def _classify(fixture: dict):
    """Facts exactly as the real gate builds them (extract_error_block +
    actual command sequence) — see eval_dataset.fixture_to_facts."""
    state = build_triage_state(fixture_to_facts(fixture))
    spec = build_triage_spec("fixture_decision")
    return RuleDecisionBackend().decide(spec, state)


def _raw_action_of(fixture: dict) -> str:
    """The raw decide_triage_action mapping (what the helper alone says)."""
    return decide_triage_action(_classify(fixture), DecisionPolicy()).action.value


def _gate_action_of(fixture: dict) -> str:
    """The gate-level action — exactly what VerificationFailureGate does:
    decide_triage_action followed by the MARK_PRE_EXISTING guard."""
    outcome = decide_triage_action(_classify(fixture), DecisionPolicy())
    return _guard_pre_existing(outcome, fixture["baseline_status"]).action.value


# ---------------- dataset shape ----------------


def test_fixture_directory_is_in_the_s1b_target_range():
    assert 50 <= len(FIXTURES) <= 80


def test_expected_rule_kind_is_never_test_needs_update():
    """test_needs_update is SEMANTIC (needs the task definition); the rule
    backend can never infer it from one output — it must stay out of
    expected_rule_kind and appear only under gold_kind."""
    rule_kinds = {fixture["expected_rule_kind"] for fixture in FIXTURES}
    assert "test_needs_update" not in rule_kinds
    assert "test_needs_update" in {fixture["gold_kind"] for fixture in FIXTURES}


def test_all_six_gold_classes_are_present():
    assert {fixture["gold_kind"] for fixture in FIXTURES} == KINDS


def test_python_js_go_and_shell_language_coverage():
    tools = {fixture["tool"].split()[0] for fixture in FIXTURES}
    assert {"pytest", "unittest", "npm", "jest", "go"}.issubset(tools)
    assert LANGUAGES.issubset({fixture["language"] for fixture in FIXTURES})


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_per_fixture_schema_fields(fixture):
    assert fixture["schema_version"] == 2
    assert fixture["name"]
    assert fixture["origin"] in ORIGINS
    assert fixture["language"] in LANGUAGES
    assert fixture["tool"]
    assert isinstance(fixture.get("command"), list), "fixtures must record the actual argv sequence"
    assert all(isinstance(token, str) and token for token in fixture["command"])
    assert len(fixture["command"]) <= 32
    assert isinstance(fixture["provenance"], str) and fixture["provenance"]
    assert fixture["expected_rule_kind"] in KINDS
    assert fixture["gold_kind"] in KINDS
    for action_field in ("expected_rule_action", "expected_gate_action", "gold_action"):
        assert fixture[action_field] in ACTIONS, (fixture["name"], action_field)
    if fixture["origin"] == "real_capture":
        capture = fixture["real_capture"]
        assert all(isinstance(capture.get(key), str) and capture[key]
                   for key in ("cmd", "source", "tool_version", "captured_at"))
        blob = json.dumps(capture)
        assert "sk-" not in blob and "api_key" not in blob.lower()
    else:
        assert not fixture.get("real_capture")


def test_real_captures_are_a_small_clearly_marked_minority():
    reals = [fixture for fixture in FIXTURES if fixture["origin"] == "real_capture"]
    assert 0 <= len(reals) <= len(FIXTURES) // 2, "synthetic hand-written scenarios must dominate"
    if reals:
        assert all("opencode" in fixture["provenance"] for fixture in reals), (
            "real captures must record where/how they were produced"
        )


def test_gold_and_rule_expectations_diverge_for_semantic_cases():
    """The whole point of the v2 schema: some gold kinds are deliberately NOT
    knowable to the rules (test_needs_update, adversarial injections,
    ambiguous environment vs code). Those fixtures pin what the rules DO say
    (expected_rule_kind) while carrying the semantic truth separately."""
    divergent = [fixture for fixture in FIXTURES if fixture["gold_kind"] != fixture["expected_rule_kind"]]
    assert len(divergent) >= 10, f"expected a meaningful semantic gap set, got {len(divergent)}"
    languages = {fixture["language"] for fixture in divergent}
    assert {"python", "javascript", "go"}.issubset(languages)


# ---------------- pinned rule-backend behaviour ----------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_rule_backend_predicts_expected_rule_kind(fixture):
    result = _classify(fixture)
    predicted = result.answers["failure_kind"].choice
    assert predicted == fixture["expected_rule_kind"], (
        f"{fixture['name']}: expected {fixture['expected_rule_kind']!r}, got {predicted!r}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_expected_rule_action_matches_current_deterministic_behaviour(fixture):
    """Pins fixture expected_rule_action to the CURRENT decide_triage_action
    output (default policy) — the raw helper mapping."""
    assert _raw_action_of(fixture) == fixture["expected_rule_action"], (
        f"{fixture['name']}: current rule action differs from expected_rule_action"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_expected_gate_action_matches_the_gate_guard(fixture):
    """Pins fixture expected_gate_action to the CURRENT pipeline action:
    decide_triage_action + the gate's MARK_PRE_EXISTING safety guard
    (decision_runtime.gate._guard_pre_existing, S1b backend slice)."""
    assert _gate_action_of(fixture) == fixture["expected_gate_action"], (
        f"{fixture['name']}: current gate action differs from expected_gate_action"
    )


def test_strong_signal_classes_meet_the_confidence_bar():
    """docs/JEV-DESIGN.md success criterion: ">= 90% precision on the stop-and-
    ask classes at the chosen threshold". Checks the rule backend's own ACT-
    band behaviour against expected_rule_kind (NOT gold) with the default policy."""
    policy = DecisionPolicy()
    strong = [f for f in FIXTURES if f["expected_rule_kind"] in _STRONG_SIGNAL_LABELS]
    assert strong

    correct = 0
    acted = 0
    for fixture in strong:
        result = _classify(fixture)
        confidence = result.confidence_of("failure_kind")
        predicted = result.answers["failure_kind"].choice
        if policy.band(confidence) is ConfidenceBand.ACT:
            acted += 1
            if predicted == fixture["expected_rule_kind"]:
                correct += 1
    assert acted == len(strong), "every strong-signal fixture is expected to reach the ACT band"
    accuracy = correct / acted
    assert accuracy >= 0.9, f"strong-signal accuracy {accuracy:.2%} below the 90% bar"


def test_semantic_gap_cases_are_low_confidence_or_rule_act_knowingly():
    """For every fixture where gold differs from the rule expectation: either
    the rule stays BELOW the ACT band (unknown semantics -> today's behaviour)
    or the fixture documents the deliberate adversarial/ambiguous ACT (those
    must carry a note)."""
    policy = DecisionPolicy()
    for fixture in FIXTURES:
        if fixture["gold_kind"] == fixture["expected_rule_kind"]:
            continue
        result = _classify(fixture)
        if policy.band(result.confidence_of("failure_kind")) is ConfidenceBand.ACT:
            assert fixture.get("note"), (
                f"{fixture['name']}: an ACT-band rule decision that diverges from gold "
                "must document why (adversarial/ambiguous)"
            )


def test_test_needs_update_gold_fixtures_stay_in_todays_behaviour():
    """The rule backend can't see 'test_needs_update' — those fixtures must
    come back as a LOW-confidence default (never ACT), i.e. continue_fix_loop
    exactly like today's pipeline."""
    policy = DecisionPolicy()
    semantic = [f for f in FIXTURES if f["gold_kind"] == "test_needs_update"]
    assert len(semantic) >= 3
    for fixture in semantic:
        result = _classify(fixture)
        confidence = result.confidence_of("failure_kind")
        assert policy.band(confidence) is not ConfidenceBand.ACT, (
            f"{fixture['name']}: rule must not ACT on unknowable semantics (confidence={confidence!r})"
        )
        assert result.answers["failure_kind"].choice == "code_bug"  # the honest default guess
        assert _gate_action_of(fixture) == "continue_fix_loop"


def test_baseline_inconclusive_fixtures_document_the_gate_guard():
    """Contradictory remote/baseline safety scenario: baseline 'error'/'timeout'
    means the baseline RERUN was inconclusive. The rule backend still classifies
    unrelated_preexisting there (raw helper output — pinned), but the gate's
    safety guard downgrades the PIPELINE action to continue_fix_loop (a wrong
    skip is worse than a wasted attempt). Fixtures record all three levels."""
    inconclusive = [f for f in FIXTURES if f["baseline_status"] in ("error", "timeout")]
    assert len(inconclusive) >= 3
    for fixture in inconclusive:
        assert fixture["gold_kind"] == "unrelated_preexisting"
        assert fixture["expected_rule_action"] == "mark_pre_existing"   # raw helper output
        assert fixture["expected_gate_action"] == "continue_fix_loop"   # after the gate guard
        assert fixture["gold_action"] == "continue_fix_loop"            # ideal gate semantics
        assert "guard" in fixture.get("note", ""), (
            f"{fixture['name']}: inconclusive-baseline fixture must document the gate guard"
        )


def test_authorized_baseline_fail_fixture_exists():
    authorized = [f for f in FIXTURES if f["baseline_status"] == "fail"]
    assert authorized
    for fixture in authorized:
        assert fixture["gold_kind"] == "unrelated_preexisting"
        assert fixture["expected_rule_action"] == "mark_pre_existing"
        assert fixture["expected_gate_action"] == "mark_pre_existing"  # guard authorizes a real FAIL baseline
        assert fixture["gold_action"] == "mark_pre_existing"


def test_adversarial_injection_probe_exists():
    """docs/JEV-DESIGN.md: Jev is 'not robust to adversarial/injected content
    in state' — the set must contain an injected-content probe for live runs."""
    adversarial = [f for f in FIXTURES if "adversarial" in f["name"]]
    assert len(adversarial) >= 3
    assert any("injected" in f.get("note", "") or "injected" in f["provenance"] for f in adversarial)


def test_confusion_summary():
    """Prints a small confusion summary (gold -> predicted counts) for
    visibility (run with `pytest -s` to see it) — not itself a pass/fail
    assertion beyond the ones above."""
    confusion = collections.Counter()
    for fixture in FIXTURES:
        result = _classify(fixture)
        predicted = result.answers["failure_kind"].choice
        confusion[(fixture["gold_kind"], predicted)] += 1

    lines = ["RuleDecisionBackend confusion summary (gold -> predicted: count)"]
    for (gold, predicted), count in sorted(confusion.items()):
        marker = "" if gold == predicted else "  <-- semantic gap"
        lines.append(f"  {gold:22s} -> {predicted:22s} : {count}{marker}")
    total = len(FIXTURES)
    matches = sum(count for (gold, predicted), count in confusion.items() if gold == predicted)
    lines.append(f"overall gold accuracy: {matches}/{total} ({matches / total:.1%})")
    print("\n".join(lines))
