"""webhost/api/run.py — A4-A5 (hata UX) ve A4-A3 (tam plan metni) için saf
birim testleri: _classify_error/_error_details/_ERROR_MESSAGES ve
_full_plan_text, gerçek bir PipelineRunner/Qt event loop'u ÇALIŞTIRMADAN.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")

from webhost.api import run as run_api  # noqa: E402


def test_classify_error_recognizes_fix_loop_exhausted_code_directly():
    assert run_api._classify_error("fix_loop_exhausted", "herhangi bir metin") == "fix_loop_exhausted"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("ACP ajanı başlatılamadı: agent bulunamadı", "acp_agent_unavailable"),
        ("Kimlik doğrulama gerekiyor (401)", "auth_required"),
        ("Rate limit exceeded (429)", "rate_limited"),
        ("İstek zaman aşımına uğradı", "timeout"),
        ("Executable not found: pytest", "verification_tool_missing"),
        ("git worktree oluşturulamadı", "worktree_git_failure"),
        ("API anahtarı eksik", "provider_not_configured"),
        ("Sağlayıcıya bağlanılamadı (connection refused)", "network_error"),
    ],
)
def test_classify_error_keyword_refinement(message, expected):
    # code=None (ör. kanonik durum okunamadı) -- yalnızca ham metinden inceltme.
    assert run_api._classify_error(None, message) == expected


def test_classify_error_unrecognized_generic_code_falls_back_to_provider_unavailable():
    assert run_api._classify_error("execution_failed", "tamamen anlaşılmaz bir hata") == "provider_unavailable"


def test_classify_error_totally_unknown_falls_back_to_generic():
    assert run_api._classify_error(None, "hiçbir anahtar sözcük içermeyen metin") == "generic"


def test_classify_error_never_raises_keyerror_for_any_bucket():
    # _ERROR_MESSAGES sözlüğünde _classify_error'ın döndürebileceği HER
    # anahtarın karşılığı olmalı -- aksi halde _error_details KeyError verir.
    for code in (
        "provider_not_configured", "auth_required", "acp_agent_unavailable", "rate_limited",
        "timeout", "network_error", "verification_tool_missing", "worktree_git_failure",
        "fix_loop_exhausted", "provider_unavailable", "generic",
    ):
        assert code in run_api._ERROR_MESSAGES
        title, description = run_api._ERROR_MESSAGES[code]
        assert title and description


def test_error_details_reads_canonical_error_code_from_coordinator():
    coordinator = SimpleNamespace(get_run=lambda: SimpleNamespace(error_code="verification_error"))
    details = run_api._error_details(coordinator, "Executable not found: npm")
    # verification_error kodu kendi başına özel değil ama mesajdaki "bulunamadı"
    # + araç sözcüğü YOK burada ("Executable not found" İngilizce anahtar sözcük
    # eşleşir) -- doğrudan verification_tool_missine düşer.
    assert details["errorCode"] == "verification_tool_missing"
    assert details["errorTitle"]
    assert details["errorDescription"]


def test_error_details_best_effort_when_coordinator_read_fails():
    def boom():
        raise RuntimeError("kanonik durum okunamadı")

    coordinator = SimpleNamespace(get_run=boom)
    details = run_api._error_details(coordinator, "bilinmeyen bir hata")
    assert details["errorCode"] == "generic"


def test_error_details_handles_none_coordinator():
    details = run_api._error_details(None, "bilinmeyen bir hata")
    assert details["errorCode"] == "generic"


def _plan_report(**overrides):
    defaults = dict(
        summary="a.txt düzeltilecek.",
        steps=(SimpleNamespace(title="Adım 1", objective="Düzelt."),),
        acceptance_criteria=("a.txt fixed yazar",),
        risks=(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_full_plan_text_includes_summary_steps_and_acceptance_criteria():
    text = run_api._full_plan_text(_plan_report())
    assert "a.txt düzeltilecek." in text
    assert "Adım 1" in text
    assert "Düzelt." in text
    assert "a.txt fixed yazar" in text


def test_full_plan_text_includes_risks_when_present():
    text = run_api._full_plan_text(_plan_report(risks=("Geriye uyumluluk riski.",)))
    assert "Geriye uyumluluk riski." in text


def test_full_plan_text_omits_empty_sections():
    text = run_api._full_plan_text(_plan_report(steps=(), acceptance_criteria=(), risks=()))
    assert text == "a.txt düzeltilecek."
