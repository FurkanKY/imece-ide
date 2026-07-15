"""keys.* — API anahtarı onboarding'i.

Sağlayıcı listesi providers.py kataloğundan gelir: anahtar isteyen her API
sağlayıcısı + PATH'te aranan ajan CLI'ları. Paketli Windows uygulamasında
anahtarlar DPAPI korumalı depoya; kaynak modunda geliştirici uyumluluğu için
.env'e yazılır. Her iki durumda adapters anahtarı os.environ'dan okur; anahtar
UI'a asla geri dönmez, yalnız son dört haneli maske gösterilir.
"""

import os
from pathlib import Path

import providers
from runtime_paths import env_path
from webhost.bridge import handler, BridgeError
from secret_store import SecretStoreError, packaged_store

ENV_PATH = env_path()


def _key_vars() -> dict[str, str]:
    """köprü sağlayıcı id'si → env değişkeni (anahtar isteyen API sağlayıcıları)."""
    return {
        e["id"]: e["key_env"]
        for e in providers.catalog()
        if e["kind"] == "openai" and e.get("key_env")
    }


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


def clear_env_keys(path: Path, keys: set[str]) -> None:
    """Başarılı paket geçişinden sonra yalnız gizli değerleri boşaltır."""
    if not path.exists():
        return
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.strip().split("=", 1)[0].strip() if "=" in line else ""
        out.append(f"{key}=" if key in keys and not line.lstrip().startswith("#") else line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _secure_values() -> dict[str, str]:
    store = packaged_store()
    if not store:
        return {}
    values = store.load()
    # Önceki paket sürümündeki düz metin .env yalnız bir kez taşınır.
    legacy = {var: (os.getenv(var) or "").strip() for var in _key_vars().values()}
    legacy = {key: value for key, value in legacy.items() if value and key not in values}
    if legacy:
        store.save(legacy)
        values.update(legacy)
        clear_env_keys(ENV_PATH, set(legacy))
    os.environ.update(values)
    return values


@handler("keys.status")
def _status(params, ctx):
    try:
        _secure_values()
    except SecretStoreError as exc:
        raise BridgeError("secret_store", str(exc)) from exc
    result: dict = {}
    for entry in providers.catalog():
        info = providers.status_of(entry)
        if entry["kind"] == "openai" and entry.get("key_env"):
            val = os.getenv(entry["key_env"], "").strip()
            info["masked"] = _mask(val) if val else ""
        result[entry["id"]] = info
    return {"providers": result, "envPath": str(ENV_PATH)}


@handler("keys.set")
def _set(params, ctx):
    key_vars = _key_vars()
    updates: dict[str, str] = {}
    for name, val in params.items():
        var = key_vars.get(name)
        if var and isinstance(val, str) and val.strip():
            updates[var] = val.strip()
    if not updates:
        raise BridgeError("empty", "Kaydedilecek anahtar yok.")
    try:
        store = packaged_store()
        if store:
            store.save(updates)
            clear_env_keys(ENV_PATH, set(updates))
        else:
            ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_env(ENV_PATH, updates)
    except (OSError, SecretStoreError) as e:
        raise BridgeError("write_failed", f"Anahtar güvenle kaydedilemedi: {e}")
    os.environ.update(updates)  # adapters bir sonraki çağrıda görür
    return {}


@handler("keys.test")
def _test(params, ctx):
    provider_id = (params.get("provider") or "").strip()
    try:
        return providers.test_provider(provider_id, params.get("key"))
    except ValueError as e:
        raise BridgeError("unknown_provider", str(e))
