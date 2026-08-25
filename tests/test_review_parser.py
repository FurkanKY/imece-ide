import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_runtime.errors import ReviewProtocolError  # noqa: E402
from review_runtime.models import ReviewSeverity, ReviewVerdict  # noqa: E402
from review_runtime.parser import parse_review_decision  # noqa: E402


def test_approved_empty_findings():
    decision = parse_review_decision('{"verdict":"APPROVED","summary":"looks good","findings":[]}')
    assert decision.verdict is ReviewVerdict.APPROVED
    assert decision.findings == ()


def test_approved_findings_key_optional():
    decision = parse_review_decision('{"verdict":"APPROVED","summary":"looks good"}')
    assert decision.findings == ()


def test_needs_fix_with_finding():
    text = (
        '{"verdict":"NEEDS_FIX","summary":"bug","findings":['
        '{"severity":"major","message":"bad thing","path":"a/b.py","start_line":1,"end_line":2}]}'
    )
    decision = parse_review_decision(text)
    assert decision.verdict is ReviewVerdict.NEEDS_FIX
    assert len(decision.findings) == 1
    finding = decision.findings[0]
    assert finding.severity is ReviewSeverity.MAJOR
    assert finding.path == "a/b.py"
    assert (finding.start_line, finding.end_line) == (1, 2)


def test_finding_without_location():
    text = '{"verdict":"NEEDS_FIX","summary":"bug","findings":[{"severity":"minor","message":"nit"}]}'
    decision = parse_review_decision(text)
    assert decision.findings[0].path is None


def test_approved_with_findings_rejected():
    text = '{"verdict":"APPROVED","summary":"x","findings":[{"severity":"minor","message":"m"}]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_needs_fix_without_findings_rejected():
    with pytest.raises(ReviewProtocolError):
        parse_review_decision('{"verdict":"NEEDS_FIX","summary":"x","findings":[]}')


def test_invalid_severity_rejected():
    text = '{"verdict":"NEEDS_FIX","summary":"x","findings":[{"severity":"catastrophic","message":"m"}]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_invalid_verdict_rejected():
    with pytest.raises(ReviewProtocolError):
        parse_review_decision('{"verdict":"MAYBE","summary":"x","findings":[]}')


@pytest.mark.parametrize("verdict", ["UNKNOWN", "PASS_WITH_WARNINGS", "APPROVED_WITH_NOTES", "approved"])
def test_non_canonical_verdicts_rejected(verdict):
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(f'{{"verdict":"{verdict}","summary":"x","findings":[]}}')


def test_invalid_path_with_traversal_rejected():
    text = (
        '{"verdict":"NEEDS_FIX","summary":"x","findings":['
        '{"severity":"minor","message":"m","path":"../evil.py","start_line":1,"end_line":1}]}'
    )
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_invalid_line_range_rejected():
    text = (
        '{"verdict":"NEEDS_FIX","summary":"x","findings":['
        '{"severity":"minor","message":"m","path":"a.py","start_line":5,"end_line":1}]}'
    )
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_one_sided_line_range_rejected():
    text = (
        '{"verdict":"NEEDS_FIX","summary":"x","findings":['
        '{"severity":"minor","message":"m","path":"a.py","start_line":5}]}'
    )
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_duplicate_top_level_key_rejected():
    text = '{"verdict":"APPROVED","summary":"a","summary":"b","findings":[]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_duplicate_finding_key_rejected():
    text = (
        '{"verdict":"NEEDS_FIX","summary":"x","findings":['
        '{"severity":"minor","severity":"major","message":"m"}]}'
    )
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_infinity_rejected(constant):
    text = f'{{"verdict":"NEEDS_FIX","summary":"x","findings":[{{"severity":"minor","message":"m","start_line":{constant}}}]}}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_unknown_top_level_key_rejected():
    text = '{"verdict":"APPROVED","summary":"a","findings":[],"extra":true}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_unknown_finding_key_rejected():
    text = '{"verdict":"NEEDS_FIX","summary":"x","findings":[{"severity":"minor","message":"m","extra":1}]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_markdown_fence_rejected():
    text = '```json\n{"verdict":"APPROVED","summary":"a","findings":[]}\n```'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_prose_prefix_rejected():
    text = 'Here is my answer: {"verdict":"APPROVED","summary":"a","findings":[]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_prose_suffix_rejected():
    text = '{"verdict":"APPROVED","summary":"a","findings":[]}\nThanks!'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(text)


def test_empty_output_rejected():
    with pytest.raises(ReviewProtocolError):
        parse_review_decision("")
    with pytest.raises(ReviewProtocolError):
        parse_review_decision("   ")


def test_legacy_free_text_protocol_rejected():
    with pytest.raises(ReviewProtocolError):
        parse_review_decision("VERDICT: APPROVED")


def test_top_level_array_rejected():
    with pytest.raises(ReviewProtocolError):
        parse_review_decision('[{"verdict":"APPROVED","summary":"a","findings":[]}]')


def test_malformed_json_never_defaults_to_approved():
    for bad in ["{", "not json at all", '{"verdict":}', "null", "42", '"just a string"']:
        with pytest.raises(ReviewProtocolError):
            parse_review_decision(bad)


def test_model_output_size_is_bounded():
    huge = '{"verdict":"APPROVED","summary":"' + ("x" * 30_000) + '","findings":[]}'
    with pytest.raises(ReviewProtocolError):
        parse_review_decision(huge)
