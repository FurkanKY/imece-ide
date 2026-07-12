"""
shell.py — masaüstü mini-IDE girişi (web-shell mimarisi, bkz. docs/ARCHITECTURE.md).

  python shell.py            web/ui/dist derlemesini app:// üzerinden yükler
  python shell.py --dev      http://localhost:5173 (Vite HMR) + F12 DevTools
"""

import os
import sys


def main() -> int:
    dev = "--dev" in sys.argv

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
