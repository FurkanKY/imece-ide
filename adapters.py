"""
adapters.py
------------
Her AI modeline BAĞLANAN fonksiyonlar. Hepsi aynı imzaya sahip:

    fonksiyon(system_prompt, user_prompt) -> LLMResponse

LLMResponse sadece metni değil, ölçüm (observability) için token/maliyet/süre
bilgisini de taşır. Böylece arayüzde her adımın kaça mal olduğunu görebiliriz.
"""

import os
import json
import time
import shutil
import subprocess
from dataclasses import dataclass, field

import requests

# Yaklaşık fiyat tablosu — USD / 1 milyon token (girdi, çıktı).
# Bunlar tahminidir; kendi planına göre güncelleyebilirsin.
PRICING = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-v4-pro": (0.27, 1.10),
    "gemini-3.5-flash": (0.075, 0.30),
    "gemini-flash-latest": (0.075, 0.30),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-3.1-pro-preview": (1.25, 5.00),
}


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)


def _estimate_cost(model: str, pin: int, pout: int) -> float:
    """Fiyat tablosundan yaklaşık maliyet hesapla."""
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    return pin / 1_000_000 * price_in + pout / 1_000_000 * price_out


# ---------------------------------------------------------------------------
# 0) GENEL — OpenAI-uyumlu /chat/completions. providers.py kataloğundaki her
#    API sağlayıcısı (DeepSeek, Gemini, OpenAI, Ollama, özel uçlar...) bu tek
#    fonksiyonla çağrılır; sağlayıcıya özgü olan yalnız konfigürasyondur.
# ---------------------------------------------------------------------------
def call_openai_compat(
    system_prompt: str,
    user_prompt: str,
    *,
    provider_id: str,
    base_url: str,
    model: str,
    key_env: str | None = None,
    pricing: dict | None = None,
) -> LLMResponse:
    headers = {}
    if key_env:
        api_key = os.getenv(key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"{key_env} tanımlı değil (Ayarlar'dan {provider_id} anahtarını ekleyin).")
        headers["Authorization"] = f"Bearer {api_key}"

    t0 = time.time()
    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        },
        timeout=600,
    )
    latency = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage") or {}
    pin = usage.get("prompt_tokens", 0)
    pout = usage.get("completion_tokens", 0)
    price_in, price_out = (pricing or {}).get(model, (0.0, 0.0))
    return LLMResponse(
        text=(data["choices"][0]["message"].get("content") or "").strip(),
        provider=provider_id,
        model=model,
        prompt_tokens=pin,
        completion_tokens=pout,
        latency_s=latency,
        cost_usd=pin / 1_000_000 * price_in + pout / 1_000_000 * price_out,
    )


def _run_cli(argv: list[str]) -> tuple[str, float]:
    """Ajan CLI'sını headless çalıştırır; (stdout, süre) döner."""
    t0 = time.time()
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    latency = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(f"'{argv[0]}' hatası:\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout, latency


def _find_cli(env_var: str, default: str) -> str:
    cli = shutil.which(os.getenv(env_var, "") or default)
    if not cli:
        raise RuntimeError(f"'{default}' komutu bulunamadı ({default} CLI PATH'te mi?).")
    return cli


# ---------------------------------------------------------------------------
# 1) CLAUDE — Claude Code CLI (headless), JSON çıktısıyla (maliyet/token için).
# ---------------------------------------------------------------------------
def call_claude(system_prompt: str, user_prompt: str) -> LLMResponse:
    cli = shutil.which(os.getenv("CLAUDE_CLI", "claude"))
    if not cli:
        raise RuntimeError("'claude' komutu bulunamadı (Claude Code CLI PATH'te mi?).")

    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    t0 = time.time()
    result = subprocess.run(
        [cli, "-p", full_prompt, "--output-format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    latency = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI hatası:\n{result.stderr}")

    data = json.loads(result.stdout)
    usage = data.get("usage", {})
    return LLMResponse(
        text=data.get("result", "").strip(),
        provider="claude",
        model="claude-code",
        prompt_tokens=usage.get("input_tokens", 0),
        completion_tokens=usage.get("output_tokens", 0),
        latency_s=latency,
        cost_usd=data.get("total_cost_usd", 0.0),
    )


# ---------------------------------------------------------------------------
# 2) DEEPSEEK — OpenAI-uyumlu web API.
# ---------------------------------------------------------------------------
def call_deepseek(system_prompt: str, user_prompt: str) -> LLMResponse:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY .env dosyasında boş.")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    t0 = time.time()
    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        },
        timeout=600,
    )
    latency = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {})
    pin = usage.get("prompt_tokens", 0)
    pout = usage.get("completion_tokens", 0)
    return LLMResponse(
        text=data["choices"][0]["message"]["content"].strip(),
        provider="deepseek",
        model=model,
        prompt_tokens=pin,
        completion_tokens=pout,
        latency_s=latency,
        cost_usd=_estimate_cost(model, pin, pout),
    )


# ---------------------------------------------------------------------------
# 3) GEMINI — Google web API.
# ---------------------------------------------------------------------------
def call_gemini(system_prompt: str, user_prompt: str) -> LLMResponse:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY .env dosyasında boş.")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    t0 = time.time()
    resp = requests.post(
        url,
        headers={"x-goog-api-key": api_key},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
        },
        timeout=600,
    )
    latency = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    um = data.get("usageMetadata", {})
    pin = um.get("promptTokenCount", 0)
    pout = um.get("candidatesTokenCount", 0)
    # Bazı cevaplarda parça birden fazla olabilir; hepsini birleştir.
    parts = data["candidates"][0]["content"].get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return LLMResponse(
        text=text,
        provider="gemini",
        model=model,
        prompt_tokens=pin,
        completion_tokens=pout,
        latency_s=latency,
        cost_usd=_estimate_cost(model, pin, pout),
    )


# ---------------------------------------------------------------------------
# 4) DİĞER AJAN CLI'LARI — Claude Code deseninin uyarlamaları: headless çağrı,
#    JSON çıktı, metin + (varsa) token sayıları. Ayrıntılı alan adları CLI'dan
#    CLI'ya değişir; parser'lar eksik alanlara toleranslıdır.
# ---------------------------------------------------------------------------
def _call_gemini_style_cli(env_var: str, default_cmd: str, provider_id: str,
                           system_prompt: str, user_prompt: str) -> LLMResponse:
    """Gemini CLI ve çatalları (Qwen Code): `-p PROMPT -o json`."""
    cli = _find_cli(env_var, default_cmd)
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    stdout, latency = _run_cli([cli, "-p", full_prompt, "-o", "json"])
    data = json.loads(stdout)
    pin = pout = 0
    model = provider_id
    for name, stats in (data.get("stats", {}).get("models") or {}).items():
        model = name
        tokens = stats.get("tokens", {})
        pin += int(tokens.get("prompt", 0))
        pout += int(tokens.get("candidates", 0))
    return LLMResponse(
        text=(data.get("response") or "").strip(),
        provider=provider_id,
        model=model,
        prompt_tokens=pin,
        completion_tokens=pout,
        latency_s=latency,
    )


def call_gemini_cli(system_prompt: str, user_prompt: str) -> LLMResponse:
    return _call_gemini_style_cli("GEMINI_CLI", "gemini", "gemini-cli", system_prompt, user_prompt)


def call_qwen_code(system_prompt: str, user_prompt: str) -> LLMResponse:
    return _call_gemini_style_cli("QWEN_CLI", "qwen", "qwen-code", system_prompt, user_prompt)


def call_codex_cli(system_prompt: str, user_prompt: str) -> LLMResponse:
    """Codex CLI: `codex exec --json PROMPT` → satır başına bir JSON olayı."""
    cli = _find_cli("CODEX_CLI", "codex")
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    stdout, latency = _run_cli([cli, "exec", "--json", full_prompt])
    text, pin, pout = "", 0, 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            text = item["text"]  # son ajan mesajı nihai cevaptır
        usage = ev.get("usage") or {}
        pin += int(usage.get("input_tokens", 0))
        pout += int(usage.get("output_tokens", 0))
    if not text:
        raise RuntimeError("Codex CLI çıktısında ajan mesajı bulunamadı.")
    return LLMResponse(
        text=text.strip(),
        provider="codex-cli",
        model="codex",
        prompt_tokens=pin,
        completion_tokens=pout,
        latency_s=latency,
    )


# Ajan CLI'ları — providers.py kataloğu bu tablodan beslenir.
CLI_AGENTS = {
    "claude": call_claude,
    "gemini-cli": call_gemini_cli,
    "codex-cli": call_codex_cli,
    "qwen-code": call_qwen_code,
}

# Geriye dönük uyumlu sağlayıcı tablosu. providers.refresh() bu sözlüğü
# katalogla genişletir/günceller (gemini → OpenAI-uyumlu uca taşınır).
PROVIDERS = {
    "claude": call_claude,
    "deepseek": call_deepseek,
    "gemini": call_gemini,
}
