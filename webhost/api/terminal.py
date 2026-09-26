"""terminal.* — gerçek etkileşimli terminal (Windows'ta pywinpty/ConPTY,
POSIX'te ptyprocess/gerçek PTY).

Eski terminal.py'nin komut-başına QProcess yaklaşımının yerine tam PTY:
ok tuşları, renkler, REPL'ler, interaktif programlar çalışır. Okuyucu QThread
ham chunk'ları sinyalle ana thread'e taşır; 16ms/256KB birleştirmeli flush
(plan risk 1: QWebChannel debisi) → `terminal.data` olayı.

İki backend de aynı minik yüzeyi (read/write/isalive/exitstatus/terminate/
setwinsize) sağlar; POSIX tarafı `_PosixPty` ile bu yüzeye sarmalanır, böylece
`_Reader`/`_Term` platform bilmez.
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


class _PosixPty:
    """ptyprocess.PtyProcessUnicode sarmalayıcısı — winpty.PtyProcess ile aynı yüzey.

    ptyprocess normal çıkışta ``exitstatus``, sinyalle öldüğünde ``signalstatus``
    doldurur (ikisi ayrı alan, biri hep None); pywinpty tarafı ile tek alanda
    tutarlı olsun diye subprocess kuralına uyup sinyal durumunu -sinyal_no
    olarak exitstatus'a taşıyoruz.
    """

    def __init__(self, proc):
        self._proc = proc

    def read(self, size: int = 4096) -> str:
        return self._proc.read(size)

    def write(self, data: str) -> int:
        return self._proc.write(data)

    def isalive(self) -> bool:
        return self._proc.isalive()

    def setwinsize(self, rows: int, cols: int) -> None:
        self._proc.setwinsize(rows, cols)

    def terminate(self, force: bool = False) -> None:
        self._proc.terminate(force=force)

    @property
    def exitstatus(self):
        if self._proc.exitstatus is not None:
            return self._proc.exitstatus
        if self._proc.signalstatus is not None:
            return -self._proc.signalstatus
        return None


def _posix_shell() -> str:
    """$SHELL varsa ve çalıştırılabilirse onu kullan; yoksa bash, o da yoksa sh."""
    shell = os.environ.get("SHELL")
    if shell and os.path.isfile(shell) and os.access(shell, os.X_OK):
        return shell
    if os.path.isfile("/bin/bash") and os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    return "/bin/sh"


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

    proj = state.get_project()
    cwd = params.get("cwd") or (proj.root if proj else os.path.expanduser("~"))
    cols = int(params.get("cols") or 120)
    rows = int(params.get("rows") or 30)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # cp1254 tuzağı (bkz. SETUP)
    env["PYTHONIOENCODING"] = "utf-8"

    if os.name == "nt":
        try:
            from winpty import PtyProcess
        except ImportError:
            raise BridgeError("no_pty", "pywinpty kurulu değil (pip install pywinpty).")
        try:
            pty = PtyProcess.spawn(
                ["powershell.exe", "-NoLogo"],
                dimensions=(rows, cols),
                cwd=cwd,
                env=env,
            )
        except Exception as e:
            raise BridgeError("spawn_failed", f"Terminal başlatılamadı: {e}")
        shell_name = "powershell"
    else:
        try:
            from ptyprocess import PtyProcessUnicode
        except ImportError:
            raise BridgeError("no_pty", "ptyprocess kurulu değil (pip install ptyprocess).")
        shell_path = _posix_shell()
        env["TERM"] = "xterm-256color"
        try:
            pty = _PosixPty(PtyProcessUnicode.spawn(
                [shell_path],
                dimensions=(rows, cols),
                cwd=cwd,
                env=env,
            ))
        except Exception as e:
            raise BridgeError("spawn_failed", f"Terminal başlatılamadı: {e}")
        shell_name = os.path.basename(shell_path)

    _next_id += 1
    term_id = f"t{_next_id}"
    _terms[term_id] = _Term(term_id, pty, ctx._bridge)
    return {"termId": term_id, "shell": shell_name}


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
