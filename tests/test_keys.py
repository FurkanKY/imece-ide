"""keys.py .env yazıcısı testleri — yorum/bilinmeyen satır korunur, anahtar güncellenir."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webhost.api.keys import write_env, clear_env_keys, _mask  # noqa: E402


def test_write_env_updates_existing_and_preserves_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# yorum satırı\nDEEPSEEK_API_KEY=eski\nGEMINI_MODEL=gemini-2.5-flash\n",
                 encoding="utf-8")
    write_env(p, {"DEEPSEEK_API_KEY": "yeni-123"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# yorum satırı"
    assert "DEEPSEEK_API_KEY=yeni-123" in lines
    assert "GEMINI_MODEL=gemini-2.5-flash" in lines
    assert "eski" not in p.read_text(encoding="utf-8")


def test_write_env_appends_missing_and_creates_file(tmp_path):
    p = tmp_path / ".env"
    write_env(p, {"GEMINI_API_KEY": "abc"})
    assert p.read_text(encoding="utf-8") == "GEMINI_API_KEY=abc\n"
    write_env(p, {"DEEPSEEK_API_KEY": "xyz"})
    content = p.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=abc" in content and "DEEPSEEK_API_KEY=xyz" in content


def test_mask_never_leaks_short_keys():
    assert _mask("kisa") == "••••"
    assert _mask("cok-uzun-anahtar-1234").endswith("1234")
    assert "cok-uzun" not in _mask("cok-uzun-anahtar-1234")


def test_clear_env_keys_keeps_models_and_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# sakla\nDEEPSEEK_API_KEY=secret\nDEEPSEEK_MODEL=deepseek-chat\n", encoding="utf-8")
    clear_env_keys(p, {"DEEPSEEK_API_KEY"})
    assert p.read_text(encoding="utf-8") == "# sakla\nDEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=deepseek-chat\n"
