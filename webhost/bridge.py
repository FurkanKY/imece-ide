"""
bridge.py — QWebChannel RPC dispatcher.

Tek `HostBridge` nesnesi kanala `host` adıyla kayıtlıdır.
  JS → Python : host.call('{"id":7,"method":"fs.readFile","params":{...}}')
  Python → JS : reply sinyali '{"id":7,"ok":true,"result":{...}}'
                event sinyali '{"channel":"run.event","payload":{...}}'

Handler kaydı dekoratörle:  @handler("app.info") def _(params): return {...}
Uzun işler handler içinde thread'e alınır ve `resolver` ile sonradan çözülür;
sinyaller HER ZAMAN ana thread'den emit edilir (emit_* yardımcıları bunu garanti eder).
"""

import json
import traceback

from PySide6.QtCore import QObject, Signal, Slot, QTimer

# method adı -> handler(params: dict, ctx: "CallContext") -> dict | None
_REGISTRY: dict = {}


def handler(method: str):
    """Köprü metodu kaydeder. Handler dict dönerse hemen yanıtlanır;
    None dönerse handler ctx.resolve/ctx.fail ile sonradan yanıtlar (async)."""
    def deco(fn):
        _REGISTRY[method] = fn
        return fn
    return deco


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CallContext:
    """Async handler'lar için geciken yanıt sözleşmesi (thread-güvenli)."""

    def __init__(self, bridge: "HostBridge", call_id: int):
        self._bridge = bridge
        self._id = call_id

    def resolve(self, result: dict) -> None:
        self._bridge.reply_from_any_thread(self._id, ok=True, result=result)

    def fail(self, code: str, message: str) -> None:
        self._bridge.reply_from_any_thread(self._id, ok=False,
                                           error={"code": code, "message": message})


class HostBridge(QObject):
    reply = Signal(str)
    event = Signal(str)
    # iç kullanım: başka thread'den ana thread'e sinyal taşıma
    _deferred = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._deferred.connect(self.reply)  # queued: hangi thread'den gelirse gelsin

    # ---- JS → Python ----
    @Slot(str)
    def call(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            call_id, method, params = msg["id"], msg["method"], msg.get("params") or {}
        except (ValueError, KeyError):
            return  # bozuk zarf — yanıt verilemez (id yok)
        fn = _REGISTRY.get(method)
        if fn is None:
            self._reply(call_id, ok=False,
                        error={"code": "unknown_method", "message": f"Bilinmeyen metot: {method}"})
            return
        try:
            result = fn(params, CallContext(self, call_id))
            if result is not None:
                self._reply(call_id, ok=True, result=result)
        except BridgeError as e:
            self._reply(call_id, ok=False, error={"code": e.code, "message": str(e)})
        except Exception as e:  # handler çökmesi UI'a düzgün hata olarak gitsin
            traceback.print_exc()
            self._reply(call_id, ok=False, error={"code": "internal", "message": str(e)})

    # ---- Python → JS ----
    def _reply(self, call_id: int, ok: bool, result: dict | None = None,
               error: dict | None = None) -> None:
        payload = {"id": call_id, "ok": ok}
        if ok:
            payload["result"] = result or {}
        else:
            payload["error"] = error or {}
        self.reply.emit(json.dumps(payload, ensure_ascii=False))

    def reply_from_any_thread(self, call_id: int, ok: bool, result: dict | None = None,
                              error: dict | None = None) -> None:
        payload = {"id": call_id, "ok": ok}
        if ok:
            payload["result"] = result or {}
        else:
            payload["error"] = error or {}
        self._deferred.emit(json.dumps(payload, ensure_ascii=False))

    def emit_event(self, channel: str, payload: dict) -> None:
        """Ana thread'den olay yayınla. (Thread'lerden: QTimer.singleShot(0, ...) ile sar.)"""
        self.event.emit(json.dumps({"channel": channel, "payload": payload}, ensure_ascii=False))

    def emit_event_from_any_thread(self, channel: str, payload: dict) -> None:
        QTimer.singleShot(0, lambda: self.emit_event(channel, payload))
