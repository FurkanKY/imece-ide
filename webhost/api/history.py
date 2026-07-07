"""history.* — oturum geçmişi (motor `history.py:HistoryStore` sarmalanır)."""

from history import HistoryStore
from webhost import state
from webhost.bridge import handler, BridgeError


@handler("history.list")
def _list(params, ctx):
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return {"items": HistoryStore(proj.root).all()}
