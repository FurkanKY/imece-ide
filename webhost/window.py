"""
window.py — frameless yerleşik pencere + tam-boy QWebEngineView.

- Arka plan rengi YÜKLEMEDEN ÖNCE koyu set edilir (beyaz flaş ölür).
- window.* köprü metotları: min/max/kapat + startSystemMove/Resize (HTML titlebar/
  kenar tutamaçları çağırır — chrome.py deseninin köprü portu).
- Geometri kalıcılığı: ui_prefs "window" alanı (v2).
- Kısayollar web tarafındadır; tek Qt kısayolu --dev'de F12 → DevTools.
"""

import json

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import QMainWindow
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

import ui_prefs
from webhost.bridge import HostBridge, handler
from webhost.scheme import UiSchemeHandler

_BG = "#0a0b0d"  # tokens.css --bg ile aynı


class ShellWindow(QMainWindow):
    def __init__(self, dev: bool = False, dev_url: str = "http://localhost:5173"):
        super().__init__()
        self._dev = dev
        self._web_ready = False       # JS köprüsü canlı mı (kapatma koruması için)
        self._close_confirmed = False
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle("Multi-Agent IDE")
        self.setMinimumSize(720, 480)

        # ---- webview ----
        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(QColor(_BG))  # yüklemeden önce!
        self.setCentralWidget(self.view)

        # app:// handler (dev modda da kayıtlı kalır — zararsız)
        self._scheme_handler = UiSchemeHandler(self)
        self.view.page().profile().installUrlSchemeHandler(b"app", self._scheme_handler)

        # ---- köprü ----
        self.bridge = HostBridge(self)
        self._channel = QWebChannel(self)
        self._channel.registerObject("host", self.bridge)
        self.view.page().setWebChannel(self._channel)
        self._register_window_handlers()

        # ---- dosya sistemi izleyicisi ----
        from webhost import watcher
        watcher.init(self.bridge, self)

        # ---- geometri geri yükleme ----
        self._restore_geometry()

        if dev:
            self._devtools = None
            sc = QShortcut(QKeySequence("F12"), self)
            sc.activated.connect(self._open_devtools)

        self.view.load(QUrl(dev_url if dev else "app://ui/index.html"))

    # ---------------- window.* köprü metotları ----------------
    def _register_window_handlers(self):
        @handler("window.minimize")
        def _min(params, ctx):
            self.showMinimized()
            return {}

        @handler("window.toggleMaximize")
        def _max(params, ctx):
            if self.isMaximized():
                self.showNormal()
            else:
                self.showMaximized()
            return {"maximized": self.isMaximized()}

        @handler("window.close")
        def _close(params, ctx):
            self.close()
            return {}

        @handler("window.confirmClose")
        def _confirm_close(params, ctx):
            # web tarafı (kaydedilmemişleri sorup) kapatmayı onayladı
            self._close_confirmed = True
            QTimer.singleShot(0, self.close)
            return {}

        @handler("window.ready")
        def _ready(params, ctx):
            # web kabuğu mount oldu → kapatma koruması devreye girebilir
            self._web_ready = True
            return {}

        @handler("window.setZoom")
        def _zoom(params, ctx):
            # UI zoom (Ctrl+± — VS Code deseni): Chromium sayfa zoom'u, DPI-doğru
            factor = float(params.get("factor", 1.0))
            self.view.page().setZoomFactor(max(0.5, min(2.0, factor)))
            return {}

        @handler("window.startSystemMove")
        def _move(params, ctx):
            self.windowHandle().startSystemMove()
            return {}

        @handler("window.startSystemResize")
        def _resize(params, ctx):
            edges = {
                "left": Qt.Edge.LeftEdge, "right": Qt.Edge.RightEdge,
                "top": Qt.Edge.TopEdge, "bottom": Qt.Edge.BottomEdge,
                "topleft": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
                "topright": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
                "bottomleft": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
                "bottomright": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
            }
            edge = edges.get(params.get("edge", ""))
            if edge is not None and not self.isMaximized():
                self.windowHandle().startSystemResize(edge)
            return {}

    # ---------------- durum olayları ----------------
    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.WindowStateChange, ev.Type.ActivationChange):
            self.bridge.emit_event("window.state", {
                "maximized": self.isMaximized(),
                "focused": self.isActiveWindow(),
            })

    # ---------------- geometri kalıcılığı ----------------
    def _restore_geometry(self):
        prefs = ui_prefs.load()
        w = prefs.get("window")
        if isinstance(w, dict) and all(k in w for k in ("x", "y", "w", "h")):
            self.setGeometry(w["x"], w["y"], w["w"], w["h"])
            if w.get("maximized"):
                QTimer.singleShot(0, self.showMaximized)
        else:
            self.resize(1320, 880)

    def closeEvent(self, ev):
        # Kaydedilmemiş-değişiklik koruması: kapatmayı web tarafına sor.
        # Web dirty sekmeleri kontrol eder; onaylarsa window.confirmClose çağırır.
        # Web hiç yüklenmediyse (çökme/boş sayfa) koruma atlanır — pencere kilitlenmesin.
        if self._web_ready and not getattr(self, "_close_confirmed", False):
            ev.ignore()
            self.bridge.emit_event("window.closeRequested", {})
            return
        g = self.normalGeometry()
        ui_prefs.save({"window": {"x": g.x(), "y": g.y(), "w": g.width(),
                                  "h": g.height(), "maximized": self.isMaximized()}})
        # koşan ajan thread'i + PTY'leri kapat (zombi önleme)
        try:
            from webhost.api import run as run_api
            run_api.shutdown()
        except Exception:
            pass
        try:
            from webhost.api import terminal as term_api
            term_api.shutdown()
        except Exception:
            pass
        try:
            from webhost.api import lsp as lsp_api
            lsp_api.shutdown()
        except Exception:
            pass
        try:
            from webhost.api import exec as exec_api
            exec_api.shutdown()
        except Exception:
            pass
        super().closeEvent(ev)

    # ---------------- devtools (--dev) ----------------
    def _open_devtools(self):
        if self._devtools is None:
            self._devtools = QWebEngineView()
            self._devtools.setWindowTitle("DevTools — Multi-Agent IDE")
            self._devtools.resize(1100, 700)
            self.view.page().setDevToolsPage(self._devtools.page())
        self._devtools.show()
        self._devtools.raise_()
