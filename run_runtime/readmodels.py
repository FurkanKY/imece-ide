"""run_runtime.readmodels — kanonik Task/Run/Event verisinden türetilen READ MODEL'ler.

Bu modül webhost/Qt BİLMEZ ve YALNIZCA OKUR — run_events'e hiçbir zaman YAZMAZ.
Frontend-uyumlu Receipt/HistoryItem sözlükleri, `tasks`/`runs`/`run_events`
akışından HER SEFERİNDE deterministik biçimde YENİDEN İNŞA edilir. Kalıcı
ikinci bir "run_history"/"run_receipts"/"summaries" tablosu YOKTUR ve bu
milestonda EKLENMEZ (bkz. proje ADR notu) — `runs` tablosu zaten sıcak
toplamları (status/phase/tokens/cost_usd/latency_s/...) atomik olarak taşır;
anlamsal (semantic) alanlar (plan/review/proposal/apply/reject) run_events'ten
okunur. Profil kanıtlamadıkça tek edinilebilir (disposable) bir önbellek dahi
BU MİLESTONDA eklenmez.

Kanonik makbuz (receipt) kimliği doğrudan run_id'dir (örn. "run_<uuid>") —
ayrı bir UUID ÜRETİLMEZ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from run_runtime.errors import RunNotFoundError, RunStoreError
from run_runtime.events import RunEvent, RunEventType
from run_runtime.models import RunPhase, RunRecord, RunStatus, TaskRecord
from run_runtime.store import MAX_EVENT_PAGE_LIMIT, RunStore

HISTORY_MAX_ITEMS = 100

_VERIFICATION_NOT_RUN: dict[str, Any] = {
    "status": "not_run",
    "detail": "Bu koşuda doğrulama komutu çalıştırılmadı.",
}


@dataclass(frozen=True, slots=True)
class RunReadSnapshot:
    """Bir Receipt/HistoryItem inşa etmek için gereken TÜM kanonik girdilerin
    değişmez bir anlık görüntüsü."""

    task: TaskRecord
    run: RunRecord
    events: tuple[RunEvent, ...]


def _to_epoch_seconds(dt: datetime | None) -> float | None:
    return None if dt is None else dt.timestamp()


def load_full_event_history(
    store: RunStore,
    run_id: str,
    *,
    page_size: int = MAX_EVENT_PAGE_LIMIT,
    through_seq: int | None = None,
) -> tuple[RunEvent, ...]:
    """`run_id` için dayanıklı event geçmişini seq sırasıyla okur.

    store.events() SAYFALIDIR — TEK bir sayfanın eksiksiz olduğu ASLA
    VARSAYILMAZ. Sıralama YALNIZCA (run_id, seq) çiftinden gelir — zaman
    damgası veya event UUID'i kullanılmaz. `has_more=True` ama sayfa boşsa
    (ilerlemeyen imleç), sonsuz döngüye girmemek için tipli bir RunStoreError
    ile durulur.

    `through_seq=None` (varsayılan): O ANDA dayanıklı olan TÜM event'ler
    okunur (`has_more=False` olana kadar).

    `through_seq=N`: TAM OLARAK 1..N arası dayanıklı önek (prefix) döndürülür
    — N+1 veya sonrası ASLA dahil edilmez. Bu, bir RunReadSnapshot'ın
    RunRecord.last_event_seq ile SIKI biçimde sınırlandırılmasını sağlar
    (bkz. RunReadService.load_snapshot): başka bir thread, RunRecord
    okunduktan SONRA event eklese bile, o yeni event'ler bu okumaya ASLA
    sızmaz. `store.events()` her sorguda `limit = min(page_size, N - after_seq)`
    ile ÇAĞRILIR, böylece hiçbir sayfa N'i AŞAN bir event getiremez.
    `has_more=True` olsa bile (N'in ÖTESİNDE event'ler var demektir), N'e
    ULAŞILDIĞINDA döngü DURUR — bunlar hiç sorgulanmaz/okunmaz bile.

    N > 0 iken akış N'e ulaşmadan biterse (dayanıklı geçmiş N'den kısa) veya
    döndürülen son seq N'e eşit değilse, sessizce budanmış/tutarsız bir önek
    döndürmek yerine tipli bir RunStoreError fırlatılır.
    """
    if through_seq is not None:
        if isinstance(through_seq, bool) or not isinstance(through_seq, int):
            raise RunStoreError(f"through_seq bir tam sayı olmalı: {through_seq!r}")
        if through_seq < 0:
            raise RunStoreError(f"through_seq >= 0 olmalı: {through_seq}")
        if through_seq == 0:
            return ()

    events: list[RunEvent] = []
    after_seq = 0
    while True:
        if through_seq is not None:
            limit = min(page_size, through_seq - after_seq)
        else:
            limit = page_size

        page = store.events(run_id, after_seq=after_seq, limit=limit)
        if page.has_more and not page.events:
            raise RunStoreError(
                f"Event sayfalama tutarsız: run_id={run_id} after_seq={after_seq} "
                "boş sayfa ile has_more=True bildirildi."
            )
        events.extend(page.events)
        if page.events:
            after_seq = page.events[-1].seq

        if through_seq is not None:
            if after_seq >= through_seq:
                break
            if not page.has_more:
                raise RunStoreError(
                    f"through_seq={through_seq}, dayanıklı akıştan (son seq={after_seq}) "
                    f"daha ileride: run_id={run_id}"
                )
            continue

        if not page.has_more:
            break

    if through_seq is not None:
        final_seq = events[-1].seq if events else 0
        if final_seq != through_seq:
            raise RunStoreError(
                f"Döndürülen son seq ({final_seq}), through_seq ({through_seq}) ile "
                f"eşleşmiyor: run_id={run_id} — budanmış/tutarsız bir önek döndürülmedi."
            )
    return tuple(events)


def canonical_status_string(run: RunRecord) -> str:
    """RunStatus/RunPhase'i mevcut frontend-uyumlu Receipt/HistoryItem durum
    dizgesine çevirir (legacy UI bildirimlerinden veya receipt JSON'undan
    ASLA çıkarılmaz — yalnızca kanonik RunRecord'dan)."""
    if run.status == RunStatus.CREATED:
        return "created"
    if run.status == RunStatus.RUNNING:
        return "running"
    if run.status == RunStatus.WAITING_USER:
        return "proposed"
    if run.status == RunStatus.SUCCEEDED:
        if run.phase == RunPhase.APPLIED:
            return "applied"
        if run.phase == RunPhase.REJECTED:
            return "rejected"
        return "completed"
    if run.status == RunStatus.FAILED:
        return "failed"
    if run.status == RunStatus.CANCELLED:
        return "cancelled"
    if run.status == RunStatus.INTERRUPTED:
        return "interrupted"
    return str(run.status)  # ileriye dönük savunma; bilinen enum ile erişilemez


def _latest(events: tuple[RunEvent, ...], event_type: str) -> RunEvent | None:
    for event in reversed(events):
        if event.type == event_type:
            return event
    return None


def _draft_change_summary(events: tuple[RunEvent, ...]) -> list[dict[str, Any]]:
    """proposal.ready HENÜZ yokken change.proposed event'lerinden ilerlemeli
    (progressive) bir taslak üretir. Aynı path tekrar ederse EN SON hali
    kazanır; path'in akıştaki İLK görüldüğü sıra korunur."""
    order: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != RunEventType.CHANGE_PROPOSED:
            continue
        path = event.payload.get("path", "")
        if path not in by_path:
            order.append(path)
        by_path[path] = {
            "path": path,
            "is_new": bool(event.payload.get("is_new")),
            "diff": event.payload.get("diff", ""),
        }
    return [by_path[p] for p in order]


def build_receipt(snapshot: RunReadSnapshot) -> dict[str, Any]:
    """Mevcut frontend Receipt sözlük şeklini (receipts.py ile AYNI) kanonik
    veriden inşa eder. SAF'tır — hiçbir I/O yapmaz, hiçbir event'i MUTASYONA
    UĞRATMAZ. Bilinmeyen/gelecekteki event türleri, aranmadıkları için
    projeksiyonu SESSİZCE etkilemez (kırılma YOK)."""
    run = snapshot.run
    task = snapshot.task
    events = snapshot.events

    plan_event = _latest(events, RunEventType.PLAN_COMPLETED)
    plan = None
    if plan_event is not None:
        plan = {
            "summary": plan_event.payload.get("summary", ""),
            "files": list(plan_event.payload.get("files", [])),
        }

    review_event = _latest(events, RunEventType.REVIEW_COMPLETED)
    review = {"verdict": "UNKNOWN", "note": ""}
    if review_event is not None:
        review = {
            "verdict": review_event.payload.get("verdict", "UNKNOWN"),
            "note": review_event.payload.get("note", ""),
        }

    proposal_ready = _latest(events, RunEventType.PROPOSAL_READY)
    if proposal_ready is not None:
        # Frontend'e YALNIZCA path/is_new/diff sızar — tam 'new' içeriği
        # kasıtlı olarak DIŞARIDA bırakılır (bkz. modül üstü ADR notu);
        # tam içerik yalnızca kanonik run_events'te kalır.
        proposals = [
            {"path": p.get("path", ""), "is_new": bool(p.get("is_new")), "diff": p.get("diff", "")}
            for p in proposal_ready.payload.get("proposals", [])
        ]
    else:
        proposals = _draft_change_summary(events)

    applied_event = _latest(events, RunEventType.PROPOSAL_APPLIED)
    applied = list(applied_event.payload.get("applied", [])) if applied_event is not None else []
    checkpoint_id = applied_event.payload.get("checkpoint_id") if applied_event is not None else None

    rejected_event = _latest(events, RunEventType.PROPOSAL_REJECTED)
    rejected = list(rejected_event.payload.get("rejected", [])) if rejected_event is not None else []

    finished_at = run.finished_at
    if finished_at is None and events:
        finished_at = events[-1].created_at
    if finished_at is None:
        finished_at = run.started_at
    if finished_at is None:
        finished_at = run.created_at

    receipt: dict[str, Any] = {
        "id": run.run_id,
        "createdAt": _to_epoch_seconds(run.created_at),
        "finishedAt": _to_epoch_seconds(finished_at),
        "status": canonical_status_string(run),
        "task": task.prompt,
        "routing": dict(run.routing),
        "plan": plan,
        "proposals": proposals,
        "review": review,
        "metrics": {
            "latency_s": run.latency_s,
            "tokens": run.total_tokens,
            "cost_usd": run.cost_usd,
        },
        "applied": applied,
        "rejected": rejected,
        "checkpointId": checkpoint_id,
        "verification": dict(_VERIFICATION_NOT_RUN),
    }
    if run.error_message:
        receipt["error"] = run.error_message
    return receipt


def build_history_item(snapshot: RunReadSnapshot) -> dict[str, Any]:
    """Mevcut frontend HistoryItem sözlük şeklini kanonik veriden inşa eder. SAF'tır."""
    run = snapshot.run
    task = snapshot.task
    events = snapshot.events

    review_event = _latest(events, RunEventType.REVIEW_COMPLETED)
    verdict = review_event.payload.get("verdict", "UNKNOWN") if review_event is not None else "UNKNOWN"

    proposal_ready = _latest(events, RunEventType.PROPOSAL_READY)
    if proposal_ready is not None:
        files = [p.get("path", "") for p in proposal_ready.payload.get("proposals", [])]
    else:
        plan_event = _latest(events, RunEventType.PLAN_COMPLETED)
        files = list(plan_event.payload.get("files", [])) if plan_event is not None else []

    return {
        "ts": _to_epoch_seconds(run.created_at),
        "task": task.prompt,
        "verdict": verdict,
        "tokens": run.total_tokens,
        "cost_usd": run.cost_usd,
        "files": files,
        "receipt_id": run.run_id,
        "status": canonical_status_string(run),
    }


def render_receipt_markdown(receipt: dict[str, Any]) -> str:
    """Bir Receipt sözlüğünden (kanonik VEYA legacy — ikisi de AYNI şekle
    sahip) Markdown metni üretir. SAF'tır: hiçbir dosyaya YAZMAZ. Dosya
    sistemi çıktısı yalnızca açık export işleminde (webhost/api/history.py)
    gerçekleşir — bu, mevcut kullanıcıya dönük bölümleri korur: kimlik/durum/
    görev, maliyet/token/gecikme, plan, kapsam, inceleme, uygulanan dosyalar,
    doğrulama, diff'ler."""
    plan = receipt.get("plan") or {}
    review = receipt.get("review") or {}
    metrics = receipt.get("metrics") or {}
    scope = [f"- `{path}`" for path in plan.get("files", [])] or ["- Dosya seçilmedi."]
    applied = [f"- `{path}`" for path in receipt.get("applied", [])] or ["- Değişiklik uygulanmadı."]
    lines = [
        "# Imece IDE değişiklik makbuzu", "",
        f"- Kimlik: `{receipt.get('id', '')}`", f"- Durum: **{receipt.get('status', 'unknown')}**",
        f"- Görev: {receipt.get('task', '')}", f"- Karar: {review.get('verdict', 'UNKNOWN')}",
        f"- Maliyet: ${float(metrics.get('cost_usd', 0)):.5f} · {metrics.get('tokens', 0)} token · "
        f"{metrics.get('latency_s', 0)} sn", "",
        "## Plan", plan.get("summary") or "Plan özeti yok.", "",
        "## Kapsam", *scope, "",
        "## İnceleme", review.get("note") or "Reviewer notu yok.", "",
        "## Uygulama", *applied, "",
        "## Doğrulama", (receipt.get("verification") or {}).get("detail", "Çalıştırılmadı."), "",
        "## Diff", "",
    ]
    for proposal in receipt.get("proposals", []):
        lines.extend([f"### `{proposal.get('path', '')}`", "```diff", proposal.get("diff", ""), "```", ""])
    return "\n".join(lines)


def _is_valid_legacy_history_item(item: Any) -> bool:
    """Legacy history.json yalnızca ÜST DÜZEY değerin bir liste olduğunu
    GARANTİ EDER (bkz. HistoryStore.all) — ÖĞELER bozuk olabilir: null,
    "text", {}, {"ts": "bad"} gibi. Yalnızca kullanılabilir SAYISAL bir 'ts'
    taşıyan dict'ler kabul edilir; bool bir 'ts' (True/False) de reddedilir
    (bool, int'in alt sınıfıdır)."""
    if not isinstance(item, dict):
        return False
    ts = item.get("ts")
    return isinstance(ts, (int, float)) and not isinstance(ts, bool)


def merge_canonical_and_legacy_history(
    canonical_items: list[dict[str, Any]],
    legacy_items: list[dict[str, Any]],
    *,
    limit: int = HISTORY_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Kanonik + legacy geçmiş öğelerini DETERMİNİSTİK bir göç (migration)
    sınırıyla birleştirir. SAF'tır (I/O yapmaz, legacy öğeleri MUTASYONA
    UĞRATMAZ).

    `canonical_items`'ın zaten ts DESC sıralı, kesinlikle geçerli ve en fazla
    `limit` öğe olduğu varsayılır (bkz. RunReadService.list_history) —
    kanonik öğeler HER ZAMAN katıdır (strict), gevşetilmez.

    `legacy_items` uyumluluk verisidir ve BOZUK olabilir: null/"text"/{}/
    {"ts": "bad"} gibi kullanılamaz öğeler, karşılaştırma/birleştirmeden ÖNCE
    SESSİZCE ATLANIR (yalnızca kullanılabilir sayısal bir 'ts' taşıyan dict
    öğeler değerlendirmeye alınır) — bozuk bir legacy girdisi canonical
    history.list'i ASLA ÇÖKERTMEZ.

    Kural:
        canonical_items sayısı >= limit  -> yalnızca canonical_items[:limit]
        canonical_items boş              -> yalnızca (geçerli) legacy_items[:limit]
        aksi halde:
            canonical_cutoff = min(canonical_items ts)
            legacy_precanonical = ts'i canonical_cutoff'tan KESİNLİKLE daha
                                  eski olan (geçerli) legacy öğeler
            (canonical + legacy_precanonical) ts DESC sıralanır, limit'e kesilir

    Bulanık (fuzzy) görev/zaman damgası eşleştirmesiyle YİNELEME AYIKLAMA
    YAPILMAZ — sınır YALNIZCA kesin ts karşılaştırmasıyla belirlenir. Bu,
    projedeki İLK kanonik Run'ı doğal bir "yetki devri" (authority cutover)
    noktası yapar ve 2F'nin çift-yazılmış geçmiş kayıtlarının YİNELENMESİNİ
    kasıtlı olarak önler.
    """
    legacy_items = [item for item in legacy_items if _is_valid_legacy_history_item(item)]

    if len(canonical_items) >= limit:
        return list(canonical_items[:limit])
    if not canonical_items:
        return list(legacy_items[:limit])

    canonical_cutoff = min(item["ts"] for item in canonical_items)
    legacy_precanonical = [item for item in legacy_items if item["ts"] < canonical_cutoff]

    merged = list(canonical_items) + legacy_precanonical
    merged.sort(key=lambda item: item.get("ts", 0), reverse=True)
    return merged[:limit]


class RunReadService:
    """SADECE OKUYAN kanonik Task/Run/Event servisi — Receipt/HistoryItem
    izdüşümlerini talep üzerine (on-demand) inşa eder. RunStore'a doğrudan
    bağımlı olabilir çünkü SALT OKUNURDUR (asla run_events'e yazmaz)."""

    def __init__(self, store: RunStore):
        self._store = store

    @property
    def store(self) -> RunStore:
        return self._store

    def _load_run_and_task(self, run_id: str) -> tuple[RunRecord, TaskRecord]:
        run = self._store.get_run(run_id)  # RunNotFoundError doğal olarak yükselir
        task = self._store.get_task(run.task_id)
        return run, task

    def load_snapshot(self, run_id: str) -> RunReadSnapshot:
        """run_id için TUTARLI bir anlık görüntü (snapshot) inşa eder.

        Event akışı, RunRecord'un KENDİ last_event_seq'i ile SINIRLANIR
        (through_seq) — böylece run.get_run() ile event okuması ARASINDA
        başka bir thread event eklese bile, o yeni event'ler bu snapshot'a
        ASLA sızmaz. İnvaryant: last_event_seq > 0 iken
        `snapshot.run.last_event_seq == snapshot.events[-1].seq`.
        """
        run, task = self._load_run_and_task(run_id)
        events = load_full_event_history(self._store, run_id, through_seq=run.last_event_seq)
        return RunReadSnapshot(task=task, run=run, events=events)

    def get_receipt(self, run_id: str, *, project_root: str) -> dict[str, Any]:
        """Belirtilen run_id için Receipt'i inşa eder.

        Proje sahipliği event AKIŞI YÜKLENMEDEN ÖNCE doğrulanır:
        TaskRecord.project_root, verilen `project_root` ile eşleşmezse
        store.events() HİÇ ÇAĞRILMADAN RunNotFoundError fırlatılır — başka
        bir projeye ait bir kanonik Run, aktif proje API'si üzerinden ASLA
        AÇIĞA ÇIKMAZ ('yok' gibi davranılır, gerçek sebep sızdırılmaz) ve
        yabancı/bozuk bir event akışı asla okunmaya ÇALIŞILMAZ.
        """
        run, task = self._load_run_and_task(run_id)
        if task.project_root != project_root:
            raise RunNotFoundError(run_id)
        events = load_full_event_history(self._store, run_id, through_seq=run.last_event_seq)
        return build_receipt(RunReadSnapshot(task=task, run=run, events=events))

    def list_history(
        self, *, project_root: str, limit: int = HISTORY_MAX_ITEMS
    ) -> list[dict[str, Any]]:
        """Verilen proje köküne ait koşuların HistoryItem listesini (en
        yeniden en eskiye) döndürür. CREATED/RUNNING koşular DA dahildir —
        bu KASITLIDIR: kanonik geçmiş, aktif/takılı kalmış koşuları da
        tamamlanmış olanlar kadar açığa çıkarmalıdır (run.finished UI
        bildirimlerinden TERMİNAL girdiler UYDURULMAZ)."""
        runs = self._store.list_runs(project_root=project_root, limit=limit)
        items: list[dict[str, Any]] = []
        for run in runs:
            task = self._store.get_task(run.task_id)
            events = load_full_event_history(self._store, run.run_id, through_seq=run.last_event_seq)
            items.append(build_history_item(RunReadSnapshot(task=task, run=run, events=events)))
        return items
