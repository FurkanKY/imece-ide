"""
bottom_panel.py
---------------
T1.2 — VS Code tarzı alt panel. Şimdilik çok sekmeli **Terminal**'i barındırır;
ileride Sorunlar / Çıktı sekmeleri için aynı çatı kullanılır.

- Sol köşe: "TERMINAL" başlığı.  Sekmeler: her terminal (kapatılabilir).
- Sağ köşe: [+ yeni terminal]  [▾ daralt/gizle].
- `commandFinished(cmd, out, code)` aktif terminalden yeniden yayınlanır → ileride
  **hata→ajan** döngüsü buna bağlanır.

Kullanım (desktop.py):
    self.bottom = BottomPanel()
    self.bottom.collapseRequested.connect(lambda: self._toggle_bottom(force_hide=True))
    self.bottom.set_root(project_root)
"""

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QWidget, QTabWidget, QLabel, QToolButton

from theme import C, icon, icon_png, RADIUS
from terminal import Terminal, terminal_style


class BottomPanel(QFrame):
    collapseRequested = Signal()
    commandFinished = Signal(str, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("bottomPanel")
        self._root = None
        self._counter = 0

        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        self.tabs = QTabWidget(); self.tabs.setObjectName("termTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)

        head = QLabel("  TERMINAL"); head.setObjectName("termHead")
        self.tabs.setCornerWidget(head, Qt.Corner.TopLeftCorner)

        actions = QWidget()
        ah = QHBoxLayout(actions); ah.setContentsMargins(0, 0, 6, 0); ah.setSpacing(2)
        new_btn = QToolButton(); new_btn.setObjectName("act")
        new_btn.setIcon(icon("mdi6.plus", color=C["muted"])); new_btn.setIconSize(QSize(16, 16))
        new_btn.setToolTip("Yeni terminal"); new_btn.clicked.connect(self.add_terminal)
        col_btn = QToolButton(); col_btn.setObjectName("act")
        col_btn.setIcon(icon("mdi6.chevron-down", color=C["muted"])); col_btn.setIconSize(QSize(18, 18))
        col_btn.setToolTip("Paneli gizle (Ctrl+`)"); col_btn.clicked.connect(self.collapseRequested.emit)
        ah.addWidget(new_btn); ah.addWidget(col_btn)
        self.tabs.setCornerWidget(actions, Qt.Corner.TopRightCorner)

        v.addWidget(self.tabs)
        self.add_terminal()

    # ---------------- terminal yönetimi ----------------
    def add_terminal(self):
        self._counter += 1
        t = Terminal(self._root)
        t.commandFinished.connect(self.commandFinished)
        idx = self.tabs.addTab(t, icon("mdi6.console", color=C["muted"]), f"Terminal {self._counter}")
        self.tabs.setCurrentIndex(idx)
        t.focus_input()
        return t

    def _close_tab(self, i):
        w = self.tabs.widget(i)
        if w is not None:
            w.shutdown()
        self.tabs.removeTab(i)
        if self.tabs.count() == 0:
            self.collapseRequested.emit()   # son sekme kapandı → paneli gizle

    def current(self):
        return self.tabs.currentWidget()

    def ensure_terminal(self):
        if self.tabs.count() == 0:
            self.add_terminal()
        return self.current()

    def focus_current(self):
        t = self.ensure_terminal()
        if t is not None:
            t.focus_input()

    def set_root(self, root):
        self._root = root
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, Terminal):
                w.set_root(root)

    def run_command(self, cmd):
        """Programatik (ör. hata→ajan): aktif terminalde çalıştır."""
        t = self.ensure_terminal()
        if t is not None:
            t.run_command(cmd)


def bottom_panel_style():
    x = icon_png("mdi6.close", C["muted"], 14)
    xh = icon_png("mdi6.close", C["text"], 14)
    return terminal_style() + f"""
QFrame#bottomPanel {{ background:{C['bg']}; border-top:1px solid {C['border']}; }}
QTabWidget#termTabs::pane {{ border:none; background:{C['bg']}; }}
QTabWidget#termTabs QTabBar::close-button {{ image:url({x}); subcontrol-position:right;
  padding:2px; }}
QTabWidget#termTabs QTabBar::close-button:hover {{ image:url({xh}); }}
QLabel#termHead {{ color:{C['faint']}; font-size:10px; font-weight:800; letter-spacing:1.4px;
  padding-left:6px; }}
"""
