"""
terminal.py
-----------
T1.2 — Entegre terminal çekirdeği. QProcess tabanlı, komut-başına kabuk: proje
kökünde çalışır, canlı çıktı (utf-8 / errors=replace), komut geçmişi, çıkış kodu.

Tasarım kararı: kalıcı interaktif PTY yerine **komut-başına QProcess**. Windows'ta
PTY/echo/prompt ayrıştırma tuzaklarından kaçınır; sağlam, headless test edilebilir ve
`commandFinished(cmd, output, exit_code)` sinyaliyle doğrudan **hata→ajan döngüsü**ne
temel olur. `cd` içsel `self.cwd` ile takip edilir (kök dışına çıkabilir — gerçek kabuk gibi).

Kullanım (bottom_panel.py):
    t = Terminal(root)
    t.set_root(root)
    t.run_command("python --version")   # programatik (ör. hata→ajan)
"""

import os
import sys

from PySide6.QtCore import Qt, Signal, QProcess, QProcessEnvironment
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLineEdit, QLabel, QToolButton,
)

from theme import C, icon, FONT_MONO, RADIUS

_IS_WIN = sys.platform.startswith("win")


class Terminal(QWidget):
    """Tek bir terminal oturumu: çıktı ekranı + komut girişi (prompt'lu)."""

    commandFinished = Signal(str, str, int)   # (komut, çıktı, çıkış kodu)
    titleChanged = Signal(str)

    def __init__(self, root=None, parent=None):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.cwd = os.path.abspath(root) if root else os.path.expanduser("~")
        self._proc = None
        self._out_buf = ""            # çalışan komutun biriken çıktısı (sinyal için)
        self._history = []
        self._hist_idx = 0

        v = QVBoxLayout(self); v.setContentsMargins(10, 8, 10, 10); v.setSpacing(7)

        self.out = QPlainTextEdit(); self.out.setObjectName("termOut"); self.out.setReadOnly(True)
        self.out.setFont(QFont(FONT_MONO, 10))
        self.out.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.out.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        v.addWidget(self.out, 1)

        row = QHBoxLayout(); row.setSpacing(7)
        self.prompt = QLabel(); self.prompt.setObjectName("termPrompt")
        self.prompt.setFont(QFont(FONT_MONO, 10))
        self.inp = QLineEdit(); self.inp.setObjectName("termInput")
        self.inp.setFont(QFont(FONT_MONO, 10))
        self.inp.setPlaceholderText("komut yaz…  (cd, dir/ls, python … ; temizlemek için 'clear')")
        self.inp.returnPressed.connect(self._on_enter)
        self.inp.installEventFilter(self)
        self.stop_btn = QToolButton(); self.stop_btn.setObjectName("termStop")
        self.stop_btn.setIcon(icon("mdi6.stop", color=C["red"])); self.stop_btn.setToolTip("Durdur (Ctrl+C)")
        self.stop_btn.clicked.connect(self.kill); self.stop_btn.setVisible(False)
        row.addWidget(self.prompt); row.addWidget(self.inp, 1); row.addWidget(self.stop_btn)
        v.addLayout(row)

        self._refresh_prompt()
        self._append_sys(f"Terminal — {self.cwd}\n")

    # ---------------- genel API ----------------
    def set_root(self, root):
        if root:
            self.cwd = os.path.abspath(root)
            self._refresh_prompt()
            self.titleChanged.emit(self.tab_title())

    def tab_title(self):
        return os.path.basename(self.cwd.rstrip("/\\")) or self.cwd

    def focus_input(self):
        self.inp.setFocus()

    def run_command(self, cmd):
        """Programatik komut çalıştır (ör. hata→ajan)."""
        self.inp.setText(cmd)
        self._on_enter()

    def shutdown(self):
        self.kill()

    # ---------------- prompt / çıktı ----------------
    def _refresh_prompt(self):
        short = self.cwd
        home = os.path.expanduser("~")
        if short.startswith(home):
            short = "~" + short[len(home):]
        short = short.replace("\\", "/")
        if len(short) > 32:
            short = "…" + short[-31:]
        self.prompt.setText(short + " ❯")

    def _append(self, text, color=None):
        cur = self.out.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.out.setTextCursor(cur)
        if color:
            self.out.appendHtml(f'<span style="color:{color}; white-space:pre-wrap">{_esc(text)}</span>')
        else:
            self.out.insertPlainText(text)
        self.out.verticalScrollBar().setValue(self.out.verticalScrollBar().maximum())

    def _append_sys(self, text):
        self._append(text, color=C["faint"])

    def _echo_cmd(self, cmd):
        self.out.appendHtml(
            f'<span style="color:{C["muted"]}">{_esc(self.prompt.text())} </span>'
            f'<span style="color:{C["text"]}">{_esc(cmd)}</span>')
        # çıktı bir sonraki satırda başlasın (aksi halde komuta yapışır)
        self.out.insertPlainText("\n")
        self.out.verticalScrollBar().setValue(self.out.verticalScrollBar().maximum())

    # ---------------- komut yürütme ----------------
    def _on_enter(self):
        cmd = self.inp.text().strip()
        self.inp.clear()
        if not cmd:
            return
        self._history.append(cmd); self._hist_idx = len(self._history)
        self._echo_cmd(cmd)

        low = cmd.lower()
        if low in ("clear", "cls"):
            self.out.clear(); return
        if low == "cd" or low.startswith("cd ") or low.startswith("cd\t"):
            self._handle_cd(cmd[2:].strip().strip('"'))
            return
        self._spawn(cmd)

    def _handle_cd(self, arg):
        if not arg or arg == "~":
            target = os.path.expanduser("~")
        else:
            target = os.path.abspath(os.path.join(self.cwd, os.path.expanduser(arg)))
        if os.path.isdir(target):
            self.cwd = target
            self._refresh_prompt()
            self.titleChanged.emit(self.tab_title())
        else:
            self._append_sys(f"cd: dizin yok: {arg}\n")

    def _spawn(self, cmd):
        if self._proc is not None:
            self._append_sys("(bir komut zaten çalışıyor)\n"); return
        p = QProcess(self)
        p.setWorkingDirectory(self.cwd)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1"); env.insert("PYTHONIOENCODING", "utf-8")
        p.setProcessEnvironment(env)
        p.readyReadStandardOutput.connect(self._on_output)
        p.finished.connect(self._on_finished)
        p.errorOccurred.connect(self._on_proc_error)
        self._proc = p
        self._out_buf = ""
        self._current_cmd = cmd
        self.stop_btn.setVisible(True); self.inp.setEnabled(False)
        if _IS_WIN:
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            p.setProgram(comspec)
            if hasattr(p, "setNativeArguments"):
                p.setNativeArguments("/d /c " + cmd)   # Qt tırnak sorununu atlar
            else:
                p.setArguments(["/d", "/c", cmd])
        else:
            p.setProgram("/bin/sh"); p.setArguments(["-c", cmd])
        p.start()

    def _on_output(self):
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        self._out_buf += data
        self._append(data)

    def _on_finished(self, code, _status):
        if code != 0:
            self._append_sys(f"[çıkış kodu {code}]\n")
        else:
            self._append("")
        cmd = getattr(self, "_current_cmd", "")
        out = self._out_buf
        self._proc = None
        self.stop_btn.setVisible(False); self.inp.setEnabled(True); self.inp.setFocus()
        self.commandFinished.emit(cmd, out, code)

    def _on_proc_error(self, _err):
        if self._proc is not None:
            self._append_sys("[komut başlatılamadı]\n")
            self._proc = None
            self.stop_btn.setVisible(False); self.inp.setEnabled(True)

    def kill(self):
        if self._proc is not None:
            self._proc.kill()
            self._append_sys("\n[durduruldu]\n")

    # ---------------- geçmiş (yukarı/aşağı) ----------------
    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.inp and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k == Qt.Key.Key_Up:
                self._history_step(-1); return True
            if k == Qt.Key.Key_Down:
                self._history_step(1); return True
            if k == Qt.Key.Key_C and (ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
                if self._proc is not None:
                    self.kill(); return True
        return super().eventFilter(obj, ev)

    def _history_step(self, d):
        if not self._history:
            return
        self._hist_idx = max(0, min(len(self._history), self._hist_idx + d))
        self.inp.setText(self._history[self._hist_idx] if self._hist_idx < len(self._history) else "")


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace("\n", "<br>").replace(" ", "&nbsp;"))


def terminal_style():
    return f"""
QWidget#terminal {{ background:{C['bg']}; }}
QPlainTextEdit#termOut {{ background:transparent; color:{C['text2']}; border:none;
  selection-background-color:{C['accentdim']}; }}
QLabel#termPrompt {{ color:{C['accent']}; font-weight:600; }}
QLineEdit#termInput {{ background:{C['field']}; border:1px solid {C['border']};
  border-radius:{RADIUS['sm']}px; padding:6px 10px; color:{C['text']};
  selection-background-color:{C['accentdim']}; }}
QLineEdit#termInput:focus {{ border-color:{C['accent']}; }}
QToolButton#termStop {{ background:transparent; border:none; border-radius:6px; padding:4px; }}
QToolButton#termStop:hover {{ background:{C['card']}; }}
"""
