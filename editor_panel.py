"""
editor_panel.py
---------------
Monaco editörünü (web/editor/index.html) bir QWidget içine gömer ve Python ile
JavaScript arasında QWebChannel köprüsü kurar.

Python -> JS : self._run_js(...)  (window.API.* çağırır)
JS -> Python : Bridge slot'ları -> Qt sinyalleri (saved/dirty/breakpoints/ready)

Kullanım (desktop.py):
    ed = EditorPanel(project_root)
    ed.saved.connect(...)      # (path, content) diske yazılır
    ed.open_file("src/x.py")
"""

import os
import json

from PySide6.QtCore import QObject, Signal, Slot, QUrl
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from project import Project

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX = os.path.join(_HERE, "web", "editor", "index.html")


class _Bridge(QObject):
    """JS -> Python kanalı. Slot'lar JavaScript'ten çağrılır."""
    ready_sig = Signal()
    saved = Signal(str, str)          # (path, content)
    dirty = Signal(str, bool)         # (path, dirty?)
    breakpoints = Signal(str, list)   # (path, [satır no])

    @Slot()
    def ready(self):
        self.ready_sig.emit()

    @Slot(str, str)
    def fileSaved(self, path, content):
        self.saved.emit(path, content)

    @Slot(str, bool)
    def dirtyChanged(self, path, is_dirty):
        self.dirty.emit(path, is_dirty)

    @Slot(str, str)
    def breakpointsChanged(self, path, lines_json):
        try:
            lines = json.loads(lines_json)
        except ValueError:
            lines = []
        self.breakpoints.emit(path, lines)


class EditorPanel(QWidget):
    # dışarıya yeniden yayınlanan sinyaller
    saved = Signal(str, str)
    dirtyChanged = Signal(str, bool)
    breakpointsChanged = Signal(str, list)
    ready = Signal()

    def __init__(self, project_root=None, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self._ready = False
        self._pending = []   # editör hazır olana kadar biriken JS çağrıları

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        lay.addWidget(self.view)

        self._bridge = _Bridge()
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self.view.page().setWebChannel(self._channel)

        # köprü sinyallerini panele bağla
        self._bridge.ready_sig.connect(self._on_ready)
        self._bridge.saved.connect(self.saved)
        self._bridge.dirty.connect(self.dirtyChanged)
        self._bridge.breakpoints.connect(self.breakpointsChanged)

        self.view.load(QUrl.fromLocalFile(_INDEX))

    # ---------------- iç ----------------
    def _on_ready(self):
        self._ready = True
        for js in self._pending:
            self.view.page().runJavaScript(js)
        self._pending.clear()
        self.ready.emit()

    def _run_js(self, js):
        if self._ready:
            self.view.page().runJavaScript(js)
        else:
            self._pending.append(js)

    @staticmethod
    def _j(x):
        return json.dumps(x, ensure_ascii=False)

    # ---------------- Python API ----------------
    def set_project(self, root):
        self.project_root = root

    def open_file(self, rel):
        """Proje kökünden dosyayı okuyup editörde (yeni sekmede) açar."""
        if not self.project_root:
            return
        content = Project(self.project_root).read_file(rel)
        self._run_js(f"window.API.openFile({self._j(rel)},{self._j(content)},null)")

    def set_content(self, rel, content):
        """Ajan diff'i uygulandıktan sonra açık sekmeyi tazele."""
        self._run_js(f"window.API.setContent({self._j(rel)},{self._j(content)})")

    def reload(self, rel):
        if self.project_root and Project(self.project_root).exists(rel):
            self.set_content(rel, Project(self.project_root).read_file(rel))

    def mark_saved(self, rel):
        self._run_js(f"window.API.markSaved({self._j(rel)})")

    def open_diff(self, rel, original, modified):
        """Merkez inline diff editöründe önerilen değişikliği tam boy göster."""
        self._run_js(
            f"window.API.openDiff({self._j(rel)},{self._j(original)},{self._j(modified)},null)"
        )

    def close_diff(self):
        self._run_js("window.API.closeDiff()")

    def close_file(self, rel):
        """Açık bir dosyayı editör sekmesinden kapat (silme/yeniden adlandırmada)."""
        self._run_js(f"window.API.closeTab({self._j(rel)})")

    def save_active(self):
        """Aktif sekmeyi kaydet (JS bridge.fileSaved'i tetikler)."""
        self._run_js("window.API.saveActive()")

    # ---- Faz 3 (debugger) için hazır ----
    def show_debug_line(self, rel, line):
        self._run_js(f"window.API.showDebugLine({self._j(rel)},{int(line)})")

    def clear_debug_line(self):
        self._run_js("window.API.clearDebugLine()")
