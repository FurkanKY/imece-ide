"""
chat_view.py
------------
Faz 1 — Sohbet mesaj kartları. Ham HTML log yerine, ajan akışını **kart tabanlı
konuşma** olarak gösterir (Cursor/Linear his). Her aşama (Plan/Kod/İnceleme) açılır-
kapanır bir kart; üstte kullanıcı görev kartı; sonda "N dosya değişti" özet kartı.

Kullanım (desktop.py::on_event):
    self.chat = ChatView()
    self.chat.openChangesRequested.connect(lambda: self._focus_view("diff"))
    ...
    self.chat.clear()
    self.chat.add_user_task("utils.py'yi ISO 8601 yap")
    self.chat.start_stage("plan", "Planlama", "claude")     # aşama kartı aç
    self.chat.append_output("plan", "…metin…")               # gövdeye ekle
    self.chat.set_metric("plan", "claude-code", 8.5, 176, 0.029)
    self.chat.set_verdict("APPROVED", "sorun yok")
    self.chat.add_summary(2)                                  # özet kartı
    self.chat.add_error("mesaj")                              # hata kartı
"""

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QScrollArea, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QToolButton,
    QPushButton,
)

from theme import C, icon, grad, FONT_MONO, RADIUS
from anim import fade_in, animate_height, Blink, FAST

STAGE_META = {
    "plan":   ("Planlama", "mdi6.lightbulb-on-outline", "planner"),
    "code":   ("Kod üretimi", "mdi6.code-braces", "coder"),
    "review": ("İnceleme", "mdi6.eye-check-outline", "reviewer"),
}
_VERDICT = {
    "APPROVED": ("ONAYLANDI", "mdi6.check-decagram", C["green"]),
    "NEEDS_FIX": ("DÜZELTME GEREKLİ", "mdi6.alert-decagram", C["amber"]),
    "UNKNOWN": ("KARAR OKUNAMADI", "mdi6.help-circle-outline", C["muted"]),
}


def _dot(color, size=8):
    d = QLabel(); d.setFixedSize(size, size)
    d.setStyleSheet(f"background:{color}; border-radius:{size//2}px;")
    return d


class StageCard(QFrame):
    """Bir ajan aşaması: başlık (ikon+ad+model+durum) + açılır gövde + footer metrik."""

    def __init__(self, stage):
        super().__init__()
        self.setObjectName("chatCard")
        self.stage = stage
        title, icon_name, _role = STAGE_META.get(stage, (stage, "mdi6.robot", ""))
        self._buf = ""
        self._open = True

        v = QVBoxLayout(self); v.setContentsMargins(13, 11, 13, 11); v.setSpacing(8)

        head = QHBoxLayout(); head.setSpacing(9)
        self.ic = QToolButton(); self.ic.setObjectName("cardIcon"); self.ic.setEnabled(False)
        self.ic.setIcon(icon(icon_name, color=C["accent"])); self.ic.setIconSize(QSize(17, 17))
        self.title = QLabel(title); self.title.setObjectName("cardTitle")
        self.badge = QLabel(""); self.badge.setObjectName("cardBadge")
        self.dot = _dot(C["accent"])
        self.chev = QToolButton(); self.chev.setObjectName("cardChev")
        self.chev.setIcon(icon("mdi6.chevron-up", color=C["faint"])); self.chev.setIconSize(QSize(16, 16))
        self.chev.clicked.connect(self.toggle)
        head.addWidget(self.ic); head.addWidget(self.title); head.addWidget(self.dot)
        head.addStretch(1); head.addWidget(self.badge); head.addWidget(self.chev)
        v.addLayout(head)

        # aç/kapa ile animasyonlu içerik konteynırı
        self.content = QWidget(); self.content.setObjectName("cardContent")
        cv = QVBoxLayout(self.content); cv.setContentsMargins(0, 2, 0, 0); cv.setSpacing(8)
        self.body = QLabel(""); self.body.setObjectName("cardBody")
        self.body.setWordWrap(True); self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setVisible(False)
        self.verdict = QLabel(""); self.verdict.setObjectName("cardVerdict"); self.verdict.setVisible(False)
        self.footer = QLabel(""); self.footer.setObjectName("cardFooter"); self.footer.setVisible(False)
        cv.addWidget(self.body); cv.addWidget(self.verdict); cv.addWidget(self.footer)
        v.addWidget(self.content)

        # aktif kart: nokta yanıp söner (yazıyor/çalışıyor imleci) — metrik gelince durur
        self._blink = Blink(self.dot); self._blink.start()

    def set_model(self, provider):
        self.badge.setText(provider or "")

    def append(self, text):
        self._buf += text
        self.body.setText(self._buf.strip())
        self.body.setVisible(self._open and bool(self._buf.strip()))

    def _stop_blink(self):
        if self._blink is not None:
            self._blink.stop(); self._blink = None

    def set_metric(self, model, latency, tokens, cost):
        self._stop_blink()
        self.badge.setText(model or self.badge.text())
        self.footer.setText(f"{latency}s · {tokens} tok · ${cost}")
        self.footer.setVisible(True)
        self.dot.setStyleSheet(f"background:{C['green']}; border-radius:4px;")
        self.ic.setIcon(icon(STAGE_META.get(self.stage, ('', 'mdi6.robot'))[1], color=C["green"]))

    def set_verdict(self, verdict, note=""):
        label, ic, color = _VERDICT.get(verdict, _VERDICT["UNKNOWN"])
        txt = f"  {label}" + (f" — {note}" if note else "")
        self.verdict.setText(txt)
        self.verdict.setStyleSheet(
            f"#cardVerdict {{ color:{color}; background:{C['card2']}; border:1px solid {color};"
            f" border-radius:8px; padding:5px 9px; font-size:11px; font-weight:700; }}")
        self.verdict.setVisible(True)

    def toggle(self):
        self._open = not self._open
        self.chev.setIcon(icon("mdi6.chevron-up" if self._open else "mdi6.chevron-down",
                               color=C["faint"]))
        if self._open:
            self.content.setVisible(True)
            target = self.content.sizeHint().height()
            animate_height(self.content, 0, target,
                           on_done=lambda: self.content.setMaximumHeight(16777215))
        else:
            animate_height(self.content, self.content.height(), 0,
                           on_done=lambda: self.content.setVisible(False))


class ChatView(QScrollArea):
    """Kart tabanlı ajan konuşması (dikey akış + otomatik kaydırma)."""

    openChangesRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("chatScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._host = QWidget(); self._host.setObjectName("chatHost")
        self._v = QVBoxLayout(self._host)
        self._v.setContentsMargins(10, 10, 10, 10); self._v.setSpacing(9)
        self._v.addStretch(1)
        self.setWidget(self._host)
        self._stages = {}

    # ---- ekleme yardımcıları ----
    def _add(self, w, animate=True):
        self._v.insertWidget(self._v.count() - 1, w)   # stretch'ten önce
        if animate:
            fade_in(w, dur=FAST)
        self._scroll_bottom()

    def _scroll_bottom(self):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))

    def clear(self):
        while self._v.count() > 1:
            it = self._v.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._stages = {}

    def add_user_task(self, text):
        card = QFrame(); card.setObjectName("userCard")
        h = QVBoxLayout(card); h.setContentsMargins(13, 10, 13, 11); h.setSpacing(4)
        lbl = QLabel("SEN"); lbl.setObjectName("userTag")
        body = QLabel(text.strip()); body.setObjectName("userBody"); body.setWordWrap(True)
        h.addWidget(lbl); h.addWidget(body)
        self._add(card)

    def add_info(self, text):
        lbl = QLabel(text); lbl.setObjectName("chatInfo"); lbl.setWordWrap(True)
        self._add(lbl)

    def start_stage(self, stage, provider=""):
        card = StageCard(stage); card.set_model(provider)
        self._stages[stage] = card
        self._add(card)
        return card

    def append_output(self, stage, text):
        c = self._stages.get(stage)
        if c:
            c.append(text); self._scroll_bottom()

    def set_metric(self, stage, model, latency, tokens, cost):
        c = self._stages.get(stage)
        if c:
            c.set_metric(model, latency, tokens, cost)

    def set_verdict(self, verdict, note=""):
        c = self._stages.get("review")
        if c:
            c.set_verdict(verdict, note)
        else:
            card = StageCard("review"); card.set_verdict(verdict, note)
            self._stages["review"] = card; self._add(card)

    def add_summary(self, n_files):
        card = QFrame(); card.setObjectName("summaryCard")
        h = QHBoxLayout(card); h.setContentsMargins(13, 10, 13, 10); h.setSpacing(10)
        ic = QToolButton(); ic.setObjectName("cardIcon"); ic.setEnabled(False)
        ic.setIcon(icon("mdi6.file-check-outline", color=C["green"])); ic.setIconSize(QSize(17, 17))
        lbl = QLabel(f"{n_files} dosya önerisi hazır"); lbl.setObjectName("summaryText")
        btn = QPushButton("  İncele"); btn.setObjectName("ghost")
        btn.setIcon(icon("mdi6.arrow-right", color=C["muted"]))
        btn.clicked.connect(self.openChangesRequested.emit)
        h.addWidget(ic); h.addWidget(lbl); h.addStretch(1); h.addWidget(btn)
        self._add(card)

    def add_error(self, text):
        card = QFrame(); card.setObjectName("errorCard")
        h = QHBoxLayout(card); h.setContentsMargins(13, 10, 13, 10); h.setSpacing(9)
        ic = QToolButton(); ic.setObjectName("cardIcon"); ic.setEnabled(False)
        ic.setIcon(icon("mdi6.alert-circle-outline", color=C["red"])); ic.setIconSize(QSize(17, 17))
        lbl = QLabel(text); lbl.setObjectName("errorText"); lbl.setWordWrap(True)
        h.addWidget(ic); h.addWidget(lbl, 1)
        self._add(card)


def chat_style():
    return f"""
QScrollArea#chatScroll {{ background:{C['panel']}; border:1px solid {C['border']};
  border-radius:{RADIUS['lg']}px; }}
QWidget#chatHost {{ background:transparent; }}
QFrame#chatCard {{ background:#1b1e25; border:1px solid {C['border']};
  border-radius:{RADIUS['md']}px; }}
QLabel#cardTitle {{ color:{C['text']}; font-size:12px; font-weight:700; }}
QLabel#cardBadge {{ color:{C['muted']}; font-size:10px; font-family:'{FONT_MONO}'; }}
QLabel#cardBody {{ color:{C['text2']}; font-size:12px; }}
QLabel#cardFooter {{ color:{C['faint']}; font-size:10px; font-family:'{FONT_MONO}'; }}
QToolButton#cardIcon, QToolButton#cardChev {{ background:transparent; border:none; }}
QFrame#userCard {{ background:{C['accentdim']}; border:1px solid {C['accent']};
  border-radius:{RADIUS['md']}px; }}
QLabel#userTag {{ color:{C['accent']}; font-size:9px; font-weight:800; letter-spacing:1.2px; }}
QLabel#userBody {{ color:{C['text']}; font-size:13px; }}
QLabel#chatInfo {{ color:{C['muted']}; font-size:11px; padding:0 4px; }}
QFrame#summaryCard {{ background:#161f1a; border:1px solid #2e5a3e;
  border-radius:{RADIUS['md']}px; }}
QLabel#summaryText {{ color:{C['text']}; font-size:12px; font-weight:600; }}
QFrame#errorCard {{ background:#2a1416; border:1px solid #5a2e2e;
  border-radius:{RADIUS['md']}px; }}
QLabel#errorText {{ color:#ffb3b8; font-size:12px; }}
"""
