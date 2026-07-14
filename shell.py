"""
shell.py — masaüstü mini-IDE girişi (web-shell mimarisi, bkz. docs/ARCHITECTURE.md).

  python shell.py            web/ui/dist derlemesini app:// üzerinden yükler
  python shell.py --dev      http://localhost:5173 (Vite HMR) + F12 DevTools
"""

import os
import sys


def _run_packaged_helper() -> bool:
    """Tek exe içindeki alt-süreç girişleri (Qt kurulmadan önce çalışır)."""
    if "--magent-debugpy" in sys.argv:
        sys.argv.remove("--magent-debugpy")
        from debugpy.server.cli import main as debugpy_main
        debugpy_main()
        return True
    if "--magent-lsp" in sys.argv:
        sys.argv.remove("--magent-lsp")
        from basedpyright.langserver import main as lsp_main
        lsp_main()
        return True
    return False


def main() -> int:
    if _run_packaged_helper():
        return 0
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
