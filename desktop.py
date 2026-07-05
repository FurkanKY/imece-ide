"""
desktop.py
----------
Multi-agent içeren mini-IDE (PySide6 / Qt). Native Windows penceresi.
Modern IDE (VS Code / Cursor) benzeri koyu arayüz + kendi dokunuşlarımız:
  - Sol aktivite çubuğu (ikon rayı)
  - İkonlu paneller, butonlar, sekmeler (qtawesome / Material Design Icons)
  - Alt durum çubuğunda CANLI ajan + maliyet/token HUD'u (çok-modelli şeffaflık —
    Cursor/VSCode'da olmayan kendi dokunuşumuz)

Düzen:
  [aktivite | gezgin | (Monaco editör / Sohbet·Diff sekmeleri)]
  [ composer: ajanlar + görev kutusu + Çalıştır/Uygula/Vazgeç ]
  [ durum çubuğu: proje · ajan modelleri · Σ token · $maliyet ]

Çalıştır:  python desktop.py
"""

import os
import sys
import subprocess

from dotenv import load_dotenv
from PySide6.QtCore import Qt, QThread, Signal, QDir, QSize, QRect, QTimer, QEvent
from PySide6.QtGui import QFont, QIcon, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QPlainTextEdit, QTextEdit, QComboBox, QFileDialog, QSplitter,
    QTreeView, QTabWidget, QFileSystemModel, QFrame, QToolButton,
    QFileIconProvider, QStackedWidget, QMenu, QInputDialog, QMessageBox,
)
from PySide6.QtCore import QFileInfo

import theme
from theme import C, icon, file_icon, icon_png, grad, load_fonts, RADIUS
from agent_pipeline import AgentPipeline, pipeline_style
from changes_panel import ChangesPanel, CHANGES_STYLE
from chat_view import ChatView, chat_style
from command_palette import CommandPalette, palette_style
from bottom_panel import BottomPanel, bottom_panel_style
from settings_panel import SettingsDialog
from history import HistoryStore
from history_panel import HistoryDialog
import anim
from anim import fade_in, animate_geometry, count_to, FAST
from chrome import TitleBar, make_frameless
import ui_prefs
from project import Project
from project_runner import run_project_task
from editor_panel import EditorPanel

load_dotenv()

PROVIDERS = ["claude", "deepseek", "gemini"]

# Orkestratör aşama adı -> ajan rolü (pipeline paneli için)
STAGE_ROLE = {"plan": "planner", "code": "coder", "review": "reviewer"}


class IconProvider(QFileIconProvider):
    """Dosya gezgininde uzantıya göre renkli ikonlar."""
    def icon(self, arg):
        if isinstance(arg, QFileInfo):
            return file_icon("", is_dir=True) if arg.isDir() else file_icon(arg.fileName())
        return super().icon(arg)


def build_style():
    """Tam stylesheet — QApplication + load_fonts() sonrası çağrılır (combo/checkbox
    ikonları PNG'ye render edilir; fontlar çağrı anında okunur)."""
    from theme import FONT_UI, FONT_MONO   # load_fonts() sonrası güncel değerler
    caret = icon_png("mdi6.chevron-down", C["muted"], 14)
    chk_off = icon_png("mdi6.checkbox-blank-outline", C["faint"], 16)
    chk_on = icon_png("mdi6.checkbox-marked", C["accent"], 16)

    s = f"""
* {{ font-family:'{FONT_UI}', system-ui, sans-serif; }}
/* KÖK KURAL: zemini YALNIZ pencereye ver. Genel QWidget'a background verMEmek,
   iç konteyner/etiket/checkbox'ların ŞEFFAF kalıp ebeveynin boyadığı yüzeyi
   göstermesini sağlar → "yazı/öğe arkası siyah kutu" sınıfı hatası tümden biter.
   Yüzey isteyen paneller/kartlar kendi #id kurallarıyla background alır. */
QMainWindow {{ background:{C['bg']}; }}
QWidget {{ color:{C['text']}; font-size:13px; }}
QLabel, QCheckBox, QRadioButton {{ background:transparent; }}
QDialog {{ background:transparent; }}   /* overlay'ler kendi kartıyla yüzer (palette/ayarlar) */
QToolTip {{ background:{C['card2']}; color:{C['text']}; border:1px solid {C['border2']};
  border-radius:7px; padding:5px 9px; font-size:12px; }}

QLabel#h    {{ color:{C['faint']}; font-size:10px; font-weight:800; letter-spacing:1.4px; }}
QLabel#path {{ color:{C['muted']}; font-size:12px; }}
QLabel#brand{{ color:{C['text']}; font-size:14px; font-weight:700; letter-spacing:0.3px; }}
QLabel#seg  {{ color:{C['muted']}; font-size:12px; }}
QLabel#segAccent {{ color:{C['text2']}; font-size:12px; font-weight:700;
  font-family:'{FONT_MONO}','{FONT_UI}'; letter-spacing:0.2px; }}
QLabel#hint {{ color:{C['faint']}; font-size:12px; }}

/* Özel başlık çubuğu (frameless chrome) */
QFrame#titlebar {{ background:{grad(C['panel2'], C['activity'])};
  border-bottom:1px solid {C['border']}; }}
QToolButton#winbtn, QToolButton#winclose {{ background:transparent; border:none;
  border-radius:7px; padding:6px; }}
QToolButton#winbtn:hover {{ background:{C['card2']}; }}
QToolButton#winclose:hover {{ background:#e5484d; }}

/* Üst komut çubuğu */
QFrame#topbar {{ background:{grad(C['panel'], C['activity'])};
  border-bottom:1px solid {C['border']}; }}
QLabel#projChip {{ color:{C['text2']}; font-size:12px; font-weight:600;
  background:{grad(C['card2'], C['card'])}; border:1px solid {C['border2']};
  border-radius:9px; padding:6px 12px; }}

/* Aktivite çubuğu */
QFrame#activity {{ background:{C['activity']}; border-right:1px solid {C['border']}; }}
QToolButton#act {{ background:transparent; border:none; border-radius:11px; padding:9px; }}
QToolButton#act:hover {{ background:{C['card']}; }}
QToolButton#act:checked {{ background:{grad(C['card2'], C['card'])}; }}
QToolButton#act:pressed {{ background:{C['border']}; }}
QFrame#actIndicator {{ background:{C['accent']}; border-radius:2px; }}

/* Breadcrumb (açık dosya yolu) */
QLabel#breadcrumb {{ background:{C['panel']}; color:{C['muted']}; font-size:12px;
  padding:7px 13px; border:1px solid {C['border']};
  border-top-left-radius:12px; border-top-right-radius:12px; border-bottom:none; }}

/* Yan panel (gezgin) */
QFrame#side {{ background:{C['side']}; border-right:1px solid {C['border']}; }}
QTreeView {{ background:transparent; border:none; outline:0; font-size:13px; }}
QTreeView::item {{ padding:5px 4px; border-radius:7px; color:{C['text2']}; }}
QTreeView::item:hover {{ background:{C['card']}; color:{C['text']}; }}
QTreeView::item:selected {{ background:{C['accentdim']}; color:{C['text']}; }}
QTreeView::branch {{ background:transparent; }}

/* Paneller (log) */
QTextEdit {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:14px;
  padding:14px; selection-background-color:{C['accentdim']}; }}

/* Composer girişi */
QPlainTextEdit#composerInput {{ background:{C['field']}; border:1px solid {C['border2']};
  border-radius:12px; padding:11px 14px; color:{C['text']}; font-size:14px;
  selection-background-color:{C['accentdim']}; }}
QPlainTextEdit#composerInput:focus {{ border-color:{C['accent']}; }}

/* Sekmeler */
QTabWidget::pane {{ border:1px solid {C['border']}; border-radius:14px; top:-1px;
  background:{C['panel']}; }}
QTabBar {{ background:transparent; }}
QTabBar::tab {{ background:transparent; color:{C['muted']}; padding:9px 17px; margin-right:4px;
  border:none; font-size:12px; font-weight:600; }}
QTabBar::tab:selected {{ color:{C['text']}; background:{C['panel']};
  border-top:2px solid {C['accent']}; border-top-left-radius:9px; border-top-right-radius:9px; }}
QTabBar::tab:hover:!selected {{ color:{C['text2']}; }}

/* Açılır menü */
QComboBox {{ background:{grad(C['card2'], C['card'])}; border:1px solid {C['border2']};
  border-radius:9px; padding:6px 4px 6px 9px; min-width:64px; color:{C['text']};
  font-size:11px; font-weight:600; }}
QComboBox:hover {{ border-color:{C['accent']}; }}
QComboBox:focus {{ border-color:{C['accent']}; }}
QComboBox::drop-down {{ subcontrol-origin:padding; subcontrol-position:center right;
  border:none; width:16px; }}
QComboBox::down-arrow {{ image:url({caret}); width:11px; height:11px; }}
QComboBox QAbstractItemView {{ background:{C['card2']}; border:1px solid {C['border2']};
  border-radius:9px; selection-background-color:{C['accentdim']}; selection-color:{C['text']};
  color:{C['text2']}; outline:0; padding:6px; }}
QComboBox QAbstractItemView::item {{ padding:6px 9px; border-radius:7px; min-height:22px; }}

/* Butonlar */
QPushButton {{ background:{grad(C['card2'], C['card'])}; color:{C['text']};
  border:1px solid {C['border2']}; border-radius:11px; padding:9px 16px; font-weight:600; }}
QPushButton:hover {{ background:{C['card2']}; border-color:#3a3e49; }}
QPushButton:pressed {{ background:{C['card']}; }}
QPushButton#primary {{ background:{grad(C['accent'], C['accent2'])}; color:#06122b;
  border:1px solid {C['accent']}; font-weight:800; }}
QPushButton#primary:hover {{ background:{grad('#79adff', C['accent'])}; }}
QPushButton#primary:pressed {{ background:{C['accent2']}; }}
QPushButton#primary:disabled {{ background:{C['accentdim']}; color:#6d7f9e; border-color:{C['accentdim']}; }}
QPushButton#apply {{ background:{grad('#54dc93', C['green'])}; color:#04180e;
  border:1px solid {C['green']}; font-weight:800; }}
QPushButton#apply:hover {{ background:{grad('#63e6a0', '#43c583')}; }}
QPushButton#apply:pressed {{ background:#39bd78; }}
QPushButton#ghost {{ background:transparent; border:1px solid {C['border2']}; color:{C['muted']}; }}
QPushButton#ghost:hover {{ background:{C['card']}; color:{C['text']}; }}
QPushButton#ghost:pressed {{ background:{C['bg']}; }}
QPushButton:disabled {{ background:{C['panel']}; color:{C['faint']}; border-color:{C['border']}; }}

/* Sağ AI paneli (Cursor benzeri) */
QFrame#aipanel {{ background:{C['side']}; border-left:1px solid {C['border']}; }}
QLabel#panelTitle {{ color:{C['text']}; font-size:12px; font-weight:800; letter-spacing:0.9px; }}
QToolButton#roleIc {{ background:transparent; border:none; }}

/* Kartlar */
QFrame#composer {{ background:{grad(C['panel2'], C['composer'])};
  border:1px solid {C['border']}; border-radius:14px; }}
QFrame#agentbar {{ background:transparent; border:none; }}
QFrame#statusbar {{ background:{C['status']}; border-top:1px solid {C['border']}; }}
QLabel#empty {{ color:{C['faint']}; font-size:13px; line-height:150%; }}
QLabel#emptyTitle {{ color:{C['text2']}; font-size:15px; font-weight:700; }}
QToolButton#emptyIcon {{ background:transparent; border:none; }}
/* Karşılama ekranı */
QWidget#welcome {{ background:{C['bg']}; }}
QLabel#welcomeTitle {{ color:{C['text']}; font-size:22px; font-weight:800; letter-spacing:0.2px; }}
QLabel#welcomeKbd {{ color:{C['faint']}; font-size:11px; font-family:'{FONT_MONO}'; }}
QPushButton#welcomeAction {{ background:{grad(C['card2'], C['card'])}; color:{C['text2']};
  border:1px solid {C['border2']}; border-radius:{RADIUS['sm']}px; padding:9px 16px; font-weight:600; }}
QPushButton#welcomeAction:hover {{ border-color:{C['accent']}; color:{C['text']}; }}

/* Değişiklik listesi onay kutuları (özel ikon) */
QListWidget#changeList::indicator {{ width:16px; height:16px; margin-right:3px; }}
QListWidget#changeList::indicator:unchecked {{ image:url({chk_off}); }}
QListWidget#changeList::indicator:checked {{ image:url({chk_on}); }}

/* Scrollbar */
QScrollBar:vertical {{ background:transparent; width:12px; margin:3px; }}
QScrollBar::handle:vertical {{ background:{C['border2']}; border-radius:5px; min-height:32px; }}
QScrollBar::handle:vertical:hover {{ background:#484c58; }}
QScrollBar:horizontal {{ background:transparent; height:12px; margin:3px; }}
QScrollBar::handle:horizontal {{ background:{C['border2']}; border-radius:5px; min-width:32px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

QSplitter::handle {{ background:transparent; }}
QSplitter::handle:horizontal {{ width:8px; }}
QSplitter::handle:vertical {{ height:8px; }}
"""
    return s + pipeline_style() + CHANGES_STYLE + chat_style() + palette_style() + bottom_panel_style()


class Worker(QThread):
    event = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, root, task, routing):
        super().__init__()
        self.root, self.task, self.routing = root, task, routing
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        gen = run_project_task(self.root, self.task, self.routing)
        try:
            for ev in gen:
                if self._cancel:
                    gen.close()
                    self.cancelled.emit()
                    return
                self.event.emit(ev)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multi-Agent — Mini IDE")
        self.resize(1320, 880)
        self.setMinimumSize(980, 640)
        self.project_root = None
        self.proposals = []
        self.worker = None
        self.combos = {}
        self.last_totals = None
        self._running = False
        self._hud_tokens = 0.0
        self._hud_cost = 0.0
        self._prefs = ui_prefs.load()             # kalıcı arayüz tercihleri
        self.enter_to_send = self._prefs["enter_to_send"]
        # frameless: kendi başlık çubuğumuz (native snap/resize korunur)
        self._resizer = make_frameless(self)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # üst komut çubuğu
        outer.addWidget(self._build_topbar())

        # üst içerik: [aktivite | gezgin | editör | AI paneli] (Cursor benzeri sağ panel)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_activity())
        self._split = QSplitter(Qt.Horizontal)
        self.side_panel = self._build_side()
        self.ai_panel = self._build_aipanel()
        self._split.addWidget(self.side_panel)
        self._split.addWidget(self._build_workspace())
        self._split.addWidget(self.ai_panel)
        self._split.setStretchFactor(0, 0)   # gezgin sabit
        self._split.setStretchFactor(1, 1)   # editör büyür
        self._split.setStretchFactor(2, 0)   # AI paneli sabit
        self._split.setSizes([230, 720, 430])
        body.addWidget(self._split, 1)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_statusbar())

        self._update_hud()

        # command palette + ayarlar + klavye kısayolları
        self.palette = CommandPalette(self)
        self.settings = SettingsDialog(self)
        self.settings.changed.connect(self._apply_prefs)
        self._history = None            # proje seçilince kurulur
        self._last_task = ""
        self.history_dialog = HistoryDialog(self)
        self.history_dialog.picked.connect(self._restore_task)
        self.task_edit.installEventFilter(self)   # composer Enter davranışı
        self._setup_shortcuts()

    # ---------------- klavye & palette ----------------
    def _setup_shortcuts(self):
        def sc(seq, fn):
            s = QShortcut(QKeySequence(seq), self); s.activated.connect(fn); return s
        sc("Ctrl+K", self._open_command_palette)
        sc("Ctrl+P", self._open_file_palette)
        sc("Ctrl+Return", self.start_run)
        sc("Ctrl+Enter", self.start_run)
        sc("Ctrl+S", self.save_current)
        sc("Ctrl+B", lambda: self._toggle_panel(self.side_panel))
        sc("Ctrl+J", lambda: self._toggle_panel(self.ai_panel))
        sc("Ctrl+Shift+Return", self._apply_shortcut)
        sc("Ctrl+L", self._clear_chat)
        sc("Ctrl+`", self._toggle_bottom)
        sc("Ctrl+Shift+`", self._new_terminal)

    def _new_terminal(self):
        self._toggle_bottom(show=True)
        self.bottom.add_terminal()

    def _commands(self):
        """Command palette komut listesi: (başlık, altbaşlık, ikon, callback)."""
        return [
            ("Çalıştır", "Görevi ajanlara gönder", "mdi6.play", self.start_run),
            ("Değişiklikleri uygula", "Seçili dosyaları yaz", "mdi6.check", self._apply_shortcut),
            ("Değişiklikleri reddet", "", "mdi6.close", self.reject_changes),
            ("Dosyaya git…", "Ctrl+P", "mdi6.file-search-outline", self._open_file_palette),
            ("Klasör aç", "Proje seç", "mdi6.folder-open-outline", self.pick_project),
            ("Kaydet", "Aktif dosya", "mdi6.content-save-outline", self.save_current),
            ("Sohbeti temizle", "", "mdi6.broom", self._clear_chat),
            ("Terminal aç/kapat", "Ctrl+`", "mdi6.console", self._toggle_bottom),
            ("Yeni terminal", "Ctrl+Shift+`", "mdi6.console-line", self._new_terminal),
            ("Ayarlar", "Accent · yoğunluk · Enter · animasyon", "mdi6.cog-outline", self._open_settings),
            ("Gezgini aç/kapat", "Ctrl+B", "mdi6.file-multiple-outline",
             lambda: self._toggle_panel(self.side_panel)),
            ("AI panelini aç/kapat", "Ctrl+J", "mdi6.robot-outline",
             lambda: self._toggle_panel(self.ai_panel)),
        ]

    def _open_command_palette(self):
        self.palette.open_commands(self._commands())

    def _open_file_palette(self):
        if not self.project_root:
            self.hint.setText("Önce bir proje klasörü seç."); return
        try:
            files = Project(self.project_root).list_files()
        except Exception:
            files = []
        self.palette.open_files(files, on_pick=self._open_palette_file)

    def _open_palette_file(self, rel):
        self._open_in_editor(rel)

    def _toggle_panel(self, panel):
        panel.setVisible(panel.isHidden())

    def _apply_shortcut(self):
        if self.proposals:
            self.apply_changes()

    # ---------------- ayarlar (Faz 5) ----------------
    def _open_settings(self):
        self.settings.open_with(self._prefs)

    def _apply_prefs(self, prefs):
        """Ayar değişince: temayı canlı uygula + kalıcılaştır."""
        self._prefs = prefs
        theme.set_accent(prefs["accent"])
        theme.set_density(prefs["density"])
        anim.set_enabled(prefs["animations"])
        self.enter_to_send = prefs["enter_to_send"]
        QApplication.instance().setStyleSheet(build_style())
        ui_prefs.save(prefs)

    def eventFilter(self, obj, ev):
        # composer: (tercihe göre) Enter gönder, Shift+Enter yeni satır
        if obj is getattr(self, "task_edit", None) and ev.type() == QEvent.Type.KeyPress:
            if self.enter_to_send and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
               not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.start_run()
                return True
        return super().eventFilter(obj, ev)

    # ---------------- yapı parçaları ----------------
    def _build_topbar(self):
        bar = TitleBar(self, height=46)   # sürüklenebilir + çift tık maximize
        h = QHBoxLayout(bar); h.setContentsMargins(13, 0, 8, 0); h.setSpacing(10)

        logo = QToolButton(); logo.setObjectName("act"); logo.setEnabled(False)
        logo.setIcon(icon("mdi6.hexagon-multiple", color=C["accent"])); logo.setIconSize(QSize(19, 19))
        brand = QLabel("Multi-Agent"); brand.setObjectName("brand")
        h.addWidget(logo); h.addWidget(brand)
        h.addSpacing(8)
        sepv = QFrame(); sepv.setFixedSize(1, 18); sepv.setStyleSheet(f"background:{C['border2']};")
        h.addWidget(sepv); h.addSpacing(8)

        self.proj_chip = QLabel("Proje seçilmedi"); self.proj_chip.setObjectName("projChip")
        h.addWidget(self.proj_chip)

        h.addStretch(1)

        openb = QPushButton("  Klasör Aç"); openb.setObjectName("ghost")
        openb.setIcon(icon("mdi6.folder-open-outline")); openb.clicked.connect(self.pick_project)
        self.save_btn = QPushButton("  Kaydet"); self.save_btn.setObjectName("ghost")
        self.save_btn.setIcon(icon("mdi6.content-save-outline")); self.save_btn.clicked.connect(self.save_current)
        h.addWidget(openb); h.addWidget(self.save_btn)

        # pencere kontrolleri (min / max / kapat)
        h.addSpacing(10)
        def winbtn(name, tip, slot, close=False):
            b = QToolButton(); b.setObjectName("winclose" if close else "winbtn")
            b.setIcon(icon(name, color=C["muted"])); b.setIconSize(QSize(15, 15))
            b.setToolTip(tip); b.clicked.connect(slot)
            return b
        self._btn_max = winbtn("mdi6.window-maximize", "Büyüt", self._toggle_max)
        h.addWidget(winbtn("mdi6.window-minimize", "Küçült", self.showMinimized))
        h.addWidget(self._btn_max)
        h.addWidget(winbtn("mdi6.window-close", "Kapat", self.close, close=True))
        return bar

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
            self._btn_max.setIcon(icon("mdi6.window-maximize", color=C["muted"]))
        else:
            self.showMaximized()
            self._btn_max.setIcon(icon("mdi6.window-restore", color=C["muted"]))

    def _build_activity(self):
        bar = QFrame(); bar.setObjectName("activity"); bar.setFixedWidth(52)
        v = QVBoxLayout(bar); v.setContentsMargins(6, 10, 6, 10); v.setSpacing(6)

        def act(name, tip, slot, checked=False):
            b = QToolButton(); b.setObjectName("act"); b.setCheckable(True)
            b.setChecked(checked); b.setIcon(icon(name, active=checked))
            b.setIconSize(QSize(22, 22)); b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self.btn_explorer = act("mdi6.file-multiple-outline", "Gezgin", lambda: self._focus_view("explorer"), True)
        self.btn_chat = act("mdi6.robot-outline", "Ajan akışı", lambda: self._focus_view("chat"))
        self.btn_diff = act("mdi6.source-branch", "Değişiklikler", lambda: self._focus_view("diff"))
        for b in (self.btn_explorer, self.btn_chat, self.btn_diff):
            v.addWidget(b)
        v.addStretch(1)
        cog = QToolButton(); cog.setObjectName("act"); cog.setIcon(icon("mdi6.cog-outline"))
        cog.setIconSize(QSize(22, 22)); cog.setToolTip("Ayarlar")
        cog.clicked.connect(self._open_settings)
        v.addWidget(cog)

        # seçili görünüme akıcı kayan aktif göstergesi (Cursor imzası)
        self._act_indicator = QFrame(bar); self._act_indicator.setObjectName("actIndicator")
        self._act_indicator.setFixedWidth(3)
        self._act_indicator.raise_()
        # ilk konum layout oturduktan sonra (geometri hazır olunca)
        QTimer.singleShot(0, lambda: self._move_indicator(self.btn_explorer, animate=False))
        return bar

    def showEvent(self, e):
        super().showEvent(e)
        # pencere gösterilince gösterge geometrisi hazır — seçili butona hizala
        checked = self.btn_explorer
        for b in (self.btn_explorer, self.btn_chat, self.btn_diff):
            if b.isChecked():
                checked = b
        QTimer.singleShot(0, lambda: self._move_indicator(checked, animate=False))

    def _move_indicator(self, button, animate=True):
        if not hasattr(self, "_act_indicator"):
            return
        g = button.geometry()
        h = 22
        target = QRect(0, g.y() + (g.height() - h) // 2, 3, h)
        if animate and self._act_indicator.height() > 0:
            animate_geometry(self._act_indicator, target, dur=FAST)
        else:
            self._act_indicator.setGeometry(target)

    def _build_side(self):
        side = QFrame(); side.setObjectName("side")
        v = QVBoxLayout(side); v.setContentsMargins(10, 10, 8, 10); v.setSpacing(8)

        header = QHBoxLayout(); header.setSpacing(1)
        lbl = QLabel("GEZGİN"); lbl.setObjectName("h")
        header.addWidget(lbl); header.addStretch(1)

        def hbtn(ic, tip, slot):
            b = QToolButton(); b.setObjectName("act"); b.setIcon(icon(ic))
            b.setIconSize(QSize(15, 15)); b.setToolTip(tip); b.clicked.connect(slot)
            header.addWidget(b); return b

        hbtn("mdi6.file-plus-outline", "Yeni dosya", lambda: self._new_file())
        hbtn("mdi6.folder-plus-outline", "Yeni klasör", lambda: self._new_folder())
        hbtn("mdi6.refresh", "Yenile", self._refresh_tree)
        hbtn("mdi6.collapse-all-outline", "Tümünü daralt", lambda: self.tree.collapseAll())
        hbtn("mdi6.folder-open-outline", "Proje klasörü seç", self.pick_project)
        v.addLayout(header)

        self.fs_model = QFileSystemModel()
        self.fs_model.setIconProvider(IconProvider())      # dosya-tipi ikonları
        self.fs_model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        for c in (1, 2, 3):
            self.tree.hideColumn(c)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(14)
        self.tree.doubleClicked.connect(self._open_from_tree)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        v.addWidget(self.tree, 1)
        return side

    # ---------------- gezgin dosya işlemleri (T1.1) ----------------
    def _tree_rel(self, index):
        """Ağaç indeksinden (rel, is_dir) döndür; geçersizse (None, True)."""
        if not index.isValid() or not self.project_root:
            return None, True
        path = self.fs_model.filePath(index)
        rel = os.path.relpath(path, self.project_root).replace("\\", "/")
        return rel, os.path.isdir(path)

    def _parent_dir(self, rel, is_dir):
        """Yeni öğenin oluşturulacağı klasör (rel klasörse kendisi, dosyaysa ebeveyni)."""
        if rel is None:
            return ""
        return rel if is_dir else os.path.dirname(rel)

    def _tree_menu(self, pos):
        if not self.project_root:
            return
        rel, is_dir = self._tree_rel(self.tree.indexAt(pos))
        m = QMenu(self)
        parent = self._parent_dir(rel, is_dir)
        m.addAction(icon("mdi6.file-plus-outline"), "Yeni Dosya", lambda: self._new_file(parent))
        m.addAction(icon("mdi6.folder-plus-outline"), "Yeni Klasör", lambda: self._new_folder(parent))
        if rel is not None:
            m.addSeparator()
            m.addAction(icon("mdi6.rename-box"), "Yeniden Adlandır", lambda: self._rename(rel, is_dir))
            m.addAction(icon("mdi6.delete-outline"), "Sil", lambda: self._delete(rel, is_dir))
            m.addSeparator()
            m.addAction(icon("mdi6.content-copy"), "Yolu Kopyala", lambda: self._copy_path(rel))
            m.addAction(icon("mdi6.folder-eye-outline"), "Sistemde Göster", lambda: self._reveal(rel))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _refresh_tree(self):
        if self.project_root:
            self.tree.setRootIndex(self.fs_model.setRootPath(self.project_root))

    def _reveal_in_tree(self, rel):
        idx = self.fs_model.index(os.path.join(self.project_root, rel))
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)
            self.tree.expand(idx.parent())

    def _new_file(self, parent=""):
        if not self.project_root:
            self.hint.setText("Önce bir proje aç."); return
        name, ok = QInputDialog.getText(self, "Yeni Dosya", "Dosya adı:")
        if not ok or not name.strip():
            return
        rel = f"{parent}/{name.strip()}".lstrip("/") if parent else name.strip()
        try:
            r = Project(self.project_root).create_file(rel)
            self._reveal_in_tree(r); self._open_in_editor(r)
        except Exception as e:
            QMessageBox.warning(self, "Yeni Dosya", str(e))

    def _new_folder(self, parent=""):
        if not self.project_root:
            self.hint.setText("Önce bir proje aç."); return
        name, ok = QInputDialog.getText(self, "Yeni Klasör", "Klasör adı:")
        if not ok or not name.strip():
            return
        rel = f"{parent}/{name.strip()}".lstrip("/") if parent else name.strip()
        try:
            r = Project(self.project_root).create_folder(rel)
            self._reveal_in_tree(r)
        except Exception as e:
            QMessageBox.warning(self, "Yeni Klasör", str(e))

    def _rename(self, rel, is_dir):
        old = os.path.basename(rel)
        name, ok = QInputDialog.getText(self, "Yeniden Adlandır", "Yeni ad:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        try:
            new_rel = Project(self.project_root).rename(rel, name.strip())
            self.editor.close_file(rel)                  # açık sekmeyi kapat
            if not is_dir:
                self._open_in_editor(new_rel)            # yeni adla yeniden aç
            self._reveal_in_tree(new_rel)
        except Exception as e:
            QMessageBox.warning(self, "Yeniden Adlandır", str(e))

    def _delete(self, rel, is_dir):
        kind = "klasörünü" if is_dir else "dosyasını"
        r = QMessageBox.question(
            self, "Sil", f"'{rel}' {kind} kalıcı olarak silmek istediğine emin misin?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            Project(self.project_root).delete(rel)
            self.editor.close_file(rel)
            self.hint.setText(f"Silindi: {rel}")
        except Exception as e:
            QMessageBox.warning(self, "Sil", str(e))

    def _copy_path(self, rel):
        abspath = os.path.join(self.project_root, rel)
        QApplication.clipboard().setText(abspath)
        self.hint.setText("Yol kopyalandı.")

    def _reveal(self, rel):
        abspath = os.path.abspath(os.path.join(self.project_root, rel))
        try:
            if sys.platform == "win32":
                subprocess.run(["explorer", "/select,", abspath])
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", abspath])
            else:
                subprocess.run(["xdg-open", os.path.dirname(abspath)])
        except Exception as e:
            self.hint.setText(f"Açılamadı: {e}")

    def _build_workspace(self):
        """Merkez: breadcrumb + Monaco editör (tam boy) + entegre terminal (alt panel)."""
        wrap = QWidget()
        ev = QVBoxLayout(wrap); ev.setContentsMargins(8, 8, 4, 8); ev.setSpacing(0)
        self.breadcrumb = QLabel(""); self.breadcrumb.setObjectName("breadcrumb")
        self.breadcrumb.setVisible(False)
        ev.addWidget(self.breadcrumb)

        self.editor = EditorPanel(self.project_root)
        self.editor.saved.connect(self._on_editor_saved)
        # Editör ↔ karşılama ekranı (dosya yokken dev boş alan yerine tasarlı boş durum)
        self.editor_stack = QStackedWidget()
        self.welcome = self._build_welcome()
        self.editor_stack.addWidget(self.welcome)   # 0: karşılama
        self.editor_stack.addWidget(self.editor)    # 1: Monaco

        # editör ↑ / terminal ↓ dikey splitter (alt panel Ctrl+` ile açılır)
        self._center_split = QSplitter(Qt.Vertical)
        self._center_split.addWidget(self.editor_stack)
        self.bottom = BottomPanel()
        self.bottom.collapseRequested.connect(lambda: self._toggle_bottom(show=False))
        self.bottom.setVisible(False)
        self._center_split.addWidget(self.bottom)
        self._center_split.setStretchFactor(0, 1)
        self._center_split.setStretchFactor(1, 0)
        self._center_split.setSizes([640, 220])
        ev.addWidget(self._center_split, 1)
        return wrap

    def _toggle_bottom(self, show=None):
        """Alt terminal panelini aç/kapat (Ctrl+`)."""
        show = (not self.bottom.isVisible()) if show is None else show
        self.bottom.setVisible(show)
        if show:
            if self.project_root:
                self.bottom.set_root(self.project_root)
            self.bottom.focus_current()

    def _build_welcome(self):
        """Dosya açık değilken gösterilen tasarlanmış karşılama ekranı."""
        w = QWidget(); w.setObjectName("welcome")
        outer = QVBoxLayout(w); outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)
        col = QVBoxLayout(); col.setSpacing(12)

        mark = QToolButton(); mark.setObjectName("emptyIcon"); mark.setEnabled(False)
        mark.setIcon(icon("mdi6.robot-happy-outline", color=C["accent"])); mark.setIconSize(QSize(54, 54))
        mrow = QHBoxLayout(); mrow.addStretch(1); mrow.addWidget(mark); mrow.addStretch(1)
        col.addLayout(mrow)

        title = QLabel("Multi-Agent IDE"); title.setObjectName("welcomeTitle"); title.setAlignment(Qt.AlignCenter)
        sub = QLabel("Bir proje aç, dosya seç ya da ajanlara bir görev ver.")
        sub.setObjectName("empty"); sub.setAlignment(Qt.AlignCenter)
        col.addWidget(title); col.addWidget(sub)

        acts = QHBoxLayout(); acts.setSpacing(10); acts.setAlignment(Qt.AlignCenter)

        def action(text, ic, slot):
            b = QPushButton("  " + text); b.setObjectName("welcomeAction")
            b.setIcon(icon(ic, color=C["text2"])); b.setIconSize(QSize(16, 16))
            b.setCursor(Qt.PointingHandCursor); b.clicked.connect(slot)
            return b

        acts.addWidget(action("Klasör Aç", "mdi6.folder-open-outline", self.pick_project))
        acts.addWidget(action("Dosyaya Git", "mdi6.file-search-outline", self._open_file_palette))
        acts.addWidget(action("Görev Başlat", "mdi6.play", lambda: self.task_edit.setFocus()))
        col.addSpacing(6); col.addLayout(acts)

        kbd = QLabel("Ctrl+K komutlar   ·   Ctrl+P dosya   ·   Ctrl+B gezgin   ·   Ctrl+J panel")
        kbd.setObjectName("welcomeKbd"); kbd.setAlignment(Qt.AlignCenter)
        col.addSpacing(4); col.addWidget(kbd)

        crow = QHBoxLayout(); crow.addStretch(1); crow.addLayout(col); crow.addStretch(1)
        outer.addLayout(crow); outer.addStretch(1)
        return w

    def _open_in_editor(self, rel):
        """Dosyayı editörde aç ve karşılama ekranından editöre geç."""
        self.editor.open_file(rel)
        self.editor_stack.setCurrentWidget(self.editor)
        self._set_breadcrumb(rel)

    def _build_aipanel(self):
        """Sağ dikey AI paneli (Cursor benzeri): başlık + ekip + akış/değişiklikler +
        composer. Sohbet gibi tek sütun."""
        panel = QFrame(); panel.setObjectName("aipanel"); panel.setMinimumWidth(340)
        v = QVBoxLayout(panel); v.setContentsMargins(9, 9, 9, 9); v.setSpacing(9)

        # başlık
        hrow = QHBoxLayout(); hrow.setSpacing(8)
        hic = QToolButton(); hic.setObjectName("act"); hic.setEnabled(False)
        hic.setIcon(icon("mdi6.forum-outline", color=C["accent"])); hic.setIconSize(QSize(17, 17))
        htitle = QLabel("AJAN SOHBETİ"); htitle.setObjectName("panelTitle")
        self.hist_btn = QToolButton(); self.hist_btn.setObjectName("act")
        self.hist_btn.setIcon(icon("mdi6.history", color=C["muted"])); self.hist_btn.setIconSize(QSize(16, 16))
        self.hist_btn.setToolTip("Geçmiş"); self.hist_btn.clicked.connect(self._open_history)
        self.new_btn = QToolButton(); self.new_btn.setObjectName("act")
        self.new_btn.setIcon(icon("mdi6.broom", color=C["muted"])); self.new_btn.setIconSize(QSize(16, 16))
        self.new_btn.setToolTip("Sohbeti temizle"); self.new_btn.clicked.connect(self._clear_chat)
        hrow.addWidget(hic); hrow.addWidget(htitle); hrow.addStretch(1)
        hrow.addWidget(self.hist_btn); hrow.addWidget(self.new_btn)
        v.addLayout(hrow)

        # ekip (dikey timeline)
        self.pipeline = AgentPipeline(vertical=True)
        v.addWidget(self.pipeline)

        # akış / değişiklikler
        self.tabs = QTabWidget()
        self.chat = ChatView()
        self.chat.openChangesRequested.connect(lambda: self._focus_view("diff"))
        self.chat_empty = self._make_empty_state(
            "mdi6.robot-happy-outline", "Ajan ekibi hazır",
            "Bir görev yaz ve Çalıştır — Planner, Coder ve Reviewer sırayla çalışır; "
            "her adım burada canlı akar.")
        self.chat_stack = QStackedWidget()
        self.chat_stack.addWidget(self.chat_empty)   # 0: boş
        self.chat_stack.addWidget(self.chat)         # 1: sohbet kartları
        self.changes = ChangesPanel()
        self.changes.applyRequested.connect(self.apply_changes)
        self.changes.rejectRequested.connect(self.reject_changes)
        self.changes.fileActivated.connect(self._open_change_diff)
        self.tabs.addTab(self.chat_stack, icon("mdi6.robot-outline"), "Akış")
        self.tabs.addTab(self.changes, icon("mdi6.source-branch"), "Değişiklikler")
        self.tabs.currentChanged.connect(lambda i: fade_in(self.tabs.widget(i), dur=FAST))
        v.addWidget(self.tabs, 1)

        # composer (girdi + yönlendirme)
        v.addWidget(self._build_composer())
        return panel

    def _clear_chat(self):
        self.chat.clear(); self.changes.clear_all(); self.proposals = []
        self.chat_stack.setCurrentWidget(self.chat_empty)
        self.pipeline.reset()
        self.hint.setText("Sohbet temizlendi.")

    def _make_empty_state(self, icon_name, title, subtitle):
        """Ortalanmış premium boş durum: büyük ikon + başlık + alt açıklama."""
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(28, 24, 28, 24); v.setSpacing(10)
        v.addStretch(1)
        ic = QToolButton(); ic.setObjectName("emptyIcon"); ic.setEnabled(False)
        ic.setIcon(icon(icon_name, color=C["faint"])); ic.setIconSize(QSize(42, 42))
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(ic); row.addStretch(1)
        v.addLayout(row)
        t = QLabel(title); t.setObjectName("emptyTitle"); t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        s = QLabel(subtitle); s.setObjectName("empty"); s.setAlignment(Qt.AlignCenter)
        s.setWordWrap(True); s.setMaximumWidth(420)
        srow = QHBoxLayout(); srow.addStretch(1); srow.addWidget(s); srow.addStretch(1)
        v.addLayout(srow)
        v.addStretch(1)
        return w

    def _build_composer(self):
        card = QFrame(); card.setObjectName("composer")
        v = QVBoxLayout(card); v.setContentsMargins(11, 10, 11, 11); v.setSpacing(8)

        # kompakt yönlendirme: rol ikonu + model combo (tek satır)
        rrow = QHBoxLayout(); rrow.setSpacing(7)
        roles = [("planner", "claude", "mdi6.lightbulb-on-outline"),
                 ("coder", "deepseek", "mdi6.code-braces"),
                 ("reviewer", "gemini", "mdi6.eye-check-outline")]
        for role, default, ic in roles:
            rb = QToolButton(); rb.setObjectName("roleIc"); rb.setEnabled(False)
            rb.setIcon(icon(ic, color=C["muted"])); rb.setIconSize(QSize(15, 15))
            cb = QComboBox(); cb.addItems(PROVIDERS); cb.setCurrentText(default)
            cb.setToolTip(role.capitalize()); cb.currentTextChanged.connect(self._update_hud)
            self.combos[role] = cb
            rrow.addWidget(rb); rrow.addWidget(cb, 1)
        v.addLayout(rrow)

        self.hint = QLabel("Proje seç, bir görev yaz ve Çalıştır."); self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)

        self.task_edit = QPlainTextEdit(); self.task_edit.setObjectName("composerInput")
        self.task_edit.setPlaceholderText("Bir görev yaz…  ör. utils.py'deki tarih biçimini ISO 8601 yap")
        self.task_edit.setFixedHeight(76)
        v.addWidget(self.task_edit)

        self.run_btn = QPushButton("  Çalıştır"); self.run_btn.setObjectName("primary")
        self.run_btn.setIcon(icon("mdi6.send", color="#07122b")); self.run_btn.setIconSize(QSize(17, 17))
        self.run_btn.setFixedHeight(40); self.run_btn.clicked.connect(self._run_or_stop)
        v.addWidget(self.run_btn)
        return card

    def _build_statusbar(self):
        bar = QFrame(); bar.setObjectName("statusbar"); bar.setFixedHeight(30)
        h = QHBoxLayout(bar); h.setContentsMargins(14, 0, 14, 0); h.setSpacing(14)

        self.st_project = QLabel("proje yok"); self.st_project.setObjectName("seg")
        self.st_agents = QLabel(""); self.st_agents.setObjectName("seg")
        self.st_cost = QLabel("Σ 0 tok · $0.000"); self.st_cost.setObjectName("segAccent")
        h.addWidget(self.st_project)
        h.addStretch(1)
        h.addWidget(self.st_agents)
        sep = QLabel("·"); sep.setObjectName("seg"); h.addWidget(sep)
        h.addWidget(self.st_cost)
        return bar

    def _update_hud(self, *_):
        if self.project_root:
            name = os.path.basename(self.project_root.rstrip("/\\"))
            self.st_project.setText("📁 " + name)
            self.proj_chip.setText(name)
        routing = {r: cb.currentText() for r, cb in self.combos.items()}
        self.st_agents.setText("🤖 " + " · ".join(f"{r[0].upper()}:{m}" for r, m in routing.items()))
        if hasattr(self, "pipeline"):
            self.pipeline.set_agents(routing)
        if self.last_totals:
            self._animate_hud(self.last_totals["tokens"], self.last_totals["cost_usd"])

    def _animate_hud(self, tokens, cost):
        """Token/maliyet toplamını eski değerden yeniye akıcı say."""
        t0, c0 = self._hud_tokens, self._hud_cost

        def render(tok):
            span = (tokens - t0) or 1
            frac = max(0.0, min(1.0, (tok - t0) / span))
            c = c0 + (cost - c0) * frac
            self.st_cost.setText(f"Σ {int(round(tok))} tok · ${c:.3f}")

        # referansı tut: QVariantAnimation aksi halde başlamadan GC'lenir
        self._hud_anim = count_to(render, t0, tokens)
        self._hud_tokens, self._hud_cost = float(tokens), float(cost)

    def _focus_view(self, which):
        self.btn_explorer.setChecked(which == "explorer")
        self.btn_chat.setChecked(which == "chat")
        self.btn_diff.setChecked(which == "diff")
        self.btn_explorer.setIcon(icon("mdi6.file-multiple-outline", active=which == "explorer"))
        self.btn_chat.setIcon(icon("mdi6.robot-outline", active=which == "chat"))
        self.btn_diff.setIcon(icon("mdi6.source-branch", active=which == "diff"))
        active_btn = {"explorer": self.btn_explorer, "chat": self.btn_chat, "diff": self.btn_diff}[which]
        self._move_indicator(active_btn)
        if which == "explorer":
            self.tree.setFocus()
        elif which == "chat":
            self.tabs.setCurrentWidget(self.chat_stack)
            fade_in(self.chat_stack, dur=FAST)
        elif which == "diff":
            self.tabs.setCurrentWidget(self.changes)
            fade_in(self.changes, dur=FAST)

    # ---------------- proje / editör / gezgin ----------------
    def pick_project(self):
        d = QFileDialog.getExistingDirectory(self, "Proje klasörünü seç")
        if not d:
            return
        self.project_root = d
        self.editor.set_project(d)
        self.tree.setRootIndex(self.fs_model.setRootPath(d))
        self._history = HistoryStore(d)
        self.bottom.set_root(d)
        self._update_hud()
        self.hint.setText("Hazır — bir görev yaz ve Çalıştır.")

    def _open_history(self):
        recs = self._history.all() if self._history else []
        self.history_dialog.open_with(recs)

    def _restore_task(self, task):
        self.task_edit.setPlainText(task)
        self.task_edit.setFocus()

    def _open_from_tree(self, index):
        path = self.fs_model.filePath(index)
        if os.path.isfile(path) and self.project_root:
            rel = os.path.relpath(path, self.project_root).replace("\\", "/")
            self._open_in_editor(rel)

    def _set_breadcrumb(self, rel):
        proj = os.path.basename(self.project_root.rstrip("/\\")) if self.project_root else ""
        segs = [proj] + [s for s in rel.split("/") if s]
        sep = f'<span style="color:{C["faint"]}">  ›  </span>'
        parts = []
        for i, s in enumerate(segs):
            col = C["text"] if i == len(segs) - 1 else C["muted"]
            weight = "600" if i == len(segs) - 1 else "400"
            parts.append(f'<span style="color:{col}; font-weight:{weight}">{s}</span>')
        self.breadcrumb.setText(sep.join(parts))
        self.breadcrumb.setVisible(True)
        fade_in(self.breadcrumb, dur=FAST)

    def save_current(self):
        self.editor.save_active()

    def _on_editor_saved(self, rel, content):
        try:
            Project(self.project_root).apply(rel, content, backup=False)
            self.editor.mark_saved(rel)
            self.hint.setText(f"Kaydedildi: {rel}")
        except Exception as e:
            self.hint.setText(f"Kaydetme hatası: {e}")

    # ---------------- ajan akışı ----------------
    def _run_or_stop(self):
        if self._running:
            self._stop_run()
        else:
            self.start_run()

    def _set_run_ui(self, running):
        self._running = running
        if running:
            self.run_btn.setText("  Durdur")
            self.run_btn.setIcon(icon("mdi6.stop", color="#07122b"))
        else:
            self.run_btn.setText("  Çalıştır")
            self.run_btn.setIcon(icon("mdi6.send", color="#07122b"))

    def _stop_run(self):
        if self.worker is not None and self._running:
            self.worker.cancel()
            self.hint.setText("Durduruluyor…")

    def start_run(self):
        if self._running:
            return
        if not self.project_root:
            self.hint.setText("Önce bir proje klasörü seç."); return
        task = self.task_edit.toPlainText().strip()
        if not task:
            self.hint.setText("Görev boş."); return
        self._last_task = task
        self.chat.clear(); self.changes.clear_all(); self.proposals = []
        self.chat.add_user_task(task)
        self.chat_stack.setCurrentWidget(self.chat)   # boş durumdan sohbete geç
        self._set_run_ui(True); self.hint.setText("Çalışıyor…")
        self._focus_view("chat")
        routing = {r: cb.currentText() for r, cb in self.combos.items()}
        self.pipeline.set_agents(routing); self.pipeline.reset()
        self.worker = Worker(self.project_root, task, routing)
        self.worker.event.connect(self.on_event)
        self.worker.failed.connect(self.on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(lambda: self._set_run_ui(False))
        self.worker.start()

    def _on_cancelled(self):
        self.pipeline.error()
        self.chat.add_info("Koşu durduruldu.")
        self.hint.setText("Durduruldu."); self._set_run_ui(False)

    def on_event(self, ev):
        t = ev["type"]
        if t == "stage":
            self.chat.start_stage(ev["stage"], ev["provider"])
            if ev["stage"] in STAGE_ROLE:
                self.pipeline.set_running(STAGE_ROLE[ev["stage"]])
        elif t == "info":
            self.chat.add_info(ev["text"])
        elif t == "output":
            self.chat.append_output(ev.get("stage", ""), ev["text"])
        elif t == "metric":
            if ev.get("stage") in STAGE_ROLE:
                self.chat.set_metric(ev["stage"], ev["model"], ev["latency_s"], ev["tokens"], ev["cost_usd"])
                self.pipeline.set_metric(STAGE_ROLE[ev["stage"]], ev["model"], ev["latency_s"], ev["tokens"], ev["cost_usd"])
        elif t == "diff":
            self.changes.add_row(ev["path"], ev["is_new"], ev["diff"])
        elif t == "verdict":
            self.chat.set_verdict(ev["verdict"], ev.get("note", ""))
            self.changes.set_verdict(ev["verdict"], ev.get("note", ""))
        elif t == "proposal":
            self._on_proposal(ev)

    def _open_change_diff(self, path):
        """Değişiklik listesinde bir dosyaya tıklanınca merkez inline diff'i aç (Cursor deseni)."""
        prop = next((p for p in self.proposals if p["path"] == path), None)
        if not prop or not self.project_root:
            return
        original = ""
        try:
            proj = Project(self.project_root)
            if not prop.get("is_new") and proj.exists(path):
                original = proj.read_file(path)
        except Exception:
            pass
        self.editor_stack.setCurrentWidget(self.editor)   # welcome'dan editöre geç
        self.editor.open_diff(path, original, prop.get("new", ""))

    def _on_proposal(self, ev):
        self.proposals = ev["proposals"]; self.last_totals = ev["totals"]
        self.changes.attach_contents(self.proposals)   # Uygula durumunu panel yönetir
        self.pipeline.finish()
        self._update_hud()
        # oturum geçmişine kaydet (Faz E)
        if self._history:
            tt = ev.get("totals", {})
            self._history.add(self._last_task, ev.get("verdict", "UNKNOWN"),
                              tt.get("tokens", 0), tt.get("cost_usd", 0.0),
                              [p["path"] for p in self.proposals])
        if self.proposals:
            self.chat.add_summary(len(self.proposals))
            self.hint.setText(f'✅ {len(self.proposals)} dosya önerisi — Değişiklikler sekmesinde incele.')
            self._focus_view("diff")                              # sağ panelde dosya listesi
            self._open_change_diff(self.proposals[0]["path"])     # ilk diff'i merkezde aç
        else:
            self.chat.add_info("Değişiklik önerisi çıkmadı.")
            self.hint.setText("Değişiklik önerisi çıkmadı.")

    def apply_changes(self):
        selected = self.changes.checked()
        if not selected:
            self.hint.setText("Uygulanacak dosya seçilmedi."); return
        proj = Project(self.project_root); n = 0; errs = []
        for p in selected:
            try:
                proj.apply(p["path"], p["new"])
                self.editor.reload(p["path"])
                n += 1
            except Exception as e:
                errs.append(f"{p['path']}: {e}")
        self.chat.add_info(f"✓ {n} dosya uygulandı (var olanlar .bak yedeklendi).")
        for e in errs:
            self.chat.add_error(e)
        self.hint.setText(f"{n} dosya uygulandı (var olanlar .bak yedeklendi).")
        self.proposals = []
        self.changes.clear_all()
        self.editor.close_diff()      # merkez diff'i kapat, editöre dön
        self._focus_view("chat")

    def reject_changes(self):
        self.proposals = []; self.changes.clear_all()
        self.editor.close_diff()
        self.hint.setText("Değişiklikler reddedildi.")

    def on_failed(self, msg):
        self.chat_stack.setCurrentWidget(self.chat)
        self.chat.add_error(f"Hata: {msg}")
        self.pipeline.error()
        self.hint.setText("Hata oluştu.")

    @staticmethod
    def _esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace(" ", "&nbsp;").replace("\n", "<br>"))


def main():
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    load_fonts()                       # gömülü Inter + JetBrains Mono
    app.setFont(QFont(theme.FONT_UI, 10))
    prefs = ui_prefs.load()            # kullanıcı tercihlerini tema öncesi uygula
    theme.set_accent(prefs["accent"]); theme.set_density(prefs["density"])
    anim.set_enabled(prefs["animations"])
    app.setStyleSheet(build_style())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
