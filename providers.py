"""
providers.py
------------
Sağlayıcı KATALOĞU ve KAYIT (registry).

Katalog: uygulamayla gelen hazır tanımlar — OpenAI-uyumlu API sağlayıcıları ve
ajan CLI'ları. Kullanıcı Ayarlar'dan bir sağlayıcıyı anahtarını girerek (veya
CLI'ı kurarak) kullanılabilir hale getirir; katalogda olmayan uç noktalar için
"özel" OpenAI-uyumlu kayıt ekleyebilir.

Kayıt, adapters.PROVIDERS sözlüğünü geriye dönük uyumlu biçimde besler:
refresh() sonrası her sağlayıcı id'si → fonksiyon(system, user) -> LLMResponse.
Kullanıcı seçimleri (model tercihi + özel sağlayıcılar) providers.json'da durur.
"""

from __future__ import annotations

import json
import os
import shutil
import time

import requests

import adapters
from runtime_paths import providers_config_path

# ---------------------------------------------------------------------------
# Katalog. kind="openai": OpenAI-uyumlu /chat/completions ucu.
# kind="cli": PATH'teki ajan CLI'sı (anahtar yerine kurulum + kendi girişi).
# models: [id, girdi $/1M, çıktı $/1M] — fiyatlar tahminidir, UI'da "≈" gösterilir.
# ---------------------------------------------------------------------------
CATALOG: list[dict] = [
    {
        "id": "deepseek", "label": "DeepSeek", "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY", "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "models": [["deepseek-chat", 0.27, 1.10], ["deepseek-reasoner", 0.55, 2.19]],
        "key_hint": "sk-…", "docs_url": "https://platform.deepseek.com",
    },
    {
        "id": "gemini", "label": "Gemini", "kind": "openai",
        # Google'ın resmî OpenAI-uyumlu ucu — özel Gemini adapter'ına gerek kalmaz.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY", "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
        "models": [
            ["gemini-2.5-flash", 0.075, 0.30],
            ["gemini-flash-latest", 0.075, 0.30],
            ["gemini-3.5-flash", 0.075, 0.30],
            ["gemini-3.1-pro-preview", 1.25, 5.00],
        ],
        "key_hint": "AIza…", "docs_url": "https://aistudio.google.com/apikey",
    },
    {
        "id": "openai", "label": "OpenAI", "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL",
        "default_model": "gpt-5.1",
        "models": [["gpt-5.1", 1.25, 10.00], ["gpt-5.1-mini", 0.25, 2.00]],
        "key_hint": "sk-…", "docs_url": "https://platform.openai.com/api-keys",
    },
    {
        "id": "mistral", "label": "Mistral", "kind": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY", "model_env": "MISTRAL_MODEL",
        "default_model": "mistral-large-latest",
        "models": [["mistral-large-latest", 2.00, 6.00], ["codestral-latest", 0.30, 0.90]],
        "key_hint": "…", "docs_url": "https://console.mistral.ai/api-keys",
    },
    {
        "id": "groq", "label": "Groq", "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY", "model_env": "GROQ_MODEL",
        "default_model": "llama-3.3-70b-versatile",
        "models": [["llama-3.3-70b-versatile", 0.59, 0.79]],
        "key_hint": "gsk_…", "docs_url": "https://console.groq.com/keys",
    },
    {
        "id": "xai", "label": "xAI (Grok)", "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY", "model_env": "XAI_MODEL",
        "default_model": "grok-4",
        "models": [["grok-4", 3.00, 15.00], ["grok-code-fast-1", 0.20, 1.50]],
        "key_hint": "xai-…", "docs_url": "https://console.x.ai",
    },
    {
        "id": "qwen", "label": "Qwen (DashScope)", "kind": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY", "model_env": "QWEN_MODEL",
        "default_model": "qwen3-coder-plus",
        "models": [["qwen3-coder-plus", 1.00, 5.00], ["qwen-plus", 0.40, 1.20]],
        "key_hint": "sk-…", "docs_url": "https://modelstudio.console.alibabacloud.com",
    },
    {
        "id": "moonshot", "label": "Moonshot (Kimi)", "kind": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "key_env": "MOONSHOT_API_KEY", "model_env": "MOONSHOT_MODEL",
        "default_model": "kimi-k2-turbo-preview",
        "models": [["kimi-k2-turbo-preview", 0.60, 2.50]],
        "key_hint": "sk-…", "docs_url": "https://platform.moonshot.ai",
    },
    {
        "id": "openrouter", "label": "OpenRouter", "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY", "model_env": "OPENROUTER_MODEL",
        "default_model": "openrouter/auto",
        "models": [["openrouter/auto", 0.0, 0.0]],
        "key_hint": "sk-or-…", "docs_url": "https://openrouter.ai/keys",
    },
    {
        "id": "ollama", "label": "Ollama (yerel)", "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "key_env": None, "model_env": "OLLAMA_MODEL",
        "default_model": "qwen2.5-coder",
        "models": [["qwen2.5-coder", 0.0, 0.0], ["llama3.1", 0.0, 0.0]],
        "key_hint": "", "docs_url": "https://ollama.com",
    },
    # ---- Ajan CLI'ları (Claude Code deseni) ----
    {
        "id": "claude", "label": "Claude Code", "kind": "cli",
        "command_env": "CLAUDE_CLI", "default_command": "claude",
        "docs_url": "https://claude.com/claude-code",
    },
    {
        "id": "gemini-cli", "label": "Gemini CLI", "kind": "cli",
        "command_env": "GEMINI_CLI", "default_command": "gemini",
        "docs_url": "https://github.com/google-gemini/gemini-cli",
    },
    {
        "id": "codex-cli", "label": "Codex CLI", "kind": "cli",
        "command_env": "CODEX_CLI", "default_command": "codex",
        "docs_url": "https://github.com/openai/codex",
    },
    {
        "id": "qwen-code", "label": "Qwen Code", "kind": "cli",
        "command_env": "QWEN_CLI", "default_command": "qwen",
        "docs_url": "https://github.com/QwenLM/qwen-code",
    },
]

# Özel kayıtlar için zorunlu alanlar (OpenAI-uyumlu varsayılır).
_CUSTOM_REQUIRED = ("id", "label", "base_url", "default_model")


# ---------------------------------------------------------------------------
# Kullanıcı konfigürasyonu: {"custom": [entry...], "models": {id: model}}
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    path = providers_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"custom": list(data.get("custom") or []),
                    "models": dict(data.get("models") or {})}
    except (OSError, ValueError):
        pass
    return {"custom": [], "models": {}}


def _save_config(cfg: dict) -> None:
    path = providers_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _custom_key_env(pid: str) -> str:
    """Özel sağlayıcının anahtarı için türetilmiş env adı."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in pid).upper()
    return f"IMECE_CUSTOM_{slug}_API_KEY"


def catalog() -> list[dict]:
    """Yerleşik katalog + kullanıcının özel kayıtları (kopya döner)."""
    entries = [dict(e) for e in CATALOG]
    for raw in _load_config()["custom"]:
        entry = dict(raw)
        entry.setdefault("kind", "openai")
        entry.setdefault("custom", True)
        entry.setdefault("key_env", _custom_key_env(entry["id"]))
        entry.setdefault("model_env", None)
        entry.setdefault("models", [[entry["default_model"], 0.0, 0.0]])
        entries.append(entry)
    return entries


def get(provider_id: str) -> dict | None:
    for entry in catalog():
        if entry["id"] == provider_id:
            return entry
    return None


def selected_model(entry: dict) -> str:
    """Model önceliği: kullanıcı seçimi → env değişkeni → katalog varsayılanı."""
    chosen = _load_config()["models"].get(entry["id"])
    if chosen:
        return chosen
    if entry.get("model_env"):
        env = os.getenv(entry["model_env"], "").strip()
        if env:
            return env
    return entry.get("default_model", "")


def set_model(provider_id: str, model: str) -> None:
    if get(provider_id) is None:
        raise ValueError(f"Bilinmeyen sağlayıcı: {provider_id}")
    cfg = _load_config()
    cfg["models"][provider_id] = model.strip()
    _save_config(cfg)
    refresh()


def add_custom(entry: dict) -> dict:
    missing = [k for k in _CUSTOM_REQUIRED if not str(entry.get(k, "")).strip()]
    if missing:
        raise ValueError(f"Eksik alanlar: {', '.join(missing)}")
    pid = entry["id"].strip()
    if get(pid) is not None:
        raise ValueError(f"'{pid}' id'si zaten kullanımda.")
    clean = {
        "id": pid,
        "label": entry["label"].strip(),
        "kind": "openai",
        "base_url": entry["base_url"].strip().rstrip("/"),
        "default_model": entry["default_model"].strip(),
        "custom": True,
    }
    cfg = _load_config()
    cfg["custom"].append(clean)
    _save_config(cfg)
    refresh()
    return dict(clean)


def remove_custom(provider_id: str) -> None:
    cfg = _load_config()
    before = len(cfg["custom"])
    cfg["custom"] = [c for c in cfg["custom"] if c.get("id") != provider_id]
    if len(cfg["custom"]) == before:
        raise ValueError(f"Özel sağlayıcı bulunamadı: {provider_id}")
    cfg["models"].pop(provider_id, None)
    _save_config(cfg)
    adapters.PROVIDERS.pop(provider_id, None)
    refresh()


# ---------------------------------------------------------------------------
# Durum ve doğrulama
# ---------------------------------------------------------------------------
def _cli_path(entry: dict) -> str | None:
    return shutil.which(os.getenv(entry.get("command_env") or "", "") or entry["default_command"])


_probe_cache: dict[str, tuple[float, bool]] = {}


def _probe_local(base_url: str) -> bool:
    """Anahtarsız yerel uç (Ollama) için hızlı erişilebilirlik kontrolü; 30 sn önbellek."""
    ts, ok = _probe_cache.get(base_url, (0.0, False))
    if time.time() - ts < 30:
        return ok
    try:
        ok = requests.get(f"{base_url}/models", timeout=0.5).ok
    except requests.RequestException:
        ok = False
    _probe_cache[base_url] = (time.time(), ok)
    return ok


def is_ready(entry: dict) -> bool:
    if entry["kind"] == "cli":
        return bool(_cli_path(entry))
    if not entry.get("key_env"):  # anahtarsız (ör. Ollama) — sunucu ayaktaysa hazır
        return _probe_local(entry["base_url"])
    return bool(os.getenv(entry["key_env"], "").strip())


def status_of(entry: dict) -> dict:
    """UI listesi için tek sağlayıcının durumu (anahtar değeri asla dönmez)."""
    info = {
        "id": entry["id"], "label": entry["label"], "kind": entry["kind"],
        "custom": bool(entry.get("custom")), "ok": is_ready(entry),
        "docsUrl": entry.get("docs_url", ""),
    }
    if entry["kind"] == "cli":
        path = _cli_path(entry)
        info["detail"] = path or f"'{entry['default_command']}' PATH'te bulunamadı"
    else:
        info["model"] = selected_model(entry)
        info["models"] = [m[0] for m in entry.get("models", [])]
        info["keyHint"] = entry.get("key_hint", "")
        info["keyless"] = not entry.get("key_env")
    return info


def test_provider(provider_id: str, api_key: str | None = None) -> dict:
    """Ucuz canlı doğrulama: GET {base}/models. Anahtar verilirse onunla dener
    (kaydetmeden önce test), verilmezse ortamdaki anahtarla."""
    entry = get(provider_id)
    if entry is None or entry["kind"] != "openai":
        raise ValueError(f"Test edilemeyen sağlayıcı: {provider_id}")
    key = (api_key or "").strip()
    if not key and entry.get("key_env"):
        key = os.getenv(entry["key_env"], "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = requests.get(f"{entry['base_url']}/models", headers=headers, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "code": "network", "detail": f"Bağlantı kurulamadı: {e.__class__.__name__}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "code": "auth", "detail": "Anahtar reddedildi (401/403)."}
    if not resp.ok:
        return {"ok": False, "code": "http", "detail": f"Beklenmeyen yanıt: HTTP {resp.status_code}"}
    return {"ok": True, "code": "", "detail": ""}


# ---------------------------------------------------------------------------
# adapters.PROVIDERS beslemesi
# ---------------------------------------------------------------------------
def _make_caller(entry: dict):
    if entry["kind"] == "cli":
        return adapters.CLI_AGENTS[entry["id"]]

    def _call(system_prompt: str, user_prompt: str) -> adapters.LLMResponse:
        live = get(entry["id"]) or entry  # model/base güncel kalsın
        pricing = {m[0]: (m[1], m[2]) for m in live.get("models", [])}
        return adapters.call_openai_compat(
            system_prompt, user_prompt,
            provider_id=live["id"], base_url=live["base_url"],
            model=selected_model(live), key_env=live.get("key_env"),
            pricing=pricing,
        )

    return _call


def refresh() -> None:
    """Kataloğu adapters.PROVIDERS'a yansıtır (mevcut id'ler dahil güncellenir)."""
    for entry in catalog():
        if entry["kind"] == "cli" and entry["id"] not in adapters.CLI_AGENTS:
            continue
        adapters.PROVIDERS[entry["id"]] = _make_caller(entry)


refresh()
