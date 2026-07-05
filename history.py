"""
history.py
----------
Faz E — proje başına oturum/iterasyon geçmişi. Her ajan koşusu (görev → sonuç →
maliyet) proje-içi `.magent/history.json`'a kaydedilir; AI panelindeki geçmiş
drawer'ı bunları listeler, tıklanınca görev geri yüklenir.
"""

import os
import json
import time

MAX_ITEMS = 100


class HistoryStore:
    def __init__(self, project_root: str):
        self.dir = os.path.join(project_root, ".magent")
        self.path = os.path.join(self.dir, "history.json")

    def all(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def add(self, task: str, verdict: str, tokens: int, cost_usd: float, files: list[str]) -> dict:
        rec = {
            "ts": time.time(),
            "task": (task or "").strip(),
            "verdict": verdict or "UNKNOWN",
            "tokens": int(tokens or 0),
            "cost_usd": round(float(cost_usd or 0.0), 5),
            "files": list(files or []),
        }
        items = self.all()
        items.insert(0, rec)          # en yeni en üstte
        items = items[:MAX_ITEMS]
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return rec


def rel_time(ts: float) -> str:
    """Basit göreli zaman: '32 sn önce', '5 dk önce', '2 sa önce', 'dün', 'GG.AA'."""
    d = max(0, time.time() - ts)
    if d < 60:
        return f"{int(d)} sn önce"
    if d < 3600:
        return f"{int(d // 60)} dk önce"
    if d < 86400:
        return f"{int(d // 3600)} sa önce"
    if d < 172800:
        return "dün"
    return time.strftime("%d.%m", time.localtime(ts))
