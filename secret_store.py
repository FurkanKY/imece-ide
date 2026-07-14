"""Windows paketinde API anahtarlarını DPAPI ile yerelde korur.

Kaynak modunda geliştiricinin .env akışı korunur. Paketli Windows sürümünde ise
anahtarlar %LOCALAPPDATA%/ImeceIDE/secrets.dat içinde, kullanıcı hesabına
bağlı Windows Data Protection API ile şifrelenir.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from runtime_paths import app_data_dir, is_frozen

_KEYS = ("DEEPSEEK_API_KEY", "GEMINI_API_KEY")


class SecretStoreError(RuntimeError):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, object]:
    buf = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Windows DPAPI yalnız Windows'ta kullanılabilir.")
    source, _source_buf = _blob(data)
    target = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(ctypes.byref(source), "ImeceIDE", None, None, None, 0, ctypes.byref(target)):
        raise SecretStoreError("Windows anahtar koruması başarısız oldu.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Windows DPAPI yalnız Windows'ta kullanılabilir.")
    source, _source_buf = _blob(data)
    target = _DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise SecretStoreError("Windows anahtar deposu çözülemedi.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


class SecretStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "secrets.dat"

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = _unprotect(base64.b64decode(self.path.read_bytes(), validate=True))
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, SecretStoreError) as exc:
            raise SecretStoreError("Anahtar deposu okunamadı.") from exc
        if not isinstance(data, dict):
            raise SecretStoreError("Anahtar deposu geçersiz.")
        return {key: value for key, value in data.items() if key in _KEYS and isinstance(value, str) and value}

    def save(self, updates: dict[str, str]) -> None:
        data = self.load()
        data.update({key: value for key, value in updates.items() if key in _KEYS and value})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = base64.b64encode(_protect(json.dumps(data, ensure_ascii=False).encode("utf-8")))
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_bytes(encoded)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()


def packaged_store() -> SecretStore | None:
    """Yalnız paketli Windows uygulamasında güvenli depo kullanılır."""
    return SecretStore() if is_frozen() and os.name == "nt" else None
