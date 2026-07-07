"""search.* — projede metin arama (eski yol haritası T1.3'ün web-shell karşılığı).

ripgrep PATH'te varsa onunla (--vimgrep, hızlı), yoksa saf Python taraması
(Project.list_files + satır tarama). QThread worker sonuçları batch'ler halinde
`search.results` olayıyla akıtır; `search.done` ile biter; iptal edilebilir.
"""

import os
import re
import shutil
import subprocess

from PySide6.QtCore import QThread, Signal

from project import Project
from webhost import state
from webhost.bridge import handler, BridgeError

MAX_RESULTS = 2000
BATCH = 80

_active: dict = {"worker": None, "search_id": 0}


class _SearchWorker(QThread):
    results = Signal(list)   # [{path, line, col, preview}]
    done = Signal(int, bool)  # (toplam, limite takıldı mı)

    def __init__(self, root: str, query: str, regex: bool, case_sensitive: bool):
        super().__init__()
        self.root = root
        self.query = query
        self.regex = regex
        self.case_sensitive = case_sensitive
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # ---------------- ripgrep yolu ----------------
    def _run_rg(self) -> tuple[int, bool] | None:
        rg = shutil.which("rg")
        if rg is None:
            return None
        args = [rg, "--vimgrep", "--no-heading", "--max-count", "50",
                "--max-columns", "300"]
        if not self.case_sensitive:
            args.append("--ignore-case")
        if not self.regex:
            args.append("--fixed-strings")
        args += ["--", self.query, "."]
        try:
            proc = subprocess.Popen(
                args, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return None
        total, limit_hit, batch = 0, False, []
        assert proc.stdout is not None
        for raw in proc.stdout:
            if self._cancel:
                proc.kill()
                break
            # biçim: yol:satır:sütun:önizleme
            parts = raw.rstrip("\n").split(":", 3)
            if len(parts) < 4:
                continue
            path, line, col, preview = parts
            batch.append({
                "path": path.replace("\\", "/").lstrip("./"),
                "line": int(line), "col": int(col),
                "preview": preview.strip()[:240],
            })
            total += 1
            if len(batch) >= BATCH:
                self.results.emit(batch)
                batch = []
            if total >= MAX_RESULTS:
                limit_hit = True
                proc.kill()
                break
        if batch:
            self.results.emit(batch)
        proc.wait(timeout=5)
        return total, limit_hit

    # ---------------- saf Python yolu ----------------
    def _run_python(self) -> tuple[int, bool]:
        proj = Project(self.root)
        flags = 0 if self.case_sensitive else re.IGNORECASE
        if self.regex:
            try:
                pat = re.compile(self.query, flags)
            except re.error as e:
                raise BridgeError("bad_regex", f"Geçersiz düzenli ifade: {e}")
        else:
            pat = re.compile(re.escape(self.query), flags)
        total, limit_hit, batch = 0, False, []
        for rel in proj.list_files(max_files=100000):
            if self._cancel or limit_hit:
                break
            try:
                with open(proj._safe(rel), encoding="utf-8", errors="replace") as fh:
                    for lineno, text in enumerate(fh, 1):
                        m = pat.search(text)
                        if not m:
                            continue
                        batch.append({
                            "path": rel, "line": lineno, "col": m.start() + 1,
                            "preview": text.strip()[:240],
                        })
                        total += 1
                        if len(batch) >= BATCH:
                            self.results.emit(batch)
                            batch = []
                        if total >= MAX_RESULTS:
                            limit_hit = True
                            break
            except OSError:
                continue
        if batch:
            self.results.emit(batch)
        return total, limit_hit

    def run(self):
        try:
            out = self._run_rg()
            if out is None:
                out = self._run_python()
            total, limit_hit = out
        except BridgeError:
            total, limit_hit = 0, False
        except Exception:
            total, limit_hit = 0, False
        self.done.emit(total, limit_hit)


@handler("search.start")
def _start(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    query = params.get("query") or ""
    if len(query) < 2:
        raise BridgeError("short_query", "En az 2 karakter gir.")

    # önceki aramayı iptal et
    prev = _active.get("worker")
    if prev is not None and prev.isRunning():
        prev.cancel()

    _active["search_id"] += 1
    search_id = str(_active["search_id"])
    bridge = ctx._bridge

    worker = _SearchWorker(
        proj.root, query,
        regex=bool(params.get("regex")),
        case_sensitive=bool(params.get("caseSensitive")),
    )
    worker.results.connect(
        lambda matches: bridge.emit_event("search.results",
                                          {"searchId": search_id, "matches": matches}))
    worker.done.connect(
        lambda total, limit: bridge.emit_event("search.done",
                                               {"searchId": search_id, "total": total,
                                                "limitHit": limit}))
    _active["worker"] = worker
    worker.start()
    return {"searchId": search_id}


@handler("search.cancel")
def _cancel(params, ctx):
    w = _active.get("worker")
    if w is not None and w.isRunning():
        w.cancel()
    return {}
