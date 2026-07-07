"""project.* — proje açma / listeleme (motor `project.py` sarmalanır)."""

import os
import time

import ui_prefs
from project import Project
from webhost import state
from webhost.bridge import handler, BridgeError


@handler("project.open")
def _open(params, ctx):
    path = params.get("path") or ""
    if not path or not os.path.isdir(path):
        raise BridgeError("not_found", "Klasör bulunamadı.")
    proj = state.set_project(path)
    name = os.path.basename(proj.root)

    # son projeler + last_project kalıcılığı (prefs v2)
    prefs = ui_prefs.load()
    recent = [p for p in (prefs.get("recent_projects") or []) if p.get("path") != proj.root]
    recent.insert(0, {"path": proj.root, "name": name,
                      "last_opened": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    ui_prefs.save({"last_project": proj.root, "recent_projects": recent[:12]})
    return {"root": proj.root, "name": name}


@handler("project.listFiles")
def _list_files(params, ctx):
    proj = _require()
    # Ctrl+P için: 500 ajan-context sınırını aş (plan risk 7).
    return {"files": proj.list_files(max_files=100000)}


def _require() -> Project:
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj
