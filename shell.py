"""
shell.py — masaüstü mini-IDE girişi (web-shell mimarisi, bkz. docs/ARCHITECTURE.md).

  python shell.py            web/ui/dist derlemesini app:// üzerinden yükler
  python shell.py --dev      http://localhost:5173 (Vite HMR) + F12 DevTools
"""

import os
import sys


def _run_packaged_helper() -> int | None:
    """Tek exe içindeki alt-süreç girişleri (Qt kurulmadan önce çalışır)."""
    from process_runtime.supervisor_launch import DISPATCH_FLAG
    if DISPATCH_FLAG in sys.argv:
        if len(sys.argv) < 2 or sys.argv[1] != DISPATCH_FLAG:
            return 125
        sys.argv.pop(1)
        try:
            from process_runtime.supervisor import main as supervisor_main
            return supervisor_main()
        except BaseException:
            return 125
    if "--imece-debugpy" in sys.argv:
        sys.argv.remove("--imece-debugpy")
        from debugpy.server.cli import main as debugpy_main
        debugpy_main()
        return 0
    if "--imece-lsp" in sys.argv:
        sys.argv.remove("--imece-lsp")
        from basedpyright.langserver import main as lsp_main
        lsp_main()
        return 0
    return None


def _configure_graphics() -> None:
    """Frozen Linux compatibility default; never relax Chromium's sandbox.

    GPU-backed QtWebEngine startup was intermittent on the build host. Keep
    source/Windows defaults unchanged and allow an explicit hardware opt-in.
    Helpers return before this GUI-only configuration is reached.
    """
    software = "--software-rendering" in sys.argv
    hardware = "--hardware-rendering" in sys.argv
    if software and hardware:
        raise ValueError("Choose either software or hardware rendering")
    if not software and not (getattr(sys, "frozen", False) and sys.platform.startswith("linux") and not hardware):
        return
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-gpu" not in flags.split():
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --disable-gpu").strip()


def main() -> int:
    helper_exit = _run_packaged_helper()
    if helper_exit is not None:
        return helper_exit
    _configure_graphics()
    dev = "--dev" in sys.argv

    # Kaynak modunda depo .env'i; pakette yazılabilir LOCALAPPDATA kopyası.
    from dotenv import load_dotenv
    from runtime_paths import env_path
    load_dotenv(env_path())
    # Paketli uygulama .env yerine DPAPI deposunu önceliklendirir. Eski .env
    # taşıması Ayarlar anahtar durumu ilk okununca güvenle tamamlanır.
    from secret_store import SecretStoreError, packaged_store
    try:
        store = packaged_store()
        if store:
            os.environ.update(store.load())
    except SecretStoreError as exc:
        print(f"Anahtar deposu okunamadı: {exc}")

    # Beta-2: dosya log'u + global istisna yakalama — HER ŞEYDEN önce.
    from webhost import applog
    applog.setup()

    # Şema kaydı QApplication'dan ÖNCE olmalı.
    from webhost.scheme import register_scheme, UI_DIST
    register_scheme()

    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)

    if not dev and not os.path.exists(os.path.join(UI_DIST, "index.html")):
        print("web/ui/dist bulunamadı. Önce derleyin:  cd web/ui && npm run build")
        print("(Gelistirme icin:  python shell.py --dev  +  cd web/ui && npm run dev)")
        return 1

    from webhost.api import register_all
    register_all()

    from webhost.window import ShellWindow
    win = ShellWindow(dev=dev)
    applog.attach_bridge(win.bridge)  # istisnalar UI'a da duyurulur
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
