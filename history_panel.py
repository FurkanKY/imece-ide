"""
history_panel.py
----------------
Faz E — oturum geçmişi overlay'i (koyu, ortalanmış). Geçmiş koşuları listeler;
bir satıra tıklanınca görev geri yüklenir (`picked` sinyali). command_palette /
settings_panel popup desenini izler.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QToolButton,
    QListWidget, QListWidgetItem, QWidget,
)

from theme import C, RADIUS, FONT_MONO, add_shadow
from anim import pop_in
from history import rel_time

_VERDICT_COLOR = {"APPROVED": C["green"], "NEEDS_FIX": C["amber"]}


class HistoryDialog(QDialog):
    picked = Signal(str)   # görev metni

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self); outer.setContentsMargins(26, 24, 26, 30)
        self.card = QFrame(); self.card.setObjectName("histCard")
        add_shadow(self.card, blur=52, y=18, alpha=190)
        outer.addWidget(self.card)
        v = QVBoxLayout(self.card); v.setContentsMargins(16, 14, 16, 16); v.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Geçmiş"); title.setObjectName("histTitle")
        close = QToolButton(); close.setObjectName("histClose"); close.setText("✕")
        close.clicked.connect(self.reject)
        head.addWidget(title); head.addStretch(1); head.addWidget(close)
        v.addLayout(head)

        self.list = QListWidget(); self.list.setObjectName("histList")
        self.list.itemActivated.connect(self._pick)
        self.list.itemClicked.connect(self._pick)
        v.addWidget(self.list)

        self.empty = QLabel("Henüz koşu yok — bir görev çalıştırınca burada birikir.")
        self.empty.setObjectName("histEmpty"); self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        v.addWidget(self.empty)

        self.resize(560, 460)

    def open_with(self, records):
        self.list.clear()
        for rec in records:
            self._add_row(rec)
        has = bool(records)
        self.list.setVisible(has); self.empty.setVisible(not has)
        self.card.setStyleSheet(self._style())

        p = self.parent()
        if p is not None:
            g = p.frameGeometry()
            self.move(g.center().x() - self.width() // 2, g.top() + max(60, g.height() // 6))
        self.show(); self.raise_()
        pop_in(self)

    def _add_row(self, rec):
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(10, 7, 12, 7); h.setSpacing(10)
        dot = QLabel(); dot.setFixedSize(7, 7)
        col = _VERDICT_COLOR.get(rec.get("verdict"), C["faint"])
        dot.setStyleSheet(f"background:{col}; border-radius:3px;")
        h.addWidget(dot)

        vb = QVBoxLayout(); vb.setSpacing(1)
        task = (rec.get("task") or "(görev yok)").replace("\n", " ")
        tl = QLabel(task[:90]); tl.setObjectName("histTask")
        meta = f'{rel_time(rec.get("ts", 0))} · {rec.get("tokens",0)} tok · ${rec.get("cost_usd",0)}'
        nf = len(rec.get("files") or [])
        if nf:
            meta += f' · {nf} dosya'
        ml = QLabel(meta); ml.setObjectName("histMeta")
        vb.addWidget(tl); vb.addWidget(ml)
        h.addLayout(vb, 1)

        item = QListWidgetItem(self.list)
        item.setSizeHint(row.sizeHint())
        item.setData(Qt.ItemDataRole.UserRole, rec.get("task", ""))
        self.list.addItem(item)
        self.list.setItemWidget(item, row)

    def _pick(self, item):
        task = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        if task:
            self.picked.emit(task)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(e)

    def _style(self):
        return f"""
QFrame#histCard {{ background:{C['panel2']}; border:1px solid {C['border2']};
  border-radius:{RADIUS['lg']}px; }}
QLabel#histTitle {{ color:{C['text']}; font-size:16px; font-weight:800; }}
QToolButton#histClose {{ background:transparent; border:none; color:{C['muted']};
  font-size:15px; border-radius:7px; padding:2px 7px; }}
QToolButton#histClose:hover {{ background:{C['card2']}; color:{C['text']}; }}
QLabel#histEmpty {{ color:{C['faint']}; font-size:13px; padding:24px; }}
QLabel#histTask {{ color:{C['text']}; font-size:13px; font-weight:600; }}
QLabel#histMeta {{ color:{C['muted']}; font-size:11px; font-family:'{FONT_MONO}'; }}
QListWidget#histList {{ background:transparent; border:none; outline:0; }}
QListWidget#histList::item {{ border-radius:{RADIUS['sm']}px; margin:1px 0; }}
QListWidget#histList::item:selected, QListWidget#histList::item:hover {{
  background:{C['card']}; }}
"""
