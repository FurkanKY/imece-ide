"""lsp.* — Python dil sunucusu köprüsü (basedpyright, stdio üzerinden LSP).

Neden basedpyright: pyright'ın npm dağıtımı çalışma zamanında Node ister;
basedpyright pip paketi Node'u tekerlek içinde gömülü getirir → son kullanıcıda
tek bağımlılık `pip install`. `basedpyright-langserver --stdio` ile açılır.

Akış (bkz. docs/IDE-PLUS-PLAN.md P7.2):
  lsp.start   → süreci başlat + `initialize` el sıkışması (proje köküne bağlı;
                proje değişirse yeniden başlar)
  lsp.request → istek/yanıt (completion/hover/definition…) — LS yanıtı gelince
                CallContext.resolve (async handler deseni)
  lsp.notify  → tek yön (didOpen/didChange/didClose/didSave)
  lsp.status  → durum çubuğu göstergesi için
  olay lsp.event {method, params} → sunucudan itilenler (publishDiagnostics,
                $/magentReady, $/magentExited)

Sunucudan GELEN isteklere (workspace/configuration vb.) burada asgari yanıt
verilir — yanıtsız bırakmak LS'i kilitleyebilir.
"""

import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from webhost import state
from webhost.bridge import handler, BridgeError
from webhost.jsonrpc import encode, FrameDecoder

SERVER_EXE = "basedpyright-langserver"

# tek aktif sunucu: süreç + okuyucu + bekleyen istekler (id → CallContext | callable)
_ls: dict = {"proc": None, "reader": None, "root": None, "ready": False,
             "next_id": 0, "pending": {}, "bridge": None}


class _Reader(QThread):
    """stdout'u bloklu okur, çerçeveleri çözer, mesajları ana thread'e taşır."""

    message = Signal(dict)
    exited = Signal()

    def __init__(self, proc):
        super().__init__()
        self._proc = proc

    def run(self):
        dec = FrameDecoder()
        try:
            while True:
                data = self._proc.stdout.read1(65536)
                if not data:
                    break
                for msg in dec.feed(data):
                    self.message.emit(msg)  # queued → ana thread
        except (OSError, ValueError):
            pass
        self.exited.emit()


# ---------------- gönderim (ana thread) ----------------

def _send(msg: dict) -> None:
    proc = _ls["proc"]
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.write(encode(msg))
        proc.stdin.flush()
    except OSError:
        pass


def _request(method: str, params: dict, on_reply) -> None:
    """LS'e istek at; yanıt gelince on_reply(msg) (CallContext veya callable)."""
    _ls["next_id"] += 1
    rid = _ls["next_id"]
    _ls["pending"][rid] = on_reply
    _send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def _notify(method: str, params: dict) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------- alım (ana thread; Reader sinyalinden) ----------------

def _on_message(msg: dict) -> None:
    if "id" in msg and "method" in msg:
        # sunucu → istemci İSTEĞİ: asgari yanıt (kilitlenme önleme)
        result = None
        if msg["method"] == "workspace/configuration":
            items = (msg.get("params") or {}).get("items") or []
            result = [{} for _ in items]
        _send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        return
    if "id" in msg:
        # bizim isteğimizin yanıtı
        waiter = _ls["pending"].pop(msg["id"], None)
        if waiter is None:
            return
        if callable(waiter):
            waiter(msg)
        elif "error" in msg:
            waiter.fail("lsp_error", str(msg["error"].get("message", "LSP hatası")))
        else:
            waiter.resolve({"result": msg.get("result")})
        return
    # sunucu bildirimi → web'e forward
    bridge = _ls["bridge"]
    if bridge is not None:
        bridge.emit_event("lsp.event",
                          {"method": msg.get("method", ""), "params": msg.get("params")})


def _on_exited() -> None:
    _ls["ready"] = False
    for waiter in list(_ls["pending"].values()):
        if not callable(waiter):
            waiter.fail("lsp_gone", "Dil sunucusu kapandı.")
    _ls["pending"].clear()
    bridge = _ls["bridge"]
    if bridge is not None:
        bridge.emit_event("lsp.event", {"method": "$/magentExited", "params": None})


# ---------------- yaşam döngüsü ----------------

def _stop_current() -> None:
    proc, reader = _ls["proc"], _ls["reader"]
    _ls.update(proc=None, reader=None, ready=False, root=None)
    for waiter in list(_ls["pending"].values()):
        if not callable(waiter):
            waiter.fail("lsp_gone", "Dil sunucusu yeniden başlıyor.")
    _ls["pending"].clear()
    if proc is not None and proc.poll() is None:
        try:
            proc.stdin.write(encode({"jsonrpc": "2.0", "id": 999999,
                                     "method": "shutdown", "params": None}))
            proc.stdin.write(encode({"jsonrpc": "2.0", "method": "exit", "params": None}))
            proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if reader is not None:
        reader.wait(1500)


def _spawn(root: str, bridge) -> None:
    exe = shutil.which(SERVER_EXE)
    if exe is None:
        raise BridgeError("no_server",
                          "basedpyright kurulu değil (pip install basedpyright).")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW — konsol flaşı yok
    proc = subprocess.Popen(
        [exe, "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=root, creationflags=flags,
    )
    reader = _Reader(proc)
    reader.message.connect(_on_message)
    reader.exited.connect(_on_exited)
    reader.start()
    _ls.update(proc=proc, reader=reader, root=root, ready=False, bridge=bridge)

    uri = Path(root).as_uri()

    def _on_initialized(_msg: dict) -> None:
        _notify("initialized", {})
        _ls["ready"] = True
        bridge.emit_event("lsp.event", {"method": "$/magentReady", "params": None})

    _request("initialize", {
        "processId": os.getpid(),
        "rootUri": uri,
        "workspaceFolders": [{"uri": uri, "name": Path(root).name}],
        "capabilities": {
            "textDocument": {
                "synchronization": {"didSave": True},
                "publishDiagnostics": {"relatedInformation": True},
                "completion": {"completionItem": {
                    "snippetSupport": False,
                    "documentationFormat": ["markdown", "plaintext"],
                }},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "signatureHelp": {"signatureInformation": {
                    "documentationFormat": ["markdown", "plaintext"],
                }},
                "definition": {},
            },
            "workspace": {"configuration": True, "workspaceFolders": True},
        },
    }, _on_initialized)


# ---------------- köprü yüzeyi ----------------

@handler("lsp.start")
def _start(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    if (_ls["proc"] is not None and _ls["proc"].poll() is None
            and _ls["root"] == proj.root):
        return {"running": True, "ready": _ls["ready"]}
    _stop_current()
    _spawn(proj.root, ctx._bridge)
    return {"running": True, "ready": False}


@handler("lsp.stop")
def _stop(params, ctx):
    _stop_current()
    return {}


@handler("lsp.status")
def _status(params, ctx):
    running = _ls["proc"] is not None and _ls["proc"].poll() is None
    return {"running": running, "ready": bool(_ls["ready"]), "server": "basedpyright"}


@handler("lsp.request")
def _req(params, ctx):
    if not _ls["ready"]:
        return {"result": None}  # LS hazırlanıyor — sessizce boş (plan kararı)
    method = params.get("method") or ""
    if not method:
        raise BridgeError("bad_request", "method boş.")
    _request(method, params.get("params") or {}, ctx)
    return None  # async — LS yanıtı CallContext'i çözer


@handler("lsp.notify")
def _not(params, ctx):
    method = params.get("method") or ""
    if method and _ls["proc"] is not None:
        _notify(method, params.get("params") or {})
    return {}


def shutdown():
    """Uygulama kapanırken dil sunucusunu düzgün kapat."""
    _stop_current()
