"""fs.* — dosya sistemi köprüsü. Yol güvenliği motor `Project._safe`'te kalır;
editör tam-okuma (MAX_READ_CHARS ajan sınırını aşar) burada yapılır."""

import os

from project import Project, IGNORE_DIRS, IGNORE_EXT
from webhost import state
from webhost.bridge import handler, BridgeError

EDITOR_MAX_BYTES = 5_000_000  # >5MB salt-okunur uyarısı (plan risk 7)


def _require() -> Project:
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


@handler("fs.listDir")
def _list_dir(params, ctx):
    """Bir klasörün doğrudan çocukları (lazy ağaç). rel="" → kök."""
    proj = _require()
    rel = params.get("rel", "") or ""
    base = proj._safe(rel)
    if not os.path.isdir(base):
        raise BridgeError("not_found", "Klasör yok.")
    dirs, files = [], []
    try:
        for name in os.listdir(base):
            full = os.path.join(base, name)
            child_rel = (rel + "/" + name if rel else name).replace("\\", "/")
            if os.path.isdir(full):
                if name in IGNORE_DIRS:
                    continue
                dirs.append({"name": name, "rel": child_rel, "isDir": True, "ext": ""})
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in IGNORE_EXT:
                    continue
                files.append({"name": name, "rel": child_rel, "isDir": False, "ext": ext})
    except OSError as e:
        raise BridgeError("io", str(e))
    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())
    return {"entries": dirs + files}


@handler("fs.readFile")
def _read_file(params, ctx):
    proj = _require()
    p = proj._safe(params.get("rel", ""))
    if not os.path.isfile(p):
        raise BridgeError("not_found", "Dosya yok.")
    size = os.path.getsize(p)
    if size > EDITOR_MAX_BYTES:
        return {"content": "", "truncated": True, "tooLarge": True}
    with open(p, encoding="utf-8", errors="replace") as fh:
        return {"content": fh.read(), "truncated": False}


@handler("fs.writeFile")
def _write_file(params, ctx):
    proj = _require()
    # editör kaydı: yedeksiz yaz (Ctrl+S sık; .bak gürültüsü istenmez)
    proj.apply(params.get("rel", ""), params.get("content", ""), backup=False)
    return {}


@handler("fs.createFile")
def _create_file(params, ctx):
    return {"rel": _require().create_file(params.get("rel", ""))}


@handler("fs.createFolder")
def _create_folder(params, ctx):
    return {"rel": _require().create_folder(params.get("rel", ""))}


@handler("fs.rename")
def _rename(params, ctx):
    return {"rel": _require().rename(params.get("rel", ""), params.get("newName", ""))}


@handler("fs.delete")
def _delete(params, ctx):
    _require().delete(params.get("rel", ""))
    return {}
