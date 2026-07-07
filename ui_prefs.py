"""
ui_prefs.py
-----------
Faz 5 — kullanıcı arayüz tercihlerinin kalıcılığı (hafif JSON, harici bağımlılık yok).

Tercihler kullanıcı ev dizininde ~/.multi_agent_ide/prefs.json altında tutulur.
theme.set_accent / set_density ve anim.set_enabled bunları okur; ayarlar ekranı yazar.
"""

import os
import json

_DIR = os.path.join(os.path.expanduser("~"), ".multi_agent_ide")
_PATH = os.path.join(_DIR, "prefs.json")

DEFAULTS = {
    "accent": "blue",           # theme.ACCENTS anahtarı
    "density": "comfortable",   # comfortable | compact
    "enter_to_send": True,      # Enter gönderir / Shift+Enter yeni satır
    "animations": True,         # mikro-animasyonlar açık mı
    # ---- v2 (web-shell, bkz. .claude/plans/web-shell-ui.md) ----
    "window": None,             # {x, y, w, h, maximized} | None
    "last_project": None,       # son açık proje kökü | None
    "recent_projects": [],      # [{path, name, last_opened}]
}


def load() -> dict:
    """Tercihleri oku; eksik/bozuksa varsayılana düş."""
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: data.get(k, v) for k, v in DEFAULTS.items()}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(prefs: dict) -> None:
    """Tercihleri diske yaz. Var olan dosyayla BİRLEŞTİRİR: çağıranın vermediği
    bilinen anahtarlar korunur (classic ↔ web-shell birbirinin alanını ezmesin)."""
    try:
        os.makedirs(_DIR, exist_ok=True)
        current = load()
        clean = {k: prefs.get(k, current[k]) for k in DEFAULTS}
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    except OSError:
        pass
