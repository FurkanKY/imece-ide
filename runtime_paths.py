"""Kaynak ve PyInstaller paketli çalışma için merkezi yol çözümleme.

Paket içeriği salt-okunur kabul edilir; kullanıcı tarafından değişen her şey
LOCALAPPDATA/MultiAgentIDE altında tutulur. Kaynak modunda mevcut geliştirme
yolları korunur, böylece yerel kurulumlar sessizce taşınmaz.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "MultiAgentIDE"
SOURCE_ROOT = Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Paket içeriğinin kökü; kaynak modunda depo kökü."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return SOURCE_ROOT


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def app_data_dir() -> Path:
    """Paketli modun yazılabilir kullanıcı-veri dizini."""
    if not is_frozen():
        return Path.home() / ".multi_agent_ide"
    base = os.getenv("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def env_path() -> Path:
    return app_data_dir() / ".env" if is_frozen() else SOURCE_ROOT / ".env"


def prefs_path() -> Path:
    return app_data_dir() / "prefs.json"


def log_path() -> Path:
    return app_data_dir() / "logs" / "app.log"


def helper_executable(name: str) -> Path | None:
    """Ana exe yanındaki paketli yardımcı programı döndürür."""
    if not is_frozen():
        return None
    suffix = ".exe" if os.name == "nt" else ""
    candidate = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    return candidate if candidate.exists() else None
