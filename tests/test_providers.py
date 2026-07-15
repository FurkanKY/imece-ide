"""providers.py katalog + kayıt testleri — ağ çağrıları mock'lanır."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapters  # noqa: E402
import providers  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Kullanıcı providers.json'ı test başına geçici dizine taşınır."""
    cfg = tmp_path / "providers.json"
    monkeypatch.setattr(providers, "providers_config_path", lambda: cfg)
    yield
    providers.refresh()


def test_catalog_ids_unique_and_fields_complete():
    entries = providers.catalog()
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    for e in entries:
        assert e["kind"] in ("openai", "cli")
        if e["kind"] == "openai":
            assert e["base_url"].startswith("http")
            assert e["default_model"]
            assert e["models"]
        else:
            assert e["default_command"]


def test_refresh_populates_adapters_providers():
    providers.refresh()
    for e in providers.catalog():
        assert e["id"] in adapters.PROVIDERS
        assert callable(adapters.PROVIDERS[e["id"]])
    # geriye dönük sözleşme: üç klasik id her zaman var
    for legacy in ("claude", "deepseek", "gemini"):
        assert legacy in adapters.PROVIDERS


def test_openai_compat_request_shape(monkeypatch):
    captured = {}

    class FakeResp:
        ok = True
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": " merhaba "}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return FakeResp()

    monkeypatch.setattr(adapters.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    resp = adapters.call_openai_compat(
        "sistem", "görev",
        provider_id="deepseek", base_url="https://api.deepseek.com",
        model="deepseek-chat", key_env="DEEPSEEK_API_KEY",
        pricing={"deepseek-chat": (0.27, 1.10)},
    )
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "sistem"}
    assert resp.text == "merhaba"
    assert resp.provider == "deepseek"
    assert resp.cost_usd == pytest.approx(100 / 1e6 * 0.27 + 50 / 1e6 * 1.10)


def test_openai_compat_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        adapters.call_openai_compat(
            "s", "u", provider_id="deepseek",
            base_url="https://api.deepseek.com", model="deepseek-chat",
            key_env="DEEPSEEK_API_KEY",
        )


def test_registry_routes_gemini_through_openai_compat(monkeypatch):
    """gemini id'si artık OpenAI-uyumlu uca gitmeli (özel adapter değil)."""
    providers.refresh()
    captured = {}

    def fake_compat(system, user, **kw):
        captured.update(kw)
        return adapters.LLMResponse(text="ok", provider=kw["provider_id"], model=kw["model"])

    monkeypatch.setattr(adapters, "call_openai_compat", fake_compat)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    resp = adapters.PROVIDERS["gemini"]("s", "u")
    assert resp.provider == "gemini"
    assert "openai" in captured["base_url"]


def test_selected_model_priority(monkeypatch):
    entry = providers.get("deepseek")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    assert providers.selected_model(entry) == "deepseek-chat"
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    assert providers.selected_model(entry) == "deepseek-reasoner"
    providers.set_model("deepseek", "deepseek-chat")  # kullanıcı seçimi env'i ezer
    assert providers.selected_model(providers.get("deepseek")) == "deepseek-chat"


def test_custom_provider_lifecycle():
    entry = providers.add_custom({
        "id": "my-llm", "label": "Şirket LLM",
        "base_url": "https://llm.example.com/v1/", "default_model": "iç-model",
    })
    assert entry["base_url"] == "https://llm.example.com/v1"  # sondaki / temizlenir
    assert providers.get("my-llm")["custom"] is True
    assert "my-llm" not in [e["id"] for e in providers.CATALOG]  # yerleşik değişmez
    providers.refresh()
    assert "my-llm" in adapters.PROVIDERS

    with pytest.raises(ValueError):
        providers.add_custom({"id": "my-llm", "label": "x",
                              "base_url": "https://a", "default_model": "m"})

    providers.remove_custom("my-llm")
    assert providers.get("my-llm") is None
    assert "my-llm" not in adapters.PROVIDERS
    with pytest.raises(ValueError):
        providers.remove_custom("my-llm")


def test_custom_provider_cannot_shadow_builtin():
    with pytest.raises(ValueError):
        providers.add_custom({"id": "deepseek", "label": "sahte",
                              "base_url": "https://kötü.example", "default_model": "m"})


def test_status_never_contains_key_values(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-sahte-ornek-1234")  # gitleaks:allow
    info = providers.status_of(providers.get("deepseek"))
    assert "sk-sahte" not in str(info)
    assert info["ok"] is True


def test_cli_status_detection(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda _: None)
    info = providers.status_of(providers.get("gemini-cli"))
    assert info["ok"] is False and "bulunamadı" in info["detail"]
    monkeypatch.setattr(providers.shutil, "which", lambda _: "/usr/bin/gemini")
    assert providers.status_of(providers.get("gemini-cli"))["ok"] is True


def test_test_provider_reports_auth_failure(monkeypatch):
    class FakeResp:
        ok = False
        status_code = 401

    monkeypatch.setattr(providers.requests, "get", lambda *a, **k: FakeResp())
    out = providers.test_provider("deepseek", "sk-yanlis")
    assert out["ok"] is False and "401" in out["detail"]
