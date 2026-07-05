"""
tools/uishot.py
---------------
Faz A — görsel öz-doğrulama düzeni. Uygulamayı OFFSCREEN kurar, farklı UI
durumlarını simüle eder ve her birinin PNG'sini `.uishots/` altına yazar.
Böylece geliştirici (veya AI) native pencereye ihtiyaç duymadan görünümü inceler.

Sınır: Monaco editör (QWebEngineView) offscreen render OLMAZ (boş görünür).
Editör içi görsel, gerçek `python desktop.py` ile doğrulanır. Chrome, aktivite,
gezgin, AI paneli, kartlar, palette, ayarlar → render olur.

Çalıştır:
    QT_QPA_PLATFORM=offscreen python tools/uishot.py
Çıktı:
    <repo>/.uishots/*.png  (yolları stdout'a yazar)
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
OUT = os.path.join(_REPO, ".uishots")
os.makedirs(OUT, exist_ok=True)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import desktop
import theme
import ui_prefs


def _save(widget, name):
    for _ in range(2):
        app.processEvents()
    path = os.path.join(OUT, name + ".png")
    widget.grab().save(path)
    print("  ->", path)
    return path


def _fresh():
    """Temiz bir MainWindow (senaryo başına state karışmasın)."""
    w = desktop.MainWindow()
    w.resize(1320, 880)
    w.show()
    for _ in range(3):
        app.processEvents()
    return w


def _set_project(w, root):
    w.project_root = root
    w.editor.set_project(root)
    w.tree.setRootIndex(w.fs_model.setRootPath(root))
    w._update_hud()
    for _ in range(2):
        app.processEvents()


def _feed(w, events):
    for ev in events:
        w.on_event(ev)
    for _ in range(2):
        app.processEvents()


def _run(w, task, events):
    """start_run'ın sohbet kurulumunu (Worker'sız) taklit et → chat kartları dolsun."""
    w.chat.clear(); w.changes.clear_all(); w.proposals = []
    w.chat.add_user_task(task)
    w.chat_stack.setCurrentWidget(w.chat)
    _feed(w, events)


TASK = "utils.py'deki tarih biçimini ISO 8601 yap"


# ---- olay dizileri ----
RUN_PARTIAL = [
    {"type": "stage", "stage": "plan", "provider": "claude"},
    {"type": "output", "stage": "plan", "text": "1. utils.py okunacak\n2. format fonksiyonu ISO 8601'e çevrilecek"},
    {"type": "metric", "stage": "plan", "provider": "claude", "model": "claude-code", "latency_s": 8.5, "tokens": 176, "cost_usd": 0.029},
    {"type": "stage", "stage": "code", "provider": "deepseek"},
]
RUN_FULL = RUN_PARTIAL + [
    {"type": "metric", "stage": "code", "provider": "deepseek", "model": "deepseek-v4-pro", "latency_s": 5.0, "tokens": 650, "cost_usd": 0.0004},
    {"type": "stage", "stage": "review", "provider": "gemini"},
    {"type": "output", "stage": "review", "text": "VERDICT: APPROVED\nTemiz ve doğru."},
    {"type": "metric", "stage": "review", "provider": "gemini", "model": "gemini-3.5-flash", "latency_s": 3.8, "tokens": 205, "cost_usd": 0.00002},
    {"type": "verdict", "verdict": "APPROVED", "note": "Temiz ve doğru."},
    {"type": "diff", "path": "utils.py", "is_new": False,
     "diff": "--- a/utils.py\n+++ b/utils.py\n@@ -3,3 +3,3 @@\n-    return d.strftime('%d/%m/%Y')\n+    return d.isoformat()"},
    {"type": "proposal", "proposals": [{"path": "utils.py", "new": "x\n", "diff": "...", "is_new": False}],
     "totals": {"latency_s": 17.3, "tokens": 1031, "cost_usd": 0.0294}, "verdict": "APPROVED"},
]


def main():
    theme.load_fonts()
    prefs = ui_prefs.load()
    theme.set_accent(prefs["accent"]); theme.set_density(prefs["density"])
    app.setStyleSheet(desktop.build_style())

    print("[1] boş / ilk açılış")
    w = _fresh()
    _save(w, "01_full_empty")
    _save(w.ai_panel, "01_aipanel_empty")

    print("[2] proje yüklü (editör boş — Monaco offscreen render olmaz)")
    w = _fresh()
    _set_project(w, _REPO)
    _save(w, "02_full_project")
    _save(w.side_panel, "02_sidebar")

    print("[3] koşu anı (Coder çalışıyor)")
    w = _fresh(); _set_project(w, _REPO); _run(w, TASK, RUN_PARTIAL)
    _save(w, "03_full_running")
    _save(w.ai_panel, "03_aipanel_running")
    _save(w.pipeline, "03_pipeline_running")

    print("[4] sonuç (öneri + verdict)")
    w = _fresh(); _set_project(w, _REPO); _run(w, TASK, RUN_FULL)
    _save(w, "04_full_result")
    _save(w.ai_panel, "04_aipanel_result")
    try:
        w._focus_view("diff");
        for _ in range(2): app.processEvents()
        _save(w.ai_panel, "04_aipanel_changes")
    except Exception as e:
        print("  (değişiklikler görünümü atlandı:", e, ")")

    print("[5] ayarlar overlay")
    w = _fresh()
    try:
        w.settings.open_with(w._prefs)
        for _ in range(3): app.processEvents()
        _save(w.settings, "05_settings")
    except Exception as e:
        print("  (ayarlar atlandı:", e, ")")

    print("[6] geçmiş overlay")
    import time
    w = _fresh()
    recs = [
        {"ts": time.time() - 90, "task": "utils.py'deki tarih biçimini ISO 8601 yap",
         "verdict": "APPROVED", "tokens": 1031, "cost_usd": 0.0294, "files": ["utils.py"]},
        {"ts": time.time() - 3600, "task": "config.py'ye loglama seviyesi ayarı ekle",
         "verdict": "NEEDS_FIX", "tokens": 820, "cost_usd": 0.021, "files": ["config.py", "log.py"]},
        {"ts": time.time() - 90000, "task": "README dosya haritasını güncelle",
         "verdict": "APPROVED", "tokens": 300, "cost_usd": 0.004, "files": ["README.md"]},
    ]
    try:
        w.history_dialog.open_with(recs)
        for _ in range(3): app.processEvents()
        _save(w.history_dialog, "06_history")
    except Exception as e:
        print("  (geçmiş atlandı:", e, ")")

    print("\nTüm PNG'ler:", OUT)


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)
    main()
