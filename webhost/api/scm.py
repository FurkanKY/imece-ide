"""scm.* — git kaynak denetimi köprüsü (P4).
Git CLI alt-süreçleriyle konuşur (harici bağımlılık yok): status/diff/stage/
unstage/discard/commit. Tüm komutlar proje kökünde, UTF-8 zorlamalı çalışır
(cp1254 tuzağı — bkz. docs/SETUP.md)."""

import os
import subprocess

from webhost import state
from webhost.bridge import handler, BridgeError

_GIT_TIMEOUT = 20  # sn — büyük repolarda status/diff için yeterli


def _root() -> str:
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj.root


def _git(args: list[str], check: bool = True) -> str:
    """git komutu çalıştır → stdout. check=True iken hata çıktısıyla BridgeError."""
    env = {**os.environ, "PYTHONUTF8": "1", "GIT_TERMINAL_PROMPT": "0",
           "LC_ALL": "C.UTF-8"}
    try:
        cp = subprocess.run(
            ["git", *args], cwd=_root(), capture_output=True,
            stdin=subprocess.DEVNULL,  # git asla girdi beklemesin
            encoding="utf-8", errors="replace", env=env, timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise BridgeError("git_missing", "git bulunamadı (PATH'te değil).")
    except subprocess.TimeoutExpired:
        raise BridgeError("git_timeout", "git komutu zaman aşımına uğradı.")
    if check and cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "").strip() or f"git {args[0]} başarısız."
        raise BridgeError("git", msg[:400])
    return cp.stdout


def _is_repo() -> bool:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=_root(),
            capture_output=True, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
            env=env, timeout=_GIT_TIMEOUT,
        )
        return cp.returncode == 0 and cp.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _parse_porcelain_z(out: str):
    """`git status --porcelain=v1 -z` → staged[] / unstaged[] listeleri.
    Kayıt: 'XY yol' (NUL ayraçlı); R/C kayıtlarında ARDINDAN eski yol gelir."""
    staged, unstaged = [], []
    toks = out.split("\0")
    i = 0
    while i < len(toks):
        entry = toks[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:].replace("\\", "/")
        orig = None
        if x in "RC" or y in "RC":  # rename/copy: sonraki token eski yol
            if i < len(toks):
                orig = toks[i].replace("\\", "/")
                i += 1
        if x == "?" and y == "?":
            unstaged.append({"path": path, "status": "U"})  # untracked
            continue
        if x not in " ?":
            staged.append({"path": path, "status": x, "origPath": orig})
        if y not in " ?":
            unstaged.append({"path": path, "status": y})
    return staged, unstaged


@handler("scm.status")
def _status(params, ctx):
    if not _is_repo():
        return {"isRepo": False, "branch": "", "ahead": 0, "behind": 0,
                "staged": [], "unstaged": []}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip() or "HEAD"
    ahead = behind = 0
    lr = _git(["rev-list", "--left-right", "--count", "@{u}...HEAD"], check=False).strip()
    if lr:
        parts = lr.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            behind, ahead = int(parts[0]), int(parts[1])
    staged, unstaged = _parse_porcelain_z(_git(["status", "--porcelain=v1", "-z"]))
    return {"isRepo": True, "branch": branch, "ahead": ahead, "behind": behind,
            "staged": staged, "unstaged": unstaged}


@handler("scm.diff")
def _diff(params, ctx):
    """Merkez Monaco diff için orijinal/yeni içerik çifti.
    staged=True → HEAD ↔ indeks; False → indeks(/HEAD) ↔ çalışma ağacı."""
    path = (params.get("path") or "").replace("\\", "/")
    staged = bool(params.get("staged"))
    if not path or not _is_repo():
        raise BridgeError("bad_request", "Geçersiz istek.")

    def show(ref: str) -> str | None:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        cp = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=_root(), capture_output=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", env=env, timeout=_GIT_TIMEOUT,
        )
        return cp.stdout if cp.returncode == 0 else None

    if staged:
        original = show("HEAD") or ""       # HEAD'de yoksa yeni dosya
        modified = show("") or ""           # ":path" = indeks sürümü
    else:
        original = show("")                 # indeks; yoksa (untracked) boş
        if original is None:
            original = show("HEAD") or ""
        full = os.path.join(_root(), path)
        if os.path.isfile(full):
            with open(full, encoding="utf-8", errors="replace") as fh:
                modified = fh.read()
        else:
            modified = ""                   # silinmiş dosya
    return {"original": original, "modified": modified}


@handler("scm.stage")
def _stage(params, ctx):
    paths = params.get("paths") or []
    if paths:
        _git(["add", "--", *paths])
    return {}


@handler("scm.unstage")
def _unstage(params, ctx):
    paths = params.get("paths") or []
    if paths:
        _git(["reset", "-q", "HEAD", "--", *paths])
    return {}


@handler("scm.discard")
def _discard(params, ctx):
    """Çalışma ağacındaki değişikliği at. untracked → dosya silinir (onay web'de)."""
    path = (params.get("path") or "").replace("\\", "/")
    if not path:
        raise BridgeError("bad_request", "Yol gerekli.")
    if params.get("untracked"):
        proj = state.get_project()
        proj.delete(path)  # yol güvenliği Project._safe'te
    else:
        _git(["checkout", "--", path])
    return {}


@handler("scm.commit")
def _commit(params, ctx):
    message = (params.get("message") or "").strip()
    if not message:
        raise BridgeError("bad_request", "Commit mesajı boş olamaz.")
    out = _git(["commit", "-m", message])
    # özet satırı: "[branch abc1234] mesaj"
    summary = (out or "").strip().splitlines()[0] if out.strip() else "Commit oluşturuldu."
    return {"summary": summary[:200]}
