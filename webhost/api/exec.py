"""exec.* — F5 "çalıştır" köprüsü (P8.1).

Terminalden farkı: çıktı YAKALANIR (exit≠0 → P9.2 "Hatayı ekibe gönder" buradan
beslenecek) ve akış salt-okunur ÇIKTI sekmesine gider. Tek eşzamanlı koşu;
yenisi başlarsa eskisi durdurulur (VS Code yeniden-koş davranışı).

Kabuk üzerinden çalışır (npm/cargo gibi .cmd sarmalayıcıları için şart);
Windows'ta süreç AĞACI taskkill /T ile öldürülür (kabuğun çocukları kalmasın).
"""

import os
import subprocess
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

import runconfig
from webhost import state
from webhost.bridge import handler, BridgeError

FLUSH_MS = 16
FLUSH_MAX = 256 * 1024

_active: dict = {"exec": None, "next_id": 0}


class _Reader(QThread):
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


class _Exec(QObject):
    """Ana thread: tampon + 16ms flush (terminal deseniyle aynı) + yaşam döngüsü."""

    def __init__(self, exec_id: str, proc, bridge, parent=None):
        super().__init__(parent)
        self.id = exec_id
        self.proc = proc
        self._bridge = bridge
        self._t0 = time.monotonic()
        self._buf: list[str] = []
        self._buf_len = 0
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self._flush)
        self.reader = _Reader(proc)
        self.reader.chunk.connect(self._on_chunk)
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
        self._bridge.emit_event("exec.output", {"execId": self.id, "data": data})

    def _on_exit(self, code: int):
        self._flush()
        self._bridge.emit_event("exec.exited", {
            "execId": self.id, "code": code,
            "durationS": round(time.monotonic() - self._t0, 2),
        })
        if _active.get("exec") is self:
            _active["exec"] = None

    def stop(self):
        if self.proc.poll() is None:
            if os.name == "nt":
                # kabuk + çocukları (python/node süreci) birlikte ölsün
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                self.proc.terminate()
        self.reader.wait(1500)


def _require_project():
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


@handler("exec.run")
def _run(params, ctx):
    proj = _require_project()
    rel = params.get("rel")
    command = (params.get("command") or "").strip()
    if not command:
        command = runconfig.file_command(rel) if rel else runconfig.project_command(proj.root)
    if not command:
        raise BridgeError(
            "no_command",
            f"'{rel}' için koşu komutu bilinmiyor — paletten 'Çalıştırma Komutunu Değiştir' ile tanımla."
            if rel else
            "Proje için koşu komutu bulunamadı — paletten 'Çalıştırma Komutunu Değiştir' ile tanımla.")

    old = _active.get("exec")
    if old is not None:
        old.stop()  # yeniden-koş: eskisini düşür

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=proj.root, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError as e:
        raise BridgeError("spawn_failed", f"Komut başlatılamadı: {e}")

    _active["next_id"] += 1
    exec_id = f"x{_active['next_id']}"
    _active["exec"] = _Exec(exec_id, proc, ctx._bridge)
    return {"execId": exec_id, "command": command}


@handler("exec.stop")
def _stop(params, ctx):
    ex = _active.get("exec")
    if ex is not None:
        ex.stop()
    return {}


@handler("exec.getCommand")
def _get_cmd(params, ctx):
    proj = _require_project()
    return {"command": runconfig.project_command(proj.root)}


@handler("exec.setCommand")
def _set_cmd(params, ctx):
    proj = _require_project()
    cmd = (params.get("command") or "").strip()
    if not cmd:
        raise BridgeError("empty_command", "Komut boş.")
    runconfig.save_project_command(proj.root, cmd)
    return {}


def shutdown():
    ex = _active.get("exec")
    if ex is not None:
        ex.stop()
    _active["exec"] = None
