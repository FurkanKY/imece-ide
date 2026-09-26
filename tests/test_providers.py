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
        assert e["kind"] in ("openai", "anthropic", "cli")
        if e["kind"] in ("openai", "anthropic"):
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


# ---------------------------------------------------------------------------
# Anthropic (Claude API) katalog girdisi
# ---------------------------------------------------------------------------

def test_anthropic_catalog_entry_shape():
    entry = providers.get("anthropic")
    assert entry is not None
    assert entry["kind"] == "anthropic"
    assert entry["key_env"] == "ANTHROPIC_API_KEY"
    assert entry["default_model"] == "claude-opus-5"
    model_ids = [m[0] for m in entry["models"]]
    assert model_ids == ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5-5"]


def test_anthropic_is_ready_follows_key_env(monkeypatch):
    entry = providers.get("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert providers.is_ready(entry) is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert providers.is_ready(entry) is True


def test_anthropic_status_of_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    info = providers.status_of(providers.get("anthropic"))
    assert info["kind"] == "anthropic"
    assert info["ok"] is True
    assert info["model"] == "claude-opus-5"
    assert "claude-haiku-4-5" in info["models"]
    assert info["keyless"] is False


def test_anthropic_test_provider_uses_x_api_key_header(monkeypatch):
    captured = {}

    class FakeResp:
        ok = True
        status_code = 200

    def fake_get(url, headers=None, timeout=None):
        captured.update(url=url, headers=headers)
        return FakeResp()

    monkeypatch.setattr(providers.requests, "get", fake_get)
    out = providers.test_provider("anthropic", "sk-ant-live")
    assert out["ok"] is True
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "sk-ant-live"
    assert "anthropic-version" in captured["headers"]


def test_registry_routes_anthropic_through_call_anthropic(monkeypatch):
    providers.refresh()
    captured = {}

    def fake_call_anthropic(system, user, **kw):
        captured.update(kw)
        return adapters.LLMResponse(text="ok", provider="anthropic", model=kw["model"])

    monkeypatch.setattr(adapters, "call_anthropic", fake_call_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    resp = adapters.PROVIDERS["anthropic"]("s", "u")
    assert resp.provider == "anthropic"
    assert captured["model"] == "claude-opus-5"
    assert captured["key_env"] == "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Akıllı varsayılan routing (providers.recommended_routing)
# ---------------------------------------------------------------------------

def test_recommended_routing_prefers_account_over_api(monkeypatch):
    """claude CLI kuruluysa (PATH'te) -> anahtarı olan API sağlayıcıları
    olsa dahi hesap tabanlı 'claude' tercih edilir."""
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    routing = providers.recommended_routing()
    assert routing == {"planner": "claude", "coder": "claude", "reviewer": "claude"}


def test_recommended_routing_falls_back_to_codex_then_gemini_cli(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None)
    routing = providers.recommended_routing()
    assert routing == {"planner": "codex-cli", "coder": "codex-cli", "reviewer": "codex-cli"}


def test_recommended_routing_prefers_anthropic_api_over_other_api_keys(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: None)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(providers, "_probe_local", lambda base_url: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    routing = providers.recommended_routing()
    assert routing == {"planner": "anthropic", "coder": "anthropic", "reviewer": "anthropic"}


def test_recommended_routing_falls_back_to_default_when_nothing_available(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(providers, "_probe_local", lambda base_url: False)
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
                "MISTRAL_API_KEY", "GROQ_API_KEY", "XAI_API_KEY", "DASHSCOPE_API_KEY",
                "MOONSHOT_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    from agents import DEFAULT_ROUTING
    assert providers.recommended_routing() == dict(DEFAULT_ROUTING)


def test_engine_support_fields_present_in_status_of():
    info = providers.status_of(providers.get("anthropic"))
    assert info["engineSupported"] is True
    info_cli = providers.status_of(providers.get("qwen-code"))
    assert info_cli["engineSupported"] is False
    assert info_cli["cliAvailable"] in (True, False)
    assert info_cli["npxAvailable"] in (True, False)


# ---------------------------------------------------------------------------
# adapters.call_anthropic — sahte (fake) anthropic SDK istemcisiyle, ağ yok.
# ---------------------------------------------------------------------------

class _FakeAnthropicTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicThinkingBlock:
    def __init__(self):
        self.type = "thinking"
        self.text = "iç muhakeme — görünmemeli"


class _FakeAnthropicUsage:
    def __init__(self, pin, pout):
        self.input_tokens = pin
        self.output_tokens = pout


class _FakeAnthropicResponse:
    def __init__(self, content, pin=100, pout=50):
        self.content = content
        self.usage = _FakeAnthropicUsage(pin, pout)


class _FakeAnthropicMessages:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    def create(self, **kwargs):
        self._captured.update(kwargs)
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response, captured):
        self.messages = _FakeAnthropicMessages(response, captured)


def test_call_anthropic_reads_text_and_skips_thinking_blocks(monkeypatch):
    captured = {}
    response = _FakeAnthropicResponse([
        _FakeAnthropicThinkingBlock(),
        _FakeAnthropicTextBlock("merhaba"),
    ])

    class _FakeAnthropicModule:
        @staticmethod
        def Anthropic(api_key=None):
            return _FakeAnthropicClient(response, captured)

    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    resp = adapters.call_anthropic(
        "sistem", "görev", model="claude-opus-5",
        pricing={"claude-opus-5": (5.00, 25.00)},
    )
    assert resp.text == "merhaba"
    assert resp.provider == "anthropic"
    assert resp.model == "claude-opus-5"
    assert resp.prompt_tokens == 100 and resp.completion_tokens == 50
    assert resp.cost_usd == pytest.approx(100 / 1e6 * 5.00 + 50 / 1e6 * 25.00)
    assert captured["max_tokens"] == 16000
    assert captured["thinking"] == {"type": "adaptive"}


def test_call_anthropic_omits_thinking_for_haiku(monkeypatch):
    captured = {}
    response = _FakeAnthropicResponse([_FakeAnthropicTextBlock("ok")])

    class _FakeAnthropicModule:
        @staticmethod
        def Anthropic(api_key=None):
            return _FakeAnthropicClient(response, captured)

    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    adapters.call_anthropic("s", "u", model="claude-haiku-4-5")
    assert "thinking" not in captured


def test_call_anthropic_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        adapters.call_anthropic("s", "u", model="claude-opus-5")
