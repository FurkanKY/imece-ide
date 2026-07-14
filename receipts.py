"""Proje içi değişiklik makbuzları.

Makbuzlar .imece/receipts altında kalır; history.json yalnız hızlı indeks görevi
görür. Böylece ayrıntılı diff/kanıt saklanırken eski geçmiş biçimi okunabilir kalır.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


class ReceiptStore:
    def __init__(self, project_root: str):
        self.dir = Path(project_root) / ".imece" / "receipts"

    def _path(self, receipt_id: str) -> Path:
        if not receipt_id or any(c not in "0123456789abcdef-" for c in receipt_id):
            raise ValueError("Geçersiz makbuz kimliği.")
        return self.dir / f"{receipt_id}.json"

    def create(self, receipt: dict) -> dict:
        now = time.time()
        data = {
            "id": str(uuid.uuid4()), "createdAt": now, "finishedAt": now,
            "status": "completed", "task": "", "routing": {}, "plan": None,
            "proposals": [], "review": {"verdict": "UNKNOWN", "note": ""},
            "metrics": {"latency_s": 0.0, "tokens": 0, "cost_usd": 0.0},
            "applied": [], "rejected": [], "checkpointId": None,
            "verification": {"status": "not_run", "detail": "Bu koşuda doğrulama komutu çalıştırılmadı."},
        }
        data.update(receipt)
        self._write(self._path(data["id"]), data)
        return data

    def get(self, receipt_id: str) -> dict:
        try:
            data = json.loads(self._path(receipt_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Makbuz bulunamadı veya bozuk.") from exc
        if not isinstance(data, dict):
            raise ValueError("Makbuz geçersiz.")
        return data

    def update(self, receipt_id: str, patch: dict) -> dict:
        data = self.get(receipt_id)
        data.update(patch)
        self._write(self._path(receipt_id), data)
        return data

    def export_markdown(self, receipt_id: str, directory: str) -> Path:
        data = self.get(receipt_id)
        target_dir = Path(directory).expanduser()
        if not target_dir.is_dir():
            raise ValueError("Dışa aktarma klasörü bulunamadı.")
        target = target_dir / f"imece-receipt-{receipt_id}.md"
        plan = data.get("plan") or {}
        review = data.get("review") or {}
        metrics = data.get("metrics") or {}
        scope = [f"- `{path}`" for path in plan.get("files", [])] or ["- Dosya seçilmedi."]
        applied = [f"- `{path}`" for path in data.get("applied", [])] or ["- Değişiklik uygulanmadı."]
        lines = [
            "# Imece IDE değişiklik makbuzu", "",
            f"- Kimlik: `{receipt_id}`", f"- Durum: **{data.get('status', 'unknown')}**",
            f"- Görev: {data.get('task', '')}", f"- Karar: {review.get('verdict', 'UNKNOWN')}",
            f"- Maliyet: ${float(metrics.get('cost_usd', 0)):.5f} · {metrics.get('tokens', 0)} token · {metrics.get('latency_s', 0)} sn", "",
            "## Plan", plan.get("summary") or "Plan özeti yok.", "",
            "## Kapsam", *scope, "",
            "## İnceleme", review.get("note") or "Reviewer notu yok.", "",
            "## Uygulama", *applied, "",
            "## Doğrulama", data.get("verification", {}).get("detail", "Çalıştırılmadı."), "",
            "## Diff", "",
        ]
        for proposal in data.get("proposals", []):
            lines.extend([f"### `{proposal.get('path', '')}`", "```diff", proposal.get("diff", ""), "```", ""])
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
