"""debug.* — Python debugger köprüsü (debugpy / DAP — P8.2).

Akış (bkz. docs/IDE-PLUS-PLAN.md P8.2):
  debug.start → serbest port bul → `python -m debugpy --listen 127.0.0.1:<port>
  --wait-for-client <dosya>` (debuggee bizim Popen'ımız; stdout ÇIKTI sekmesine
  akar) → sokete bağlan (retry'lı, ayrı thread) → DAP el sıkışması:
  initialize → attach → [initialized olayı] → setBreakpoints → configurationDone.

DAP teli LSP ile aynı Content-Length çerçevesi → `webhost/jsonrpc.py` ortak.

Durunca (stopped) çağrı yığınını Python kendisi çeker ve `debug.stopped`
olayına koyar — web ekstra round-trip yapmaz; scopes/variables tembeldir
(`debug.scopes` / `debug.variables`, variablesReference ile).
"""

import os
import socket
import subprocess
import sys
import time

from PySide6.QtCore import QThread, QTimer, Signal

from webhost import state
from webhost.bridge import handler, BridgeError
from webhost.jsonrpc import encode, FrameDecoder
from runtime_paths import is_frozen

FLUSH_MS = 16
FLUSH_MAX = 256 * 1024
CONNECT_TIMEOUT_S = 8.0

# tek aktif oturum
_dbg: dict = {
    "proc": None, "sock": None, "reader": None, "out_reader": None,
    "connector": None, "seq": 0, "pending": {}, "bridge": None,
    "thread_id": None, "exit_code": None, "root": None,
    "start_ctx": None, "bp": [], "out_buf": [], "out_len": 0, "out_timer": None,
    "t0": 0.0,
}


class _SockReader(QThread):
    """DAP soketini bloklu okur; çerçeveleri ana thread'e taşır."""

    message = Signal(dict)
    closed = Signal()

    def __init__(self, sock):
        super().__init__()
        self._sock = sock

    def run(self):
        dec = FrameDecoder()
        try:
            while True:
                data = self._sock.recv(65536)
                if not data:
                    break
                for msg in dec.feed(data):
                    self.message.emit(msg)
        except OSError:
            pass
        self.closed.emit()


class _OutReader(QThread):
    """Debuggee stdout'u (bizim Popen borumuz) — ÇIKTI sekmesine akar."""

    chunk = Signal(str)
    exited = Signal(int)

    def __init__(self, proc):
        super().__init__()
        self._proc = proc

    def run(self):
        try:
            while True:
                data = self._proc.stdout.read1(65536)
                if not data:
                    break
                self.chunk.emit(data.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        self.exited.emit(self._proc.wait())


class _Connector(QThread):
    """debugpy dinleyene kadar bağlanmayı dener (adapter açılışı ~saniye alır)."""

    connected = Signal(object)
    failed = Signal(str)

    def __init__(self, port: int):
        super().__init__()
        self._port = port

    def run(self):
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self._port), timeout=1.0)
                self.connected.emit(s)
                return
            except OSError:
                time.sleep(0.15)
        self.failed.emit("debugpy'a bağlanılamadı (zaman aşımı).")


# ---------------- yol çevirisi ----------------

def _to_abs(rel: str) -> str:
    return os.path.normpath(os.path.join(_dbg["root"], rel))


def _to_rel(path: str) -> str:
    """Mutlak kaynak yolunu köke göre çevir; kök dışıysa olduğu gibi bırak."""
    try:
        rel = os.path.relpath(path, _dbg["root"])
    except ValueError:  # farklı sürücü
        return path.replace("\\", "/")
    if rel.startswith(".."):
        return path.replace("\\", "/")
    return rel.replace("\\", "/")


# ---------------- gönderim (ana thread) ----------------

def _send(msg: dict) -> None:
    sock = _dbg["sock"]
    if sock is None:
        return
    try:
        sock.sendall(encode(msg))
    except OSError:
        pass


def _request(command: str, arguments: dict | None = None, on_reply=None) -> None:
    """DAP isteği; yanıt gelince on_reply(msg) (callable | CallContext | None)."""
    _dbg["seq"] += 1
    seq = _dbg["seq"]
    if on_reply is not None:
        _dbg["pending"][seq] = on_reply
    _send({"seq": seq, "type": "request", "command": command,
           "arguments": arguments or {}})


def _emit(channel: str, payload: dict) -> None:
    bridge = _dbg["bridge"]
    if bridge is not None:
        bridge.emit_event(channel, payload)


# ---------------- alım (ana thread) ----------------

def _on_message(msg: dict) -> None:
    t = msg.get("type")
    if t == "response":
        waiter = _dbg["pending"].pop(msg.get("request_seq"), None)
        if waiter is None:
            return
        if callable(waiter):
            waiter(msg)
        elif msg.get("success"):
            waiter.resolve({"body": msg.get("body") or {}})
        else:
            waiter.fail("dap_error", msg.get("message") or "DAP hatası")
        return
    if t == "event":
        _on_event(msg.get("event") or "", msg.get("body") or {})


def _on_event(event: str, body: dict) -> None:
    if event == "initialized":
        # el sıkışmasının 2. yarısı: breakpoint'ler + configurationDone
        for src in _dbg["bp"]:
            _request("setBreakpoints", {
                "source": {"path": _to_abs(src["path"])},
                "breakpoints": [{"line": n} for n in src["lines"]],
            })

        def _cfg_done(_msg: dict) -> None:
            ctx = _dbg.pop("start_ctx", None)
            if ctx is not None:
                ctx.resolve({"started": True})

        _request("configurationDone", {}, _cfg_done)
    elif event == "stopped":
        _dbg["thread_id"] = body.get("threadId")
        reason = body.get("reason") or ""

        def _with_stack(msg: dict) -> None:
            frames = []
            for f in (msg.get("body") or {}).get("stackFrames") or []:
                src = (f.get("source") or {}).get("path") or ""
                frames.append({
                    "id": f.get("id"), "name": f.get("name") or "?",
                    "path": _to_rel(src) if src else "",
                    "line": f.get("line") or 0,
                })
            top = frames[0] if frames else {"path": "", "line": 0}
            _emit("debug.stopped", {
                "reason": reason, "threadId": _dbg["thread_id"],
                "path": top["path"], "line": top["line"], "frames": frames,
            })

        _request("stackTrace",
                 {"threadId": _dbg["thread_id"], "startFrame": 0, "levels": 32},
                 _with_stack)
    elif event == "continued":
        _emit("debug.continued", {})
    elif event == "exited":
        _dbg["exit_code"] = body.get("exitCode")


def _on_sock_closed() -> None:
    # bekleyen istekler çözülemez — temizle
    for waiter in list(_dbg["pending"].values()):
        if not callable(waiter):
            waiter.fail("debug_gone", "Debug oturumu kapandı.")
    _dbg["pending"].clear()


# ---------------- debuggee stdout → ÇIKTI sekmesi ----------------

def _on_out_chunk(data: str) -> None:
    _dbg["out_buf"].append(data)
    _dbg["out_len"] += len(data)
    timer = _dbg["out_timer"]
    if _dbg["out_len"] >= FLUSH_MAX:
        _flush_out()
    elif timer is not None and not timer.isActive():
        timer.start()


def _flush_out() -> None:
    timer = _dbg["out_timer"]
    if timer is not None:
        timer.stop()
    if not _dbg["out_buf"]:
        return
    data = "".join(_dbg["out_buf"])
    _dbg["out_buf"].clear()
    _dbg["out_len"] = 0
    _emit("debug.output", {"data": data})


def _on_proc_exited(code: int) -> None:
    _flush_out()
    final = _dbg["exit_code"] if _dbg["exit_code"] is not None else code
    duration = round(time.monotonic() - _dbg["t0"], 2)
    _emit("debug.terminated", {"code": final, "durationS": duration})
    _cleanup()


# ---------------- yaşam döngüsü ----------------

def _active() -> bool:
    proc = _dbg["proc"]
    return proc is not None and proc.poll() is None


def _cleanup() -> None:
    sock, reader, out_reader = _dbg["sock"], _dbg["reader"], _dbg["out_reader"]
    _dbg.update(proc=None, sock=None, reader=None, out_reader=None,
                connector=None, thread_id=None, start_ctx=None)
    _dbg["pending"].clear()
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    if reader is not None:
        reader.wait(1500)
    if out_reader is not None:
        out_reader.wait(1500)


def _kill_proc() -> None:
    proc = _dbg["proc"]
    if proc is not None and proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.terminate()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------- köprü yüzeyi ----------------

@handler("debug.start")
def _start(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    rel = (params.get("rel") or "").strip()
    if not rel.endswith(".py"):
        raise BridgeError("not_python", "Debugger şimdilik yalnız Python dosyaları için.")
    if _active():
        _kill_proc()  # yeniden-koş: eski oturumu düşür (exec deseni)
        _cleanup()

    port = _free_port()
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    script = os.path.normpath(os.path.join(proj.root, rel))
    try:
        command = ([sys.executable, "--imece-debugpy"] if is_frozen()
                   else [sys.executable, "-m", "debugpy"])
        proc = subprocess.Popen(
            command + ["--listen", f"127.0.0.1:{port}", "--wait-for-client", script],
            cwd=proj.root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError as e:
        raise BridgeError("spawn_failed", f"debugpy başlatılamadı: {e}")

    _dbg.update(proc=proc, root=proj.root, bridge=ctx._bridge, start_ctx=ctx,
                thread_id=None, exit_code=None, t0=time.monotonic(),
                bp=list(params.get("breakpoints") or []))
    if _dbg["out_timer"] is None:
        timer = QTimer()
        timer.setInterval(FLUSH_MS)
        timer.timeout.connect(_flush_out)
        _dbg["out_timer"] = timer

    out_reader = _OutReader(proc)
    out_reader.chunk.connect(_on_out_chunk)
    out_reader.exited.connect(_on_proc_exited)
    out_reader.start()
    _dbg["out_reader"] = out_reader

    def _on_connected(sock) -> None:
        _dbg["sock"] = sock
        reader = _SockReader(sock)
        reader.message.connect(_on_message)
        reader.closed.connect(_on_sock_closed)
        reader.start()
        _dbg["reader"] = reader

        def _after_init(_msg: dict) -> None:
            _request("attach", {"justMyCode": True})

        _request("initialize", {
            "clientID": "imece", "adapterID": "debugpy",
            "pathFormat": "path", "linesStartAt1": True, "columnsStartAt1": True,
            "supportsVariableType": True,
        }, _after_init)

    def _on_failed(message: str) -> None:
        failed_ctx = _dbg.pop("start_ctx", None)
        _kill_proc()
        if failed_ctx is not None:
            failed_ctx.fail("connect_failed", message)

    conn = _Connector(port)
    conn.connected.connect(_on_connected)
    conn.failed.connect(_on_failed)
    conn.start()
    _dbg["connector"] = conn
    return None  # async — configurationDone yanıtı çözer


@handler("debug.setBreakpoints")
def _set_bp(params, ctx):
    rel = params.get("path") or ""
    lines = [int(n) for n in (params.get("lines") or [])]
    if not _active() or _dbg["sock"] is None:
        return {"lines": lines}  # oturum yok — web yereldekiyle devam eder

    def _reply(msg: dict) -> None:
        got = [(b.get("line") or 0)
               for b in (msg.get("body") or {}).get("breakpoints") or []
               if b.get("verified")]
        ctx.resolve({"lines": got})

    _request("setBreakpoints", {
        "source": {"path": _to_abs(rel)},
        "breakpoints": [{"line": n} for n in lines],
    }, _reply)
    return None


def _step(command: str):
    if not _active() or _dbg["thread_id"] is None:
        raise BridgeError("not_stopped", "Debugger durmuş durumda değil.")
    _request(command, {"threadId": _dbg["thread_id"]})
    _emit("debug.continued", {})
    return {}


@handler("debug.continue")
def _continue(params, ctx):
    return _step("continue")


@handler("debug.next")
def _next(params, ctx):
    return _step("next")


@handler("debug.stepIn")
def _step_in(params, ctx):
    return _step("stepIn")


@handler("debug.stepOut")
def _step_out(params, ctx):
    return _step("stepOut")


@handler("debug.stack")
def _stack(params, ctx):
    if not _active() or _dbg["thread_id"] is None:
        raise BridgeError("not_stopped", "Debugger durmuş durumda değil.")

    def _reply(msg: dict) -> None:
        frames = []
        for f in (msg.get("body") or {}).get("stackFrames") or []:
            src = (f.get("source") or {}).get("path") or ""
            frames.append({"id": f.get("id"), "name": f.get("name") or "?",
                           "path": _to_rel(src) if src else "",
                           "line": f.get("line") or 0})
        ctx.resolve({"frames": frames})

    _request("stackTrace",
             {"threadId": _dbg["thread_id"], "startFrame": 0, "levels": 32}, _reply)
    return None


@handler("debug.scopes")
def _scopes(params, ctx):
    if not _active():
        raise BridgeError("not_stopped", "Debugger durmuş durumda değil.")

    def _reply(msg: dict) -> None:
        scopes = [{"name": s.get("name") or "?",
                   "ref": s.get("variablesReference") or 0,
                   "expensive": bool(s.get("expensive"))}
                  for s in (msg.get("body") or {}).get("scopes") or []]
        ctx.resolve({"scopes": scopes})

    _request("scopes", {"frameId": params.get("frameId")}, _reply)
    return None


@handler("debug.variables")
def _variables(params, ctx):
    if not _active():
        raise BridgeError("not_stopped", "Debugger durmuş durumda değil.")

    def _reply(msg: dict) -> None:
        out = [{"name": v.get("name") or "?", "value": v.get("value") or "",
                "type": v.get("type") or "", "ref": v.get("variablesReference") or 0}
               for v in (msg.get("body") or {}).get("variables") or []]
        ctx.resolve({"variables": out})

    _request("variables", {"variablesReference": params.get("ref")}, _reply)
    return None


@handler("debug.evaluate")
def _evaluate(params, ctx):
    if not _active():
        raise BridgeError("not_stopped", "Debugger durmuş durumda değil.")

    def _reply(msg: dict) -> None:
        if msg.get("success"):
            body = msg.get("body") or {}
            ctx.resolve({"result": body.get("result") or "",
                         "ref": body.get("variablesReference") or 0})
        else:
            ctx.fail("eval_error", msg.get("message") or "Değerlendirilemedi.")

    _request("evaluate", {"expression": params.get("expr") or "",
                          "frameId": params.get("frameId"),
                          "context": "repl"}, _reply)
    return None


@handler("debug.stop")
def _stop(params, ctx):
    if _active():
        _request("disconnect", {"terminateDebuggee": True})
        # adapter nazikçe kapanmazsa süreci zorla düşür (temizlik _on_proc_exited'te)
        QTimer.singleShot(900, _kill_proc)
    return {}


@handler("debug.status")
def _status(params, ctx):
    return {"active": _active(), "stopped": _dbg["thread_id"] is not None}


def shutdown():
    """Uygulama kapanırken debug oturumunu düşür (zombi önleme)."""
    _kill_proc()
    _cleanup()
