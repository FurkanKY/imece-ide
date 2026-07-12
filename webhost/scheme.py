"""
scheme.py — `app://` özel URL şeması.

Neden file:// değil: Vite ES modülleri/worker'ları file:// altında CORS'a takılır.
SecureScheme → secure context (worker'lar, clipboard API); CorsEnabled+FetchApiAllowed →
modül/font/worker yüklemeleri serbest. register_scheme() QApplication'dan ÖNCE çağrılmalı.
"""

import mimetypes
import os

from runtime_paths import resource_path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWebEngineCore import (
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
    QWebEngineUrlRequestJob,
)

UI_DIST = str(resource_path("web", "ui", "dist"))

_EXTRA_MIME = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".json": "application/json", ".wasm": "application/wasm",
    ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".svg": "image/svg+xml", ".map": "application/json",
}


def register_scheme() -> None:
    """app:// şemasını kaydet — QApplication kurulmadan ÖNCE çağır."""
    scheme = QWebEngineUrlScheme(b"app")
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.CorsEnabled
        | QWebEngineUrlScheme.Flag.FetchApiAllowed
    )
    QWebEngineUrlScheme.registerScheme(scheme)


class UiSchemeHandler(QWebEngineUrlSchemeHandler):
    """app://ui/<yol> → web/ui/dist/<yol>. Yol güvenliği: dist dışına çıkılamaz."""

    def __init__(self, parent=None, root: str = UI_DIST):
        super().__init__(parent)
        self._root = os.path.abspath(root)
        self._buffers = []  # QBuffer'ları yaşat (GC job bitmeden toplamasın)

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        url = job.requestUrl()
        rel = url.path().lstrip("/") or "index.html"
        path = os.path.abspath(os.path.join(self._root, rel))
        if not path.startswith(self._root + os.sep) and path != self._root:
            job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
            return
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.exists(path):
            # SPA fallback: bilinmeyen yol → index.html
            path = os.path.join(self._root, "index.html")
            if not os.path.exists(path):
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
        ext = os.path.splitext(path)[1].lower()
        mime = _EXTRA_MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        buf = QBuffer(parent=job)
        buf.setData(data)
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._buffers.append(buf)
        job.destroyed.connect(lambda *_: self._buffers.remove(buf) if buf in self._buffers else None)
        job.reply(mime.encode(), buf)
