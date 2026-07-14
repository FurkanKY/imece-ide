"""runconfig.py — "bunu çalıştır" komut sezgisi + proje-içi kalıcılık.

Eski ROADMAP Faz 2 tasarımının portu (web-shell/P8.1). Motor kuralına uygun:
yeni bağımsız modül, mevcut motor dosyalarına dokunmaz.

Öncelik sırası (proje komutu):
  1. .imece/run.json  {"project": "<komut>"}  — kullanıcı ne yazdıysa o
  2. package.json  scripts.dev → `npm run dev`, scripts.start → `npm start`
  3. Cargo.toml → `cargo run` · go.mod → `go run .`
  4. main.py → `python main.py` · app.py → `python app.py`

Dosya komutu uzantıdan gelir (FILE_CMDS); bilinmeyen uzantı → None (UI yol gösterir).
"""

import json
import hashlib
from pathlib import Path

FILE_CMDS = {
    ".py": 'python "{file}"',
    ".js": 'node "{file}"',
    ".mjs": 'node "{file}"',
}

_RUN_JSON = ".imece/run.json"


def file_command(rel: str) -> str | None:
    """Aktif dosya için koşu komutu (proje köküne görece çalıştırılır)."""
    ext = Path(rel).suffix.lower()
    tpl = FILE_CMDS.get(ext)
    return tpl.format(file=rel) if tpl else None


def load_project_command(root: str) -> str | None:
    """Kayıtlı proje komutu (yalnız kullanıcı kaydı; sezgi ayrı)."""
    try:
        data = json.loads((Path(root) / _RUN_JSON).read_text(encoding="utf-8"))
        cmd = data.get("project")
        return cmd if isinstance(cmd, str) and cmd.strip() else None
    except (OSError, ValueError):
        return None


def save_project_command(root: str, command: str) -> None:
    p = Path(root) / _RUN_JSON
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["project"] = command
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_project_command(root: str) -> str | None:
    """Kayıt yoksa sezgi: proje tipine göre makul varsayılan."""
    r = Path(root)
    try:
        pkg = json.loads((r / "package.json").read_text(encoding="utf-8"))
        scripts = pkg.get("scripts") or {}
        if "dev" in scripts:
            return "npm run dev"
        if "start" in scripts:
            return "npm start"
    except (OSError, ValueError):
        pass
    if (r / "Cargo.toml").exists():
        return "cargo run"
    if (r / "go.mod").exists():
        return "go run ."
    for entry in ("main.py", "app.py"):
        if (r / entry).exists():
            return f'python "{entry}"'
    return None


def project_command(root: str) -> str | None:
    return load_project_command(root) or detect_project_command(root)


def resolve(root: str, rel: str | None = None, command: str | None = None) -> dict | None:
    """Çalıştırma komutunu ve kaynağını döndürür; güven onayı bunun üstüne kurulur."""
    if command:
        return {"command": command, "source": "explicit"}
    if rel:
        cmd = file_command(rel)
        return {"command": cmd, "source": "file"} if cmd else None
    saved = load_project_command(root)
    if saved:
        return {"command": saved, "source": "project_config"}
    detected = detect_project_command(root)
    return {"command": detected, "source": "detected"} if detected else None


def fingerprint(root: str, command: str, source: str) -> str:
    raw = f"{root}\0{source}\0{command}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
