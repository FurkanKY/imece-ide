"""applog.py — beta hata dayanıklılığı: dönen dosya log'u + global istisna yakalama.

Beta kullanıcısı çökme anında ne görür sorusunun cevabı:
  1) her şey ~/.multi_agent_ide/logs/app.log'a yazılır (1MB × 3, dönen)
  2) yakalanmamış Python istisnası uygulamayı sessizce öldürmez — log + web'e
     `app.error` olayı (UI temalı hata toast'ı gösterir; "sorun bildir" oradan)

GİZLİLİK: istem içerikleri ve API anahtarları LOGLANMAZ — yalnız hata mesajı +
stack. (app.log köprü metodu da mesajı kırpar; anahtar taşıyan params loglanmaz.)
"""

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path

from runtime_paths import log_path

LOG_PATH = log_path()

logger = logging.getLogger("magent")
_bridge = None


def setup(log_path: Path | None = None) -> None:
    """Uygulama girişinde BİR KEZ: dosya handler + global excepthook'lar."""
    global LOG_PATH
    if log_path is not None:  # test override
        LOG_PATH = Path(log_path)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    sys.excepthook = _excepthook
    threading.excepthook = lambda args: _excepthook(
        args.exc_type, args.exc_value, args.exc_traceback)
    logger.info("oturum başladı (%s)", sys.platform)


def attach_bridge(bridge) -> None:
    """Pencere kurulunca çağrılır — istisnalar UI'a da duyurulabilsin."""
    global _bridge
    _bridge = bridge


def _excepthook(etype, value, tb) -> None:
    # C-katmanı/thread istisnaları traceback'siz gelebilir (ör. OverflowError);
    # repr + thread adı olmadan teşhis imkânsızdı (Beta-3 PTY blokeri dersi).
    text = "".join(traceback.format_exception(etype, value, tb))
    thread = threading.current_thread().name
    logger.error("YAKALANMAMIŞ İSTİSNA [thread=%s] %r\n%s", thread, value, text)
    if _bridge is not None:
        try:
            _bridge.emit_event_from_any_thread("app.error", {
                "message": str(value) or etype.__name__,
                "logPath": str(LOG_PATH),
            })
        except Exception:
            pass  # köprü de düştüyse en azından log var
    sys.__excepthook__(etype, value, tb)
