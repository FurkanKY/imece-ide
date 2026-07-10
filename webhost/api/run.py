"""run.* — multi-agent koşu köprüsü.

desktop.py Worker(QThread) deseninin portu: `project_runner.run_project_task`
generator'ının olayları DEĞİŞMEDEN `run.event` kanalına forward edilir
(stage/info/output/metric/diff/verdict/proposal). Proposal içerikleri sunucu
tarafında tutulur → applyProposals yalnız yol listesi alır (içerik köprüden
geri taşınmaz). Proposal gelince geçmişe kaydedilir (desktop.py davranışı).
"""

from PySide6.QtCore import QThread, Signal

from history import HistoryStore
from checkpoints import CheckpointStore
from project import Project
from project_runner import run_project_task
from agents import DEFAULT_ROUTING
from adapters import PROVIDERS
from webhost import state
from webhost.bridge import handler, BridgeError

_active: dict = {"worker": None, "run_id": 0, "proposals": [], "task": ""}


class _Worker(QThread):
    event = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, root, task, routing):
        super().__init__()
        self.root, self.task, self.routing = root, task, routing
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        gen = run_project_task(self.root, self.task, self.routing)
        try:
            for ev in gen:
                if self._cancel:
                    gen.close()
                    self.cancelled.emit()
                    return
                self.event.emit(ev)
        except Exception as e:  # motor hatası UI'a düzgün gitsin
            self.failed.emit(str(e))


def _require_project() -> Project:
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


@handler("run.providers")
def _providers(params, ctx):
    return {"providers": list(PROVIDERS.keys()), "defaultRouting": dict(DEFAULT_ROUTING)}


@handler("run.start")
def _start(params, ctx):
    proj = _require_project()
    task = (params.get("task") or "").strip()
    if not task:
        raise BridgeError("empty_task", "Görev boş.")
    if _active["worker"] is not None and _active["worker"].isRunning():
        raise BridgeError("busy", "Zaten bir koşu sürüyor.")

    routing = params.get("routing") or {}
    _active["run_id"] += 1
    run_id = str(_active["run_id"])
    _active["proposals"] = []
    _active["task"] = task

    bridge = ctx._bridge  # ana thread'e sinyalle taşınır (queued connection)
    worker = _Worker(proj.root, task, routing)

    def on_event(ev: dict):
        if ev.get("type") == "proposal":
            _active["proposals"] = ev.get("proposals", [])
            # geçmişe kaydet (desktop.py:_on_proposal davranışı)
            tt = ev.get("totals", {})
            HistoryStore(proj.root).add(
                task, ev.get("verdict", "UNKNOWN"),
                tt.get("tokens", 0), tt.get("cost_usd", 0.0),
                [p.get("path", "") for p in _active["proposals"]],
            )
        bridge.emit_event("run.event", {"runId": run_id, "ev": ev})

    ended = {"flag": False}  # failed/cancelled sonrası ikinci "done" yayınlanmasın

    def finish(status: str, error: str | None = None):
        if ended["flag"]:
            return
        ended["flag"] = True
        payload = {"runId": run_id, "status": status}
        if error:
            payload["error"] = error
        bridge.emit_event("run.finished", payload)

    worker.event.connect(on_event)
    worker.failed.connect(lambda msg: finish("failed", msg))
    worker.cancelled.connect(lambda: finish("cancelled"))
    worker.finished.connect(lambda: finish("done"))

    _active["worker"] = worker
    worker.start()
    return {"runId": run_id}


@handler("run.cancel")
def _cancel(params, ctx):
    w = _active.get("worker")
    if w is not None and w.isRunning():
        w.cancel()
    return {}


@handler("run.applyProposals")
def _apply(params, ctx):
    proj = _require_project()
    wanted = set(params.get("paths") or [])
    proposals = [p for p in _active.get("proposals", []) if p.get("path") in wanted]
    if not proposals:
        return {"applied": [], "errors": [], "checkpointId": None}
    try:
        checkpoint = CheckpointStore(proj.root).create(
            proj, [p["path"] for p in proposals], str(_active.get("run_id")),
        )
    except Exception as e:
        raise BridgeError("checkpoint", f"Checkpoint oluşturulamadı: {e}")

    applied, errors = [], []
    for p in proposals:
        try:
            proj.apply(p["path"], p.get("new", ""), backup=False)
            applied.append(p["path"])
        except Exception as e:
            errors.append({"path": p.get("path", ""), "message": str(e)})
    # Kısmi apply'da checkpoint geri yüklenir; kullanıcı hiçbir yarım değişiklik görmez.
    if errors:
        store = CheckpointStore(proj.root)
        store.restore(proj, checkpoint["id"])
        store.drop(checkpoint["id"])
        applied = []
    if applied:
        _active["proposals"] = [
            p for p in _active["proposals"] if p.get("path") not in set(applied)
        ]
    if applied:
        ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": applied})
    return {
        "applied": applied,
        "errors": errors,
        "checkpointId": checkpoint["id"] if applied else None,
    }


@handler("run.rejectProposals")
def _reject(params, ctx):
    _active["proposals"] = []
    return {}


def shutdown():
    """Uygulama kapanırken koşuyu iptal et (zombi thread önleme)."""
    w = _active.get("worker")
    if w is not None and w.isRunning():
        w.cancel()
        w.wait(2000)
