"""terminal.* — gerçek etkileşimli terminal (pywinpty / ConPTY).

Eski terminal.py'nin komut-başına QProcess yaklaşımının yerine tam PTY:
ok tuşları, renkler, REPL'ler, interaktif programlar çalışır. Okuyucu QThread
ham chunk'ları sinyalle ana thread'e taşır; 16ms/256KB birleştirmeli flush
(plan risk 1: QWebChannel debisi) → `terminal.data` olayı.
"""

import os

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from webhost import state
from webhost.bridge import handler, BridgeError

FLUSH_MS = 16
FLUSH_MAX = 256 * 1024  # byte üst sınırı — tek olayda taşınacak azami veri

_terms: dict[str, "_Term"] = {}
_next_id = 0


def _clamp_i32(code) -> int:
    """PTY çıkış kodunu Qt Signal(int) (C++ 32-bit) için güvenli aralığa indir."""
    if code is None:
        return 0
    try:
        c = int(code)
    except (TypeError, ValueError, OverflowError):
        return -1
    if c > 2_147_483_647 or c < -2_147_483_648:
        return c & 0xFFFF  # düşük 16 bit yeterli sinyal (ör. 0x013A → 314)
    return c


class _Reader(QThread):
    chunk = Signal(str)
    exited = Signal(int)

    def __init__(self, pty):
        super().__init__()
        self._pty = pty

    def run(self):
        try:
            while True:
                data = self._pty.read(4096)  # bloklar; süreç ölünce EOFError
                if not data:
                    if not self._pty.isalive():
                        break
                    continue
                self.chunk.emit(data)
        except (EOFError, OSError, RuntimeError):
            pass
        # ConPTY başarısız olursa exitstatus 32-bit'e sığmayan bir Windows kodu
        # (ör. 0xC000013A) olabilir → Signal(int) C++ int taşması → traceback'siz
        # OverflowError (Beta-3 blokeri). Güvenli aralığa kıstır.
        try:
            code = self._pty.exitstatus
        except Exception:
            code = None
        self.exited.emit(_clamp_i32(code))


class _Term(QObject):
    """Ana thread'de yaşar: tampon + zamanlayıcı + PTY yazma ucu."""

    def __init__(self, term_id: str, pty, bridge, parent=None):
        super().__init__(parent)
        self.id = term_id
        self.pty = pty
        self._bridge = bridge
        self._buf: list[str] = []
        self._buf_len = 0
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self._flush)
        self.reader = _Reader(pty)
        self.reader.chunk.connect(self._on_chunk)      # queued → ana thread
        self.reader.exited.connect(self._on_exit)
        self.reader.start()

    def _on_chunk(self, data: str):
        self._buf.append(data)
        self._buf_len += len(data)
        if self._buf_len >= FLUSH_MAX:
            self._flush()
        elif not self._timer.isActive():
            self._timer.start()

    def _flush(self):
        self._timer.stop()
        if not self._buf:
            return
        data = "".join(self._buf)
        self._buf.clear()
        self._buf_len = 0
        self._bridge.emit_event("terminal.data", {"termId": self.id, "data": data})

    def _on_exit(self, code: int):
        self._flush()
        self._bridge.emit_event("terminal.exit", {"termId": self.id, "code": code})
        _terms.pop(self.id, None)

    def dispose(self):
        try:
            if self.pty.isalive():
                self.pty.terminate(force=True)
        except Exception:
            pass
        self.reader.wait(1500)


@handler("terminal.create")
def _create(params, ctx):
    global _next_id
    try:
        from winpty import PtyProcess
    except ImportError:
        raise BridgeError("no_pty", "pywinpty kurulu değil (pip install pywinpty).")

    proj = state.get_project()
    cwd = params.get("cwd") or (proj.root if proj else os.path.expanduser("~"))
    cols = int(params.get("cols") or 120)
    rows = int(params.get("rows") or 30)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # cp1254 tuzağı (bkz. SETUP)
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        pty = PtyProcess.spawn(
            ["powershell.exe", "-NoLogo"],
            dimensions=(rows, cols),
            cwd=cwd,
            env=env,
        )
    except Exception as e:
        raise BridgeError("spawn_failed", f"Terminal başlatılamadı: {e}")

    _next_id += 1
    term_id = f"t{_next_id}"
    _terms[term_id] = _Term(term_id, pty, ctx._bridge)
    return {"termId": term_id}


def _get(term_id: str) -> "_Term":
    t = _terms.get(term_id)
    if t is None:
        raise BridgeError("not_found", "Terminal yok (kapanmış olabilir).")
    return t


@handler("terminal.write")
def _write(params, ctx):
    _get(params.get("termId", "")).pty.write(params.get("data", ""))
    return {}


@handler("terminal.resize")
def _resize(params, ctx):
    t = _get(params.get("termId", ""))
    rows = int(params.get("rows") or 30)
    cols = int(params.get("cols") or 120)
    try:
        t.pty.setwinsize(rows, cols)
    except Exception:
        pass
    return {}


@handler("terminal.kill")
def _kill(params, ctx):
    t = _terms.pop(params.get("termId", ""), None)
    if t:
        t.dispose()
    return {}


def shutdown():
    """Kapanışta tüm PTY'leri öldür (zombi conhost önleme)."""
    for t in list(_terms.values()):
        t.dispose()
    _terms.clear()
