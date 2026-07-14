"""history.* — oturum geçmişi (motor `history.py:HistoryStore` sarmalanır)."""

from history import HistoryStore
from receipts import ReceiptStore
from webhost import state
from webhost.bridge import handler, BridgeError


@handler("history.list")
def _list(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return {"items": HistoryStore(proj.root).all()}


@handler("receipt.get")
def _receipt_get(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    try:
        return {"receipt": ReceiptStore(proj.root).get(params.get("receiptId", ""))}
    except ValueError as exc:
        raise BridgeError("not_found", str(exc)) from exc


@handler("receipt.export")
def _receipt_export(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    try:
        path = ReceiptStore(proj.root).export_markdown(params.get("receiptId", ""), params.get("directory", ""))
        return {"path": str(path)}
    except ValueError as exc:
        raise BridgeError("export_failed", str(exc)) from exc
