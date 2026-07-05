"""
chrome.py
---------
Özel pencere kromu (frameless): Windows'un varsayılan başlık çubuğunu kaldırıp
uygulamanın kendi koyu başlık/komut çubuğunu kullanmasını sağlar — "gerçek uygulama"
hissi. Native davranışlar (aero snap, kenardan boyutlandırma) Qt6'nın
`QWindow.startSystemMove()` / `startSystemResize()` ile korunur (ctypes gerekmez).

Kullanım (desktop.py):
    make_frameless(self)                     # __init__ içinde, pencere kurulduktan sonra
    titlebar = TitleBar(self)                # sürüklenebilir + çift tık maximize
    ... titlebar içine kendi kontrollerini ekle ...
"""

from PySide6.QtCore import Qt, QObject, QEvent, QPoint
from PySide6.QtWidgets import QFrame, QApplication


class TitleBar(QFrame):
    """Sürüklenince pencereyi taşır (native move-loop → aero snap), çift tıkla
    maximize/normal geçişi yapar. İçine yerleşim ekleyip kendi kontrollerini koy."""

    def __init__(self, window, height=44):
        super().__init__()
        self._win = window
        self.setObjectName("titlebar")
        self.setFixedHeight(height)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._win.isMaximized():
            h = self._win.windowHandle()
            if h is not None:
                h.startSystemMove()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()
        super().mouseDoubleClickEvent(e)

    def _toggle_max(self):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()


class _ResizeFilter(QObject):
    """Uygulama geneli mouse filtresi: pencere kenarına (MARGIN px) gelince yeniden
    boyutlandırma imleci gösterir ve basınca native resize başlatır."""

    MARGIN = 7

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._cursor_on = False
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.MouseMove:
            if not self.win.isMaximized() and self.win.isActiveWindow():
                self._apply_cursor(ev.globalPosition().toPoint())
        elif t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            if not self.win.isMaximized():
                edges = self._edges(ev.globalPosition().toPoint())
                if edges:
                    h = self.win.windowHandle()
                    if h is not None:
                        self._clear_cursor()
                        h.startSystemResize(edges)
                        return True
        return False

    def _edges(self, gp):
        r = self.win.frameGeometry()
        m = self.MARGIN
        e = Qt.Edge(0)
        if abs(gp.x() - r.left()) <= m:
            e |= Qt.Edge.LeftEdge
        if abs(gp.x() - r.right()) <= m:
            e |= Qt.Edge.RightEdge
        if abs(gp.y() - r.top()) <= m:
            e |= Qt.Edge.TopEdge
        if abs(gp.y() - r.bottom()) <= m:
            e |= Qt.Edge.BottomEdge
        return e if int(e) else None

    def _apply_cursor(self, gp):
        e = self._edges(gp)
        if not e:
            self._clear_cursor()
            return
        horiz = bool(e & Qt.Edge.LeftEdge) or bool(e & Qt.Edge.RightEdge)
        vert = bool(e & Qt.Edge.TopEdge) or bool(e & Qt.Edge.BottomEdge)
        tl_br = ((e & Qt.Edge.LeftEdge and e & Qt.Edge.TopEdge) or
                 (e & Qt.Edge.RightEdge and e & Qt.Edge.BottomEdge))
        tr_bl = ((e & Qt.Edge.RightEdge and e & Qt.Edge.TopEdge) or
                 (e & Qt.Edge.LeftEdge and e & Qt.Edge.BottomEdge))
        if tl_br:
            shape = Qt.CursorShape.SizeFDiagCursor
        elif tr_bl:
            shape = Qt.CursorShape.SizeBDiagCursor
        elif horiz and vert:
            shape = Qt.CursorShape.SizeAllCursor
        elif horiz:
            shape = Qt.CursorShape.SizeHorCursor
        else:
            shape = Qt.CursorShape.SizeVerCursor
        QApplication.setOverrideCursor(shape) if not self._cursor_on else \
            QApplication.changeOverrideCursor(shape)
        self._cursor_on = True

    def _clear_cursor(self):
        if self._cursor_on:
            QApplication.restoreOverrideCursor()
            self._cursor_on = False


def make_frameless(window):
    """Pencereyi frameless yapar ve kenar-boyutlandırma filtresini kurar.
    Döndürdüğü filtreyi çağıran canlı tutmalı (window'a parent'landığından tutulur)."""
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    return _ResizeFilter(window)
