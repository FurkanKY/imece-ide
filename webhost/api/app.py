"""app.* — uygulama düzeyi köprü metotları."""

import os
import platform
import subprocess

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog

from webhost.bridge import handler, BridgeError
from webhost import applog

APP_VERSION = "0.3.0-beta.1"


@handler("app.log")
def _log(params, ctx):
    """Web tarafı hataları dosya log'una (Beta-2). Mesaj kırpılır; istem/anahtar
    içeriği taşınmaz — çağıran yalnız hata mesajı + stack gönderir."""
    import logging
    level = {"error": logging.ERROR, "warn": logging.WARNING}.get(
        params.get("level"), logging.INFO)
    message = str(params.get("message", ""))[:2000]
    stack = str(params.get("stack") or "")[:4000]
    applog.logger.log(level, "WEB %s%s", message, ("\n" + stack) if stack else "")
    return {"logPath": str(applog.LOG_PATH)}


@handler("app.info")
def _info(params, ctx):
    from PySide6.QtWebEngineCore import qWebEngineChromiumVersion
    return {
        "version": APP_VERSION,
        "platform": platform.system().lower(),
        "chromium": qWebEngineChromiumVersion(),
    }


@handler("app.openExternal")
def _open_external(params, ctx):
    url = params.get("url", "")
    if not url.startswith(("http://", "https://")):
        raise BridgeError("bad_url", "Yalnız http(s) bağlantıları açılabilir.")
    QDesktopServices.openUrl(QUrl(url))
    return {}


@handler("app.pickFolder")
def _pick_folder(params, ctx):
    # Tek kalan native diyalog (OS klasör seçici) — bilinçli karar, bkz. plan.
    path = QFileDialog.getExistingDirectory(None, params.get("title") or "Proje klasörü seç")
    return {"path": path or None}


@handler("app.clipboardWrite")
def _clip_write(params, ctx):
    QApplication.clipboard().setText(params.get("text", ""))
    return {}


@handler("app.revealInOS")
def _reveal(params, ctx):
    # P1'de aktif Project köküne bağlanır; şimdilik mutlak yol kabul eder.
    path = params.get("path", "")
    if not path or not os.path.exists(path):
        raise BridgeError("not_found", "Yol bulunamadı.")
    if platform.system() == "Windows":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    return {}
