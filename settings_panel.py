"""
settings_panel.py
-----------------
Faz 5 — Ayarlar overlay'i (koyu, ortalanmış, gölgeli). command_palette / chrome
popup desenini izler. Değişiklikler anında `changed` sinyaliyle yayılır; desktop
tarafı temayı yeniden uygular ve ui_prefs ile kalıcılaştırır.

Kullanım (desktop.py):
    self.settings = SettingsDialog(self)
    self.settings.changed.connect(self._apply_prefs)
    self.settings.open_with(self._prefs)
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QToolButton,
    QPushButton, QCheckBox, QButtonGroup,
)

from theme import C, ACCENTS, RADIUS, add_shadow
from anim import pop_in

DENSITIES = [("comfortable", "Rahat"), ("compact", "Kompakt")]


class SettingsDialog(QDialog):
    changed = Signal(dict)

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._loading = False

        outer = QVBoxLayout(self); outer.setContentsMargins(26, 24, 26, 30)
        card = QFrame(); card.setObjectName("settingsCard")
        add_shadow(card, blur=52, y=18, alpha=190)
        outer.addWidget(card)
        v = QVBoxLayout(card); v.setContentsMargins(20, 18, 20, 20); v.setSpacing(16)

        # başlık
        head = QHBoxLayout()
        title = QLabel("Ayarlar"); title.setObjectName("setTitle")
        close = QToolButton(); close.setObjectName("setClose"); close.setText("✕")
        close.clicked.connect(self.reject)
        head.addWidget(title); head.addStretch(1); head.addWidget(close)
        v.addLayout(head)

        # accent rengi
        v.addWidget(self._label("ACCENT RENGİ"))
        self.accent_group = QButtonGroup(self); self.accent_group.setExclusive(True)
        self._accent_keys = {}
        arow = QHBoxLayout(); arow.setSpacing(10)
        for i, (key, (col, _c2, _cd, _oa)) in enumerate(ACCENTS.items()):
            b = QToolButton(); b.setCheckable(True); b.setFixedSize(28, 28)
            b.setStyleSheet(
                f"QToolButton{{background:{col}; border-radius:14px; border:2px solid transparent;}}"
                f"QToolButton:checked{{border:2px solid {C['text']};}}"
                f"QToolButton:hover{{border:2px solid {C['border2']};}}"
            )
            b.setToolTip(key)
            self.accent_group.addButton(b, i)
            self._accent_keys[i] = key
            arow.addWidget(b)
        arow.addStretch(1)
        v.addLayout(arow)
        self.accent_group.idToggled.connect(lambda _i, on: on and self._emit())

        # yoğunluk (segmented)
        v.addWidget(self._label("YOĞUNLUK"))
        self.density_group = QButtonGroup(self); self.density_group.setExclusive(True)
        self._density_keys = {}
        drow = QHBoxLayout(); drow.setSpacing(8)
        for i, (key, tr) in enumerate(DENSITIES):
            b = QPushButton(tr); b.setObjectName("seg"); b.setCheckable(True)
            self.density_group.addButton(b, i)
            self._density_keys[i] = key
            drow.addWidget(b)
        drow.addStretch(1)
        v.addLayout(drow)
        self.density_group.idToggled.connect(lambda _i, on: on and self._emit())

        # toggle'lar
        self.chk_enter = QCheckBox("Enter ile gönder  (Shift+Enter yeni satır)")
        self.chk_anim = QCheckBox("Mikro-animasyonlar")
        for chk in (self.chk_enter, self.chk_anim):
            chk.toggled.connect(lambda _on: self._emit())
            v.addWidget(chk)

        card.setStyleSheet(self._style())
        self.resize(440, 400)

    def _label(self, text):
        lbl = QLabel(text); lbl.setObjectName("setSection")
        return lbl

    # ---- açılış ----
    def open_with(self, prefs):
        self._loading = True
        # accent
        keys = list(ACCENTS.keys())
        idx = keys.index(prefs.get("accent", "blue")) if prefs.get("accent") in keys else 0
        btn = self.accent_group.button(idx)
        if btn:
            btn.setChecked(True)
        # density
        dkeys = [k for k, _ in DENSITIES]
        didx = dkeys.index(prefs.get("density", "comfortable")) if prefs.get("density") in dkeys else 0
        dbtn = self.density_group.button(didx)
        if dbtn:
            dbtn.setChecked(True)
        self.chk_enter.setChecked(bool(prefs.get("enter_to_send", True)))
        self.chk_anim.setChecked(bool(prefs.get("animations", True)))
        self._loading = False

        card = self.findChild(QFrame, "settingsCard")
        if card:
            card.setStyleSheet(self._style())   # güncel accent'i yansıt

        p = self.parent()
        if p is not None:
            g = p.frameGeometry()
            self.move(g.center().x() - self.width() // 2, g.top() + max(60, g.height() // 6))
        self.show(); self.raise_()
        pop_in(self)

    def _current(self):
        aid = self.accent_group.checkedId()
        did = self.density_group.checkedId()
        return {
            "accent": self._accent_keys.get(aid, "blue"),
            "density": self._density_keys.get(did, "comfortable"),
            "enter_to_send": self.chk_enter.isChecked(),
            "animations": self.chk_anim.isChecked(),
        }

    def _emit(self):
        if not self._loading:
            self.changed.emit(self._current())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(e)

    def _style(self):
        return f"""
QFrame#settingsCard {{ background:{C['panel2']}; border:1px solid {C['border2']};
  border-radius:{RADIUS['lg']}px; }}
QLabel#setTitle {{ color:{C['text']}; font-size:16px; font-weight:800; }}
QLabel#setSection {{ color:{C['faint']}; font-size:10px; font-weight:800; letter-spacing:1.4px; }}
QToolButton#setClose {{ background:transparent; border:none; color:{C['muted']};
  font-size:15px; border-radius:7px; padding:2px 7px; }}
QToolButton#setClose:hover {{ background:{C['card2']}; color:{C['text']}; }}
QPushButton#seg {{ background:{C['card']}; border:1px solid {C['border2']}; color:{C['text2']};
  border-radius:{RADIUS['sm']}px; padding:7px 18px; }}
QPushButton#seg:checked {{ background:{C['accentdim']}; border-color:{C['accent']}; color:{C['text']}; }}
QCheckBox {{ color:{C['text2']}; font-size:13px; spacing:9px; }}
"""
