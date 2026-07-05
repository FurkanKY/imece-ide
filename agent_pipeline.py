"""
agent_pipeline.py
-----------------
Multi-agent'a özgü imza öğe: Planner → Coder → Reviewer akışını canlı gösteren
görsel şerit. Aktif ajan nabız animasyonuyla belli olur; her ajanın altında model,
süre ve maliyet görünür.

Kullanım (desktop.py):
    self.pipeline = AgentPipeline()
    self.pipeline.set_agents({"planner":"claude","coder":"deepseek","reviewer":"gemini"})
    self.pipeline.reset()
    self.pipeline.set_running("planner")
    self.pipeline.set_metric("planner", "claude-code", 8.5, 176, 0.029)  # -> done
    self.pipeline.finish()
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QToolButton

from theme import C, icon, grad
from anim import Breathe

ROLE_ICON = {
    "planner": "mdi6.lightbulb-on-outline",
    "coder": "mdi6.code-braces",
    "reviewer": "mdi6.eye-check-outline",
}
ROLE_TR = {"planner": "Planner", "coder": "Coder", "reviewer": "Reviewer"}


class AgentCard(QFrame):
    def __init__(self, role):
        super().__init__()
        self.role = role
        self.setObjectName("agentCard")
        self._state = "idle"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 8, 12, 8)
        lay.setSpacing(11)

        self.ic = QToolButton(); self.ic.setEnabled(False)
        self.ic.setObjectName("agentIcon")
        self.ic.setIcon(icon(ROLE_ICON[role], color=C["muted"]))
        self.ic.setIconSize(_qsize(18))
        lay.addWidget(self.ic)

        col = QVBoxLayout(); col.setSpacing(2)
        self.name = QLabel(ROLE_TR[role]); self.name.setObjectName("agentName")
        self.sub = QLabel("—"); self.sub.setObjectName("agentSub")
        col.addWidget(self.name); col.addWidget(self.sub)
        lay.addLayout(col)
        lay.addStretch(1)

        # sağ: durum/metrik + küçük durum noktası (boş sağ alanı anlamlı doldurur)
        self.status = QLabel(""); self.status.setObjectName("agentStatus")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.dot = QLabel(); self.dot.setFixedSize(7, 7); self._paint_dot(C["faint"])
        lay.addWidget(self.status)
        lay.addWidget(self.dot)

    def _paint_dot(self, color):
        self.dot.setStyleSheet(f"background:{color}; border-radius:3px;")

    def set_model(self, model):
        self.sub.setText(model or "—")

    def set_state(self, state):
        self._state = state
        dotc = {"idle": C["faint"], "running": C["accent"], "done": C["green"], "error": C["red"]}[state]
        self._paint_dot(dotc)
        self.ic.setIcon(icon(ROLE_ICON[self.role], color=dotc))
        border = {"idle": C["border"], "running": C["accent"], "done": C["border"], "error": C["red"]}[state]
        self.setStyleSheet(f"#agentCard {{ border:1px solid {border}; }}")
        self.status.setStyleSheet(f"color:{dotc};")
        if state == "idle":
            self.status.setText("")
        elif state == "running":
            self.status.setText("çalışıyor")
        elif state == "error":
            self.status.setText("hata")

    def set_metric(self, model, latency, tokens, cost):
        self.set_model(model)
        self.status.setText(f"{latency}s · ${cost}")   # sağa hizalı metrik
        self.set_state("done")                          # metin done'da korunur


def _qsize(n):
    from PySide6.QtCore import QSize
    return QSize(n, n)


class AgentPipeline(QFrame):
    def __init__(self, vertical=False):
        super().__init__()
        self.setObjectName("pipeline")
        self.cards = {}
        self._vertical = vertical
        roles = ["planner", "coder", "reviewer"]

        if vertical:
            # Dar sağ panel için dikey timeline: başlık + alt alta kartlar.
            outer = QVBoxLayout(self)
            outer.setContentsMargins(11, 10, 11, 11); outer.setSpacing(8)
            hrow = QHBoxLayout(); hrow.setSpacing(6)
            head = QLabel("EKİP"); head.setObjectName("h")
            self.state = QLabel("Hazır"); self.state.setObjectName("pipeState")
            hrow.addWidget(head); hrow.addStretch(1); hrow.addWidget(self.state)
            outer.addLayout(hrow)
            for role in roles:
                card = AgentCard(role)
                self.cards[role] = card
                outer.addWidget(card)
        else:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(12, 8, 12, 8); lay.setSpacing(8)
            hic = QToolButton(); hic.setEnabled(False); hic.setObjectName("agentIcon")
            hic.setIcon(icon("mdi6.account-group-outline", color=C["faint"])); hic.setIconSize(_qsize(14))
            head = QLabel("EKİP"); head.setObjectName("h")
            lay.addWidget(hic); lay.addWidget(head); lay.addSpacing(6)
            for i, role in enumerate(roles):
                card = AgentCard(role)
                self.cards[role] = card
                lay.addWidget(card)
                if i < len(roles) - 1:
                    arr = QLabel("→"); arr.setObjectName("arrow")
                    lay.addWidget(arr)
            lay.addStretch(1)
            self.state = QLabel("Hazır"); self.state.setObjectName("pipeState")
            lay.addWidget(self.state)

        # aktif kart için akıcı "nefes" nabzı (opacity breathing)
        self._active = None
        self._breathe = None
        self._total_latency = 0.0

    def set_agents(self, routing):
        # Yalnızca boştaki kartların modelini güncelle — koşu bitince (done)
        # kartlardaki metrik metnini (_update_hud→set_agents ile) silmemek için.
        for role, model in routing.items():
            if role in self.cards and self.cards[role]._state == "idle":
                self.cards[role].set_model(model)

    def reset(self):
        self._stop_breathe()
        self._total_latency = 0.0
        for card in self.cards.values():
            card.set_state("idle")
        self._set_state("Hazır", C["faint"])

    def set_running(self, role):
        self._stop_breathe()
        for r, card in self.cards.items():
            if r == role:
                card.set_state("running")
        self._active = role
        if role in self.cards:
            self._breathe = Breathe(self.cards[role])
            self._breathe.start()
        self._set_state(f"{ROLE_TR.get(role, role)} çalışıyor…", C["accent"])

    def set_metric(self, role, model, latency, tokens, cost):
        if self._active == role:
            self._stop_breathe()   # önce efekti kaldır, sonra done stilini bas
        if role in self.cards:
            self.cards[role].set_metric(model, latency, tokens, cost)
        try:
            self._total_latency += float(latency)
        except (TypeError, ValueError):
            pass

    def finish(self):
        self._stop_breathe()
        for card in self.cards.values():
            if card._state == "running":
                card.set_state("done")
        self._set_state(f"✓ Tamamlandı · {self._total_latency:.1f}s", C["green"])

    def error(self, role=None):
        self._stop_breathe()
        if role and role in self.cards:
            self.cards[role].set_state("error")
        self._set_state("✗ Hata", C["red"])

    def _set_state(self, text, color):
        self.state.setText(text)
        self.state.setStyleSheet(f"color:{color};")

    # ---- nefes nabzı ----
    def _stop_breathe(self):
        if self._breathe is not None:
            self._breathe.stop()
            self._breathe = None
        self._active = None


# Bu widget için stil (desktop build_style'ına eklenir). Font yüklendikten sonra
# çağrıldığından fonksiyon — FONT_MONO'yu çağrı anında okur (stale import olmaz).
def pipeline_style():
    from theme import FONT_MONO
    return f"""
QFrame#pipeline {{ background:transparent; border:none; }}
QFrame#agentCard {{ background:{C['card']}; border:1px solid {C['border']};
  border-radius:10px; }}
QToolButton#agentIcon {{ background:transparent; border:none; }}
QLabel#agentName {{ color:{C['text']}; font-size:12px; font-weight:700;
  letter-spacing:0.2px; }}
QLabel#agentSub {{ color:{C['muted']}; font-size:11px; font-family:'{FONT_MONO}'; }}
QLabel#agentStatus {{ color:{C['faint']}; font-size:10px; font-family:'{FONT_MONO}'; }}
QLabel#arrow {{ color:{C['faint']}; font-size:14px; }}
QLabel#pipeState {{ color:{C['faint']}; font-size:11px; font-weight:600;
  font-family:'{FONT_MONO}'; }}
"""
