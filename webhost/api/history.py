"""history.* / receipt.* — geçmiş & makbuz köprüsü.

2G (kanonik read model göçü): Yazma-tarafı kaynağı artık YALNIZCA kanonik run
runtime'dır (tasks/runs/run_events); receipts.py/history.py legacy JSON
depoları KANONİK-ÖNCESİ verileri okumak için burada kalır — yeni bir kanonik
Run onlara ASLA YAZMAZ (bkz. webhost/api/run.py, run_runtime.readmodels).

Makbuz (receipt) kimlik yönlendirmesi:

    "run_" ile BAŞLAYAN kimlikler KANONİKTİR -> RunReadService kullanılır.
    Kanonik bir "run_..." kimliği okunamazsa legacy'e DÜŞÜLMEZ (bkz.
    run_runtime.readmodels + BridgeError ayrımı: not_found vs. store hatası).

    Diğer (UUID) kimlikler kanonik-ÖNCESİ legacy ReceiptStore ile okunur.

history.list, kanonik proje geçmişini (RunReadService, project_root ile
kapsamlı) legacy history.json ile DETERMİNİSTİK bir göç sınırıyla birleştirir
(bkz. run_runtime.readmodels.merge_canonical_and_legacy_history) — projedeki
İLK kanonik Run, doğal bir yetki devri (authority cutover) noktasıdır; 2F'nin
çift-yazılmış legacy girdileri asla kanonik girdilerle YİNELENMEZ.
"""

from pathlib import Path

from history import HistoryStore
from receipts import ReceiptStore
from run_runtime.errors import RunNotFoundError, RunRuntimeError
from run_runtime.readmodels import (
    HISTORY_MAX_ITEMS,
    RunReadService,
    merge_canonical_and_legacy_history,
    render_receipt_markdown,
)
from webhost import state
from webhost.bridge import handler, BridgeError


def _require_project():
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


def _read_service() -> RunReadService:
    return RunReadService(state.get_run_runtime().store)


@handler("history.list")
def _list(params, ctx):
    proj = _require_project()
    try:
        canonical_items = _read_service().list_history(
            project_root=proj.root, limit=HISTORY_MAX_ITEMS,
        )
    except RunRuntimeError as exc:
        # Kanonik okuma BAŞARISIZ oluşu, "kanonik geçmiş yokmuş gibi" legacy'e
        # sessizce düşülecek bir durum DEĞİLDİR — hata AÇIKÇA raporlanır.
        raise BridgeError("canonical_read_failed", f"Kanonik geçmiş okunamadı: {exc}") from exc
    legacy_items = HistoryStore(proj.root).all()
    items = merge_canonical_and_legacy_history(canonical_items, legacy_items, limit=HISTORY_MAX_ITEMS)
    return {"items": items}


@handler("receipt.get")
def _receipt_get(params, ctx):
    proj = _require_project()
    receipt_id = params.get("receiptId", "")
    if receipt_id.startswith("run_"):
        try:
            receipt = _read_service().get_receipt(receipt_id, project_root=proj.root)
        except RunNotFoundError as exc:
            raise BridgeError("not_found", "Makbuz bulunamadı.") from exc
        except RunRuntimeError as exc:
            raise BridgeError("canonical_read_failed", f"Kanonik makbuz okunamadı: {exc}") from exc
        return {"receipt": receipt}
    try:
        return {"receipt": ReceiptStore(proj.root).get(receipt_id)}
    except ValueError as exc:
        raise BridgeError("not_found", str(exc)) from exc


@handler("receipt.export")
def _receipt_export(params, ctx):
    proj = _require_project()
    receipt_id = params.get("receiptId", "")
    directory = params.get("directory", "")

    if receipt_id.startswith("run_"):
        try:
            receipt = _read_service().get_receipt(receipt_id, project_root=proj.root)
        except RunNotFoundError as exc:
            raise BridgeError("export_failed", "Makbuz bulunamadı.") from exc
        except RunRuntimeError as exc:
            raise BridgeError("canonical_read_failed", f"Kanonik makbuz okunamadı: {exc}") from exc

        target_dir = Path(directory).expanduser()
        if not target_dir.is_dir():
            raise BridgeError("export_failed", "Dışa aktarma klasörü bulunamadı.")
        target = target_dir / f"imece-receipt-{receipt_id}.md"
        try:
            target.write_text(render_receipt_markdown(receipt), encoding="utf-8")
        except OSError as exc:
            raise BridgeError("export_failed", str(exc)) from exc
        return {"path": str(target)}

    try:
        path = ReceiptStore(proj.root).export_markdown(receipt_id, directory)
        return {"path": str(path)}
    except ValueError as exc:
        raise BridgeError("export_failed", str(exc)) from exc
