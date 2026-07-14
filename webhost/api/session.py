"""session.* — proje-içi oturum kalıcılığı (.imece/session.json).
Açık sekmeler + aktif sekme + kabuk düzeni (panel görünürlük/boyutları, kenar
görünümü — P4); history.py ile aynı .imece klasörünü paylaşır."""

import json
import os

from webhost import state
from webhost.bridge import handler

_DEFAULT = {"openTabs": [], "activeTab": None, "layout": None}


def _path() -> str | None:
    proj = state.get_project()
    if proj is None:
        return None
    return os.path.join(proj.root, ".imece", "session.json")


@handler("session.get")
def _get(params, ctx):
    p = _path()
    if p is None or not os.path.exists(p):
        return dict(_DEFAULT)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {k: data.get(k, v) for k, v in _DEFAULT.items()}
    except (OSError, ValueError):
        return dict(_DEFAULT)


@handler("session.save")
def _save(params, ctx):
    p = _path()
    if p is None:
        return {}
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        clean = {k: params.get(k, v) for k, v in _DEFAULT.items()}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # oturum kaydı kritik değil — sessiz geç
    return {}
