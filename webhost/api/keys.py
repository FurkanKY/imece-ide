"""keys.* — API anahtarı onboarding'i (beta hazırlığı).

Anahtarlar kökteki .env'e yazılır ve os.environ ANINDA güncellenir (adapters
her çağrıda os.getenv okur → yeniden başlatma gerekmez). Claude'un anahtarı yok:
Claude Code CLI varlığı kontrol edilir. Anahtar UI'a asla geri dönmez — yalnız
maske (son 4 hane).
"""

import os
import shutil
from pathlib import Path

from webhost.bridge import handler, BridgeError

ROOT = Path(__file__).resolve().parents[2]  # repo/uygulama kökü
ENV_PATH = ROOT / ".env"

# köprü alan adı → env değişkeni
KEY_VARS = {"deepseek": "DEEPSEEK_API_KEY", "gemini": "GEMINI_API_KEY"}


def _mask(v: str) -> str:
    return ("•••• " + v[-4:]) if len(v) >= 8 else "••••"


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Var olan .env'i satır satır koruyarak günceller; eksik anahtarları ekler.

    Yorumlar ve bilinmeyen satırlar aynen kalır (kullanıcının elle yazdıkları).
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key in remaining and not stripped.startswith("#"):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


@handler("keys.status")
def _status(params, ctx):
    cli = shutil.which(os.getenv("CLAUDE_CLI", "claude"))
    providers: dict = {
        "claude": {
            "ok": bool(cli),
            "detail": cli if cli else "Claude Code CLI PATH'te bulunamadı",
        },
    }
    for name, var in KEY_VARS.items():
        val = (os.getenv(var) or "").strip()
        providers[name] = {"ok": bool(val), "masked": _mask(val) if val else ""}
    return {"providers": providers, "envPath": str(ENV_PATH)}


@handler("keys.set")
def _set(params, ctx):
    updates: dict[str, str] = {}
    for name, var in KEY_VARS.items():
        val = params.get(name)
        if isinstance(val, str) and val.strip():
            updates[var] = val.strip()
    if not updates:
        raise BridgeError("empty", "Kaydedilecek anahtar yok.")
    try:
        write_env(ENV_PATH, updates)
    except OSError as e:
        raise BridgeError("write_failed", f".env yazılamadı: {e}")
    os.environ.update(updates)  # adapters bir sonraki çağrıda görür
    return {}
