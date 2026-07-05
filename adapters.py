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


PROVIDERS = {
    "claude": call_claude,
    "deepseek": call_deepseek,
    "gemini": call_gemini,
}
