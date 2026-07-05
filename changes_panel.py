"""
changes_panel.py
----------------
Faz 1.5 · #2 — Dosya bazında kabul/ret + Reviewer verdict rozeti.

Toptan "Uygula" yerine her önerilen dosya ayrı bir onay kutusuyla listelenir;
kullanıcı hangi dosyaların yazılacağını tek tek seçer. Üstte Reviewer'ın kararı
(APPROVED / NEEDS_FIX) renkli bir rozet olarak durur. Bir dosya seçilince diff'i
altta renklendirilmiş gösterilir.

Kullanım (desktop.py):
    self.changes = ChangesPanel()
    self.changes.selectionChanged.connect(self._update_apply_state)
    ...
    self.changes.clear_all()
    self.changes.add_row(path, is_new, diff)      # her 'diff' olayında
    self.changes.set_verdict("APPROVED", note)     # 'verdict' olayında
    self.changes.attach_contents(proposals)        # 'proposal' olayında
    for p in self.changes.checked(): proj.apply(p["path"], p["new"])
"""

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QTextEdit, QSplitter, QToolButton, QPushButton,
)

from theme import C, icon, file_icon
from anim import fade_in, FAST

_PATH_ROLE = Qt.UserRole
_DIFF_ROLE = Qt.UserRole + 1
_NEW_ROLE = Qt.UserRole + 2

# verdict -> (etiket, ikon, renk, arka plan)
_VERDICT = {
    "APPROVED": ("İNCELEME: ONAYLANDI", "mdi6.check-decagram", C["green"], "#16321f"),
    "NEEDS_FIX": ("İNCELEME: DÜZELTME GEREKLİ", "mdi6.alert-decagram", C["amber"], "#3a2e12"),
    "UNKNOWN": ("İNCELEME: KARAR OKUNAMADI", "mdi6.help-circle-outline", C["muted"], C["card"]),
    "PENDING": ("İnceleme bekleniyor…", "mdi6.timer-sand", C["muted"], C["card"]),
}


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace(" ", "&nbsp;").replace("\n", "<br>"))


def render_diff_html(diff: str) -> str:
    """Unified diff'i renkli HTML'e çevir."""
    out = []
    for line in (diff or "").splitlines():
        color = "#cbd5e1"
        if line.startswith("+") and not line.startswith("+++"):
            color = C["green"]
        elif line.startswith("-") and not line.startswith("---"):
            color = C["red"]
        elif line.startswith("@@"):
            color = C["accent"]
        out.append(f'<span style="color:{color}">{_esc(line)}</span>')
    return "<br>".join(out) if out else f'<span style="color:{C["muted"]}">(değişiklik yok)</span>'


class ChangesPanel(QWidget):
    """Önerilen değişikliklerin dosya bazında onay listesi + verdict rozeti + diff."""

    selectionChanged = Signal()  # onay kutuları değişince (Uygula durumunu güncellemek için)
    applyRequested = Signal()     # "Uygula" tıklandı
    rejectRequested = Signal()    # "Vazgeç" tıklandı
    fileActivated = Signal(str)   # bir dosyaya tıklandı → merkez diff editöründe aç

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # --- verdict rozeti + toplu seçim ---
        head = QHBoxLayout(); head.setSpacing(8)
        self.badge_ic = QToolButton(); self.badge_ic.setObjectName("verdictIcon")
        self.badge_ic.setEnabled(False); self.badge_ic.setIconSize(QSize(16, 16))
        self.badge = QLabel(); self.badge.setObjectName("verdictText")
        self.badge_wrap = QWidget(); self.badge_wrap.setObjectName("verdictBadge")
        bl = QHBoxLayout(self.badge_wrap); bl.setContentsMargins(9, 4, 11, 4); bl.setSpacing(6)
        bl.addWidget(self.badge_ic); bl.addWidget(self.badge)
        head.addWidget(self.badge_wrap)
        head.addStretch(1)

        self.all_btn = QToolButton(); self.all_btn.setObjectName("linkBtn")
        self.all_btn.setText("Tümünü seç"); self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn = QToolButton(); self.none_btn.setObjectName("linkBtn")
        self.none_btn.setText("Hiçbiri"); self.none_btn.clicked.connect(lambda: self._set_all(False))
        head.addWidget(self.all_btn); head.addWidget(self.none_btn)
        v.addLayout(head)

        # --- dosya listesi (tıkla → merkez inline diff editörü açılır) ---
        self.list = QListWidget(); self.list.setObjectName("changeList")
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.itemClicked.connect(self._on_click)
        v.addWidget(self.list, 1)

        # --- eylem çubuğu: özet + Vazgeç + Uygula(n) (diff'in yanında) ---
        actions = QHBoxLayout(); actions.setSpacing(8)
        self.summary = QLabel("Öneri bekleniyor."); self.summary.setObjectName("changeSummary")
        self.reject_btn = QPushButton("  Vazgeç"); self.reject_btn.setObjectName("ghost")
        self.reject_btn.setIcon(icon("mdi6.close")); self.reject_btn.setEnabled(False)
        self.reject_btn.clicked.connect(self.rejectRequested.emit)
        self.apply_btn = QPushButton("  Uygula"); self.apply_btn.setObjectName("apply")
        self.apply_btn.setIcon(icon("mdi6.check", color="#06140b")); self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.applyRequested.emit)
        actions.addWidget(self.summary, 1)
        actions.addWidget(self.reject_btn); actions.addWidget(self.apply_btn)
        v.addLayout(actions)

        self._guard = False  # itemChanged sinyal fırtınasını önlemek için
        self.selectionChanged.connect(self._refresh_actions)
        self.set_verdict("PENDING")

    # ---------------- doldurma ----------------
    def clear_all(self):
        self._guard = True
        self.list.clear()
        self._guard = False
        self.set_verdict("PENDING")
        self.summary.setText("Öneri bekleniyor.")
        self._refresh_actions()

    def _refresh_actions(self):
        n = self.list.count()
        c = len(self.checked())
        self.reject_btn.setEnabled(n > 0)
        self.apply_btn.setEnabled(c > 0)
        self.apply_btn.setText(f"  Uygula ({c})" if c else "  Uygula")
        if n:
            self.summary.setText(f"{c}/{n} dosya seçili")

    def set_summary(self, text):
        self.summary.setText(text)

    def add_row(self, path, is_new, diff):
        """Bir 'diff' olayı geldiğinde çağrılır — onay kutulu satır ekler."""
        self._guard = True
        it = QListWidgetItem()
        it.setIcon(file_icon(path))
        label = f"{path}   ·   YENİ" if is_new else path
        it.setText(label)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        it.setData(_PATH_ROLE, path)
        it.setData(_DIFF_ROLE, diff or "")
        it.setData(_NEW_ROLE, bool(is_new))
        self.list.addItem(it)
        self._guard = False
        if self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        if self.list.count() == 1:
            fade_in(self.list)   # ilk satır gelince liste akıcı belirsin
        self.selectionChanged.emit()

    def attach_contents(self, proposals):
        """'proposal' olayında tam dosya içeriklerini (apply için) satırlara bağla."""
        by_path = {p["path"]: p for p in proposals}
        seen = set()
        for i in range(self.list.count()):
            it = self.list.item(i)
            p = by_path.get(it.data(_PATH_ROLE))
            if p:
                it.setData(_DIFF_ROLE, p.get("diff") or it.data(_DIFF_ROLE))
                it.setData(Qt.UserRole + 3, p.get("new"))
                seen.add(it.data(_PATH_ROLE))
        # Listede olmayan (diff olayı kaçmış) öneri varsa ekle.
        for p in proposals:
            if p["path"] not in seen:
                self.add_row(p["path"], p.get("is_new"), p.get("diff"))
                self.list.item(self.list.count() - 1).setData(Qt.UserRole + 3, p.get("new"))
        self.selectionChanged.emit()

    def set_verdict(self, verdict, note=""):
        label, ic, color, bg = _VERDICT.get(verdict, _VERDICT["UNKNOWN"])
        # Not'u rozete GÖMME (dar satırda kırpılır) — tooltip'e al; not zaten chat
        # kartındaki verdict rozetinde tam görünür.
        self.badge_wrap.setToolTip(note or "")
        self.badge_ic.setIcon(icon(ic, color=color))
        self.badge.setText(label)
        self.badge.setStyleSheet(f"color:{color}; font-size:11px; font-weight:700;")
        self.badge_wrap.setStyleSheet(
            f"#verdictBadge {{ background:{bg}; border:1px solid {color}; border-radius:8px; }}"
        )
        if verdict not in ("PENDING", None):
            fade_in(self.badge_wrap)   # inceleme kararı akıcı belirsin

    # ---------------- olaylar ----------------
    def _on_item_changed(self, _item):
        if not self._guard:
            self.selectionChanged.emit()

    def _on_click(self, item):
        """Satıra tıklandı → merkez diff editöründe tam boy aç (checkbox tıklaması hariç)."""
        if item is not None:
            self.fileActivated.emit(item.data(_PATH_ROLE))

    def _set_all(self, checked):
        self._guard = True
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._guard = False
        self.selectionChanged.emit()

    # ---------------- sorgu ----------------
    def checked(self):
        """Onaylı ve içeriği hazır öneriler: [{'path','new'}]."""
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                new = it.data(Qt.UserRole + 3)
                if new is not None:
                    out.append({"path": it.data(_PATH_ROLE), "new": new})
        return out

    def any_checked(self):
        for i in range(self.list.count()):
            if self.list.item(i).checkState() == Qt.Checked:
                return True
        return False

    def count(self):
        return self.list.count()


CHANGES_STYLE = f"""
QToolButton#verdictIcon {{ background:transparent; border:none; }}
QToolButton#linkBtn {{ background:transparent; border:none; color:{C['muted']};
  font-size:11px; padding:2px 6px; }}
QToolButton#linkBtn:hover {{ color:{C['accent']}; }}
QLabel#changeSummary {{ color:{C['muted']}; font-size:12px; }}
QListWidget#changeList {{ background:{C['panel']}; border:1px solid {C['border']};
  border-radius:10px; outline:0; padding:4px; }}
QListWidget#changeList::item {{ padding:5px 6px; border-radius:6px; color:{C['text']}; }}
QListWidget#changeList::item:hover {{ background:{C['card']}; }}
QListWidget#changeList::item:selected {{ background:#34343c; color:#fff; }}
"""
