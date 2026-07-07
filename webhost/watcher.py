"""
watcher.py — dosya sistemi izleyicisi (QFileSystemWatcher).

Gezginde yüklenen klasörler izlenir; dışarıdan değişiklik olunca (150ms debounce)
`fs.changed` olayı yayınlanır → web ağacı tazeler. Proje değişince sıfırlanır.
"""

import os

from PySide6.QtCore import QObject, QFileSystemWatcher, QTimer


class Watcher(QObject):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._root: str | None = None
        self._fsw = QFileSystemWatcher(self)
        self._fsw.directoryChanged.connect(self._on_dir_changed)
        # debounce: hızlı ardışık değişiklikleri tek olayda topla
        self._pending: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._flush)

    def reset(self, root: str) -> None:
        dirs = self._fsw.directories()
        if dirs:
            self._fsw.removePaths(dirs)
        self._root = os.path.abspath(root)
        self._pending.clear()
        self.watch_dir(self._root)

    def watch_dir(self, abs_path: str) -> None:
        if self._root is None or not os.path.isdir(abs_path):
            return
        if abs_path not in self._fsw.directories():
            self._fsw.addPath(abs_path)

    def _on_dir_changed(self, abs_path: str) -> None:
        if self._root is None:
            return
        rel = os.path.relpath(abs_path, self._root).replace("\\", "/")
        self._pending.add("" if rel == "." else rel)
        self._timer.start()

    def _flush(self) -> None:
        if not self._pending:
            return
        paths = sorted(self._pending)
        self._pending.clear()
        self._bridge.emit_event("fs.changed", {"kind": "modified", "paths": paths})


_instance: Watcher | None = None


def init(bridge, parent=None) -> Watcher:
    global _instance
    _instance = Watcher(bridge, parent)
    return _instance


def get() -> Watcher | None:
    return _instance
