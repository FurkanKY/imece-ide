"""
command_palette.py
------------------
Faz 2 — Cursor/Linear imzası: Ctrl+K komut paleti, Ctrl+P dosyaya git.
Ortalanmış, gölgeli, fuzzy aramalı frameless overlay.

Kullanım (desktop.py):
    self.palette = CommandPalette(self)
    self.palette.open_commands([(baslik, altbaslik, "mdi6.play", callback), ...])
    self.palette.open_files(["a.py","src/b.py"], on_pick=self.editor.open_file)
"""

from PySide6.QtCore import Qt, QEvent, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFrame, QLineEdit, QListWidget, QListWidgetItem,
    QLabel, QHBoxLayout, QWidget,
)

from PySide6.QtCore import QPropertyAnimation, QAbstractAnimation, QEasingCurve
from theme import C, icon, file_icon, add_shadow, RADIUS, FONT_MONO
from anim import fade_in, FAST


def _fuzzy(query, text):
    """Basit alt-dizi fuzzy skoru. Eşleşmezse None; küçük skor = daha iyi."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    i = 0
    first = None
    gaps = 0
    for ch in q:
        j = t.find(ch, i)
        if j < 0:
            return None
        if first is None:
            first = j
        if i and j > i:
            gaps += j - i
        i = j + 1
    return first + gaps  # baştan yakın + boşluksuz = düşük skor


class CommandPalette(QDialog):
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._items = []       # (title, subtitle, icon_name, payload)
        self._mode = "cmd"
        self._on_pick = None

        outer = QVBoxLayout(self); outer.setContentsMargins(26, 24, 26, 30)
        self._card = QFrame(); self._card.setObjectName("palette")
        add_shadow(self._card, blur=52, y=18, alpha=190)
        outer.addWidget(self._card)
        v = QVBoxLayout(self._card); v.setContentsMargins(10, 10, 10, 10); v.setSpacing(8)

        self.search = QLineEdit(); self.search.setObjectName("paletteSearch")
        self.search.setPlaceholderText("Komut ara…")
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        v.addWidget(self.search)

        self.list = QListWidget(); self.list.setObjectName("paletteList")
        self.list.itemActivated.connect(lambda it: self._run(it))
        self.list.itemClicked.connect(lambda it: self._run(it))
        v.addWidget(self.list)

        self.resize(600, 420)

    # ---- açılışlar ----
    def open_commands(self, commands):
        self._mode = "cmd"; self._on_pick = None
        self._items = commands
        self.search.setPlaceholderText("Komut ara…")
        self._show()

    def open_files(self, files, on_pick):
        self._mode = "file"; self._on_pick = on_pick
        self._items = [(f, "", None, f) for f in files]
        self.search.setPlaceholderText("Dosyaya git…")
        self._show()

    def _show(self):
        self.search.clear()
        self._filter("")
        # ana pencere üstünde ortala
        p = self.parent()
        tx, ty = 200, 120
        if p is not None:
            g = p.frameGeometry()
            tx = g.center().x() - self.width() // 2
            ty = g.top() + max(60, g.height() // 6)
        # açılış animasyonu: fade (windowOpacity) + yukarıdan hafif kayma
        self.setWindowOpacity(0.0)
        self.move(tx, ty - 12)
        self.show(); self.raise_()
        self.search.setFocus()
        oa = QPropertyAnimation(self, b"windowOpacity", self)
        oa.setDuration(FAST); oa.setStartValue(0.0); oa.setEndValue(1.0)
        oa.setEasingCurve(QEasingCurve.Type.OutCubic)
        oa.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped); self._oa = oa
        pa = QPropertyAnimation(self, b"pos", self)
        from PySide6.QtCore import QPoint
        pa.setDuration(FAST + 40); pa.setStartValue(QPoint(tx, ty - 12)); pa.setEndValue(QPoint(tx, ty))
        pa.setEasingCurve(QEasingCurve.Type.OutCubic)
        pa.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped); self._pa = pa

    # ---- filtre / render ----
    def _filter(self, text):
        text = text.strip()
        scored = []
        for it in self._items:
            title = it[0]
            s = _fuzzy(text, title + " " + (it[1] or ""))
            if s is not None:
                scored.append((s, it))
        scored.sort(key=lambda x: x[0])
        self.list.clear()
        for _s, it in scored[:200]:
            title, subtitle, icon_name, payload = it
            row = QListWidgetItem()
            if self._mode == "file":
                row.setIcon(file_icon(title))
            elif icon_name:
                row.setIcon(icon(icon_name, color=C["muted"]))
            row.setText(title if not subtitle else f"{title}")
            row.setData(Qt.ItemDataRole.UserRole, payload)
            row.setData(Qt.ItemDataRole.UserRole + 1, subtitle or "")
            self.list.addItem(row)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _run(self, item):
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        if self._mode == "file":
            if self._on_pick and payload:
                self._on_pick(payload)
        else:
            if callable(payload):
                payload()

    # ---- klavye: arama alanından Up/Down/Enter/Esc yönlendir ----
    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                n = self.list.count()
                if n:
                    cur = self.list.currentRow()
                    cur = (cur + (1 if k == Qt.Key.Key_Down else -1)) % n
                    self.list.setCurrentRow(cur)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run(self.list.currentItem())
                return True
            if k == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, ev)


def palette_style():
    return f"""
QFrame#palette {{ background:{C['panel2']}; border:1px solid {C['border2']};
  border-radius:{RADIUS['lg']}px; }}
QLineEdit#paletteSearch {{ background:{C['field']}; border:1px solid {C['border2']};
  border-radius:{RADIUS['sm']}px; padding:10px 13px; color:{C['text']}; font-size:15px;
  selection-background-color:{C['accentdim']}; }}
QLineEdit#paletteSearch:focus {{ border-color:{C['accent']}; }}
QListWidget#paletteList {{ background:transparent; border:none; outline:0; font-size:13px; }}
QListWidget#paletteList::item {{ padding:9px 11px; border-radius:{RADIUS['sm']}px;
  color:{C['text2']}; margin:1px 0; }}
QListWidget#paletteList::item:selected {{ background:{C['accentdim']}; color:{C['text']}; }}
QListWidget#paletteList::item:hover {{ background:{C['card']}; }}
"""
