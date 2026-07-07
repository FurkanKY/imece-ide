"""settings.* — tercih kalıcılığı (ui_prefs v2 sarmalayıcısı).
Web tarafı camelCase, Python tarafı snake_case — dönüşüm burada."""

import ui_prefs
from webhost.bridge import handler

_TO_JS = {
    "accent": "accent", "density": "density", "enter_to_send": "enterToSend",
    "animations": "animations", "last_project": "lastProject",
    "recent_projects": "recentProjects",
}
_TO_PY = {v: k for k, v in _TO_JS.items()}


@handler("settings.get")
def _get(params, ctx):
    prefs = ui_prefs.load()
    out = {js: prefs[py] for py, js in _TO_JS.items()}
    out["recentProjects"] = [
        {"path": p.get("path", ""), "name": p.get("name", ""),
         "lastOpened": p.get("last_opened", "")}
        for p in (prefs.get("recent_projects") or [])
    ]
    return out


@handler("settings.set")
def _set(params, ctx):
    py = {}
    for js_key, val in params.items():
        py_key = _TO_PY.get(js_key)
        if py_key is None:
            continue
        if py_key == "recent_projects":
            val = [{"path": p.get("path", ""), "name": p.get("name", ""),
                    "last_opened": p.get("lastOpened", "")} for p in (val or [])]
        py[py_key] = val
    ui_prefs.save(py)
    return {}
