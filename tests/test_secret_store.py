"""secret_store — _known_key_envs() katalogdan türetme testleri.

DPAPI yalnız Windows'ta çalışır (_protect/_unprotect os.name == "nt" gerektirir);
kaynak modunda (Linux/CI) SecretStore.load()/save() gerçek şifrelemeyi
sürmez. Bu yüzden burada sadece anahtar FİLTRELEME mantığını (_known_key_envs
ve onun tükettiği providers.catalog() türetimi) doğrudan test ediyoruz —
DPAPI'ye hiç dokunmadan.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import providers  # noqa: E402
import secret_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    cfg = tmp_path / "providers.json"
    monkeypatch.setattr(providers, "providers_config_path", lambda: cfg)
    yield
    providers.refresh()


def test_known_key_envs_covers_builtin_catalog():
    envs = secret_store._known_key_envs()
    assert "ANTHROPIC_API_KEY" in envs
    assert "DEEPSEEK_API_KEY" in envs
    assert "GEMINI_API_KEY" in envs
    assert "OPENAI_API_KEY" in envs
    # CLI girdilerinin key_env'i yok -> listede olmamalı
    assert "CLAUDE_CLI" not in envs


def test_known_key_envs_covers_custom_endpoint():
    providers.add_custom({
        "id": "sirket", "label": "Şirket LLM",
        "base_url": "https://llm.example.com/v1", "default_model": "ic-model",
    })
    try:
        envs = secret_store._known_key_envs()
        assert "IMECE_CUSTOM_SIRKET_API_KEY" in envs
    finally:
        providers.remove_custom("sirket")


def test_known_key_envs_has_no_duplicates_and_is_sorted():
    envs = secret_store._known_key_envs()
    assert list(envs) == sorted(set(envs))


def test_secret_store_filtering_uses_known_key_envs(monkeypatch):
    """load()/save()'in filtre mantığı _known_key_envs()'e göre çalışır —
    DPAPI'yi bypas ederek _protect/_unprotect'i sahte (round-trip) hale getirir."""
    monkeypatch.setattr(secret_store, "_known_key_envs", lambda: ("ANTHROPIC_API_KEY",))
    store_data = {}

    def fake_protect(data: bytes) -> bytes:
        return data

    def fake_unprotect(data: bytes) -> bytes:
        return data

    monkeypatch.setattr(secret_store, "_protect", fake_protect)
    monkeypatch.setattr(secret_store, "_unprotect", fake_unprotect)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        store = secret_store.SecretStore(Path(d) / "secrets.dat")
        store.save({"ANTHROPIC_API_KEY": "sk-ant-abc", "GEMINI_API_KEY": "should-be-dropped"})
        loaded = store.load()
        assert loaded == {"ANTHROPIC_API_KEY": "sk-ant-abc"}


# ---------------- Jev (TypeSafe) izin listesi (S1b) ----------------

def test_known_key_envs_includes_typesafe():
    envs = secret_store._known_key_envs()
    assert "TYPESAFE_API_KEY" in envs


def test_secret_store_keeps_typesafe_key(monkeypatch, tmp_path):
    """Paketli depo, Jev anahtarını izin listesiyle aynen korur; bilinmeyen
    env adları düşürülür (DPAPI bypas — round-trip sahte)."""
    monkeypatch.setattr(secret_store, "_known_key_envs", lambda: ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY"))
    monkeypatch.setattr(secret_store, "_protect", lambda data: data)
    monkeypatch.setattr(secret_store, "_unprotect", lambda data: data)
    store = secret_store.SecretStore(tmp_path / "secrets.dat")
    store.save({"TYPESAFE_API_KEY": "ts-gizli-uzun", "BILINMEYEN_ENV": "dusur"})
    assert store.load() == {"TYPESAFE_API_KEY": "ts-gizli-uzun"}
