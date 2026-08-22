"""run_runtime.legacy — project_runner legacy generator <-> kanonik RunRuntime köprüsü.

Bu milestonda (2F) project_runner.py DEĞİŞTİRİLMEZ. project_runner hâlâ
sözlük (dict) tabanlı "legacy" event'ler üretir (stage/info/output/metric/
plan/diff/verdict/proposal). Bu modül, o legacy akışı kanonik run_events
geçmişine ÇEVİRİR:

    project_runner legacy dict
        -> LegacyEventAdapter.translate()   (saf, durumlu çeviri)
        -> CanonicalEventSpec listesi       (henüz kalıcı DEĞİL)
        -> LegacyRunCoordinator             (yaşam döngüsü + RunRuntime.record())
        -> RunRuntime.record()              (SQLite'a COMMIT + canlı bildirim)

Yalnızca bir legacy event'ten türeyen TÜM kanonik event'ler dayanıklı biçimde
kaydedildikten SONRA webhost/api/run.py orijinal legacy event'i `run.event`
kanalına iletebilir (bkz. webhost/api/run.py on_event sıralaması). Mevcut
React arayüzü bu geçişten TAMAMEN habersizdir; frontend hâlâ yalnızca ham
legacy event sözlüklerini görür.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agents import DEFAULT_ROUTING

from run_runtime.errors import InvalidRunStateError
from run_runtime.events import RunEvent, RunEventType
from run_runtime.models import RunRecord, RunStatus, new_execution_id
from run_runtime.service import RunRuntime

LEGACY_SOURCE = "legacy.project_runner"
LIFECYCLE_SOURCE = "webhost.run"

_TERMINAL_STATUSES = frozenset({
    RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED,
})

# Legacy 'stage' adı -> (kanonik RunPhase değeri, kanonik rol).
_STAGE_PHASE = {"plan": "planning", "code": "executing", "review": "reviewing"}
_STAGE_ROLE = {"plan": "planner", "code": "worker", "review": "reviewer"}


@dataclass(frozen=True, slots=True)
class CanonicalEventSpec:
    """Bir kanonik event NİYETİ — henüz RunRuntime.record() ile YAZILMAMIŞ.

    Kalıcı RunEvent'i YİNELEMEZ (event_id/seq/schema_version/created_at gibi
    yalnızca kalıcılık katmanının atayabileceği alanları taşımaz).
    """

    type: str
    payload: dict[str, Any]
    execution_id: str | None = None
    source: str = LEGACY_SOURCE


class LegacyEventAdapter:
    """project_runner'ın legacy event sözlüklerini CanonicalEventSpec listesine çevirir.

    translate(), dahili durum (adapter state) AÇISINDAN SAFTIR: yalnızca şu anki
    durumu OKUR, hiçbir şeyi YAZMAZ/İLERLETMEZ. Dönen CanonicalEventSpec'ler
    henüz kalıcı DEĞİLDİR. Durum yalnızca commit_recorded(spec) ile, İLGİLİ
    spec RunRuntime.record() ile DAYANIKLI biçimde COMMIT olduktan SONRA
    ilerletilir (bkz. LegacyRunCoordinator.handle_legacy_event). Bu ayrım
    kritiktir: çok-spec'li bir çeviri kısmen başarısız olursa (örn. 3 spec'ten
    2'si commit olur, 3.'sü başarısız olur), adapter durumu YALNIZCA gerçekten
    commit olan ÖN EK ile tutarlı kalır — hiçbir zaman henüz yazılmamış bir
    execution'ı "açık" saymaz.

    RunStore/RunRuntime BİLMEZ (I/O yapmaz). Girdi (legacy) sözlükleri ASLA
    MUTASYONA UĞRATILMAZ.
    """

    def __init__(self) -> None:
        self._stage: str | None = None
        self._execution_id: str | None = None

    @property
    def current_execution_id(self) -> str | None:
        return self._execution_id

    def mark_execution_closed(self, execution_id: str | None) -> None:
        """`execution_id`, dahili durumdaki AÇIK execution ile eşleşiyorsa temizler.

        Yalnızca ilgili execution.completed/execution.failed event'i
        DAYANIKLI biçimde COMMIT olduktan SONRA çağrılmalıdır — asla önce.
        """
        if execution_id is not None and execution_id == self._execution_id:
            self._execution_id = None

    def commit_recorded(self, spec: CanonicalEventSpec) -> None:
        """`spec` RunRuntime.record() ile DAYANIKLI biçimde COMMIT olduktan SONRA çağrılır.

        Adapter durumunu YALNIZCA bu noktada ilerletir:

        - execution.completed / execution.failed: eşleşen açık execution'ı kapatır.
        - execution.started: yeni execution_id'yi ve legacy_stage'i BENİMSER.
        - diğer tüm türler adapter durumunu ETKİLEMEZ (proposal.ready dahil —
          önceki execution zaten kendi execution.completed spec'i commit
          olduğunda kapatılmıştır; burada YENİ bir execution durumu İCAT EDİLMEZ).
        """
        if spec.type in (RunEventType.EXECUTION_COMPLETED, RunEventType.EXECUTION_FAILED):
            self.mark_execution_closed(spec.execution_id)
        elif spec.type == RunEventType.EXECUTION_STARTED:
            self._execution_id = spec.execution_id
            self._stage = spec.payload.get("legacy_stage")

    def translate(self, legacy_event: dict[str, Any]) -> list[CanonicalEventSpec]:
        handler = _HANDLERS.get(legacy_event.get("type"))
        if handler is None:
            return [self._unknown(legacy_event)]
        return handler(self, legacy_event)

    # ---------------- yardımcılar (SAF — okur, YAZMAZ) ----------------

    def _unknown(self, legacy_event: dict[str, Any]) -> CanonicalEventSpec:
        return CanonicalEventSpec(
            type="legacy.event",
            payload={"event": dict(legacy_event)},
            execution_id=self._execution_id,
            source=LEGACY_SOURCE,
        )

    def _current_close_spec(self) -> CanonicalEventSpec | None:
        """Açık bir execution varsa, onu kapatacak spec'i üretir (durumu DEĞİŞTİRMEZ)."""
        if self._execution_id is None:
            return None
        return CanonicalEventSpec(
            type=RunEventType.EXECUTION_COMPLETED, payload={},
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )

    # ---------------- legacy event türü işleyicileri (SAF) ----------------

    def _on_stage(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        stage = ev.get("stage")
        if stage not in _STAGE_PHASE:
            return [self._unknown(ev)]

        specs: list[CanonicalEventSpec] = []
        closing = self._current_close_spec()
        if closing is not None:
            specs.append(closing)

        specs.append(CanonicalEventSpec(
            type=RunEventType.RUN_PHASE_CHANGED,
            payload={"phase": _STAGE_PHASE[stage]},
            execution_id=None, source=LEGACY_SOURCE,
        ))

        new_exec_id = new_execution_id()
        role = _STAGE_ROLE[stage]
        exec_payload: dict[str, Any] = {
            "role": role, "legacy_stage": stage, "provider": ev.get("provider"),
        }
        if stage == "code":
            # Uzun vadeli kanonik rol 'worker'dır; 'coder' yalnızca uyumluluk
            # meta verisi olarak korunur.
            exec_payload["legacy_role"] = "coder"
        specs.append(CanonicalEventSpec(
            type=RunEventType.EXECUTION_STARTED, payload=exec_payload,
            execution_id=new_exec_id, source=LEGACY_SOURCE,
        ))
        return specs

    def _on_info(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        return [CanonicalEventSpec(
            type=RunEventType.EXECUTION_OUTPUT,
            payload={"kind": "info", "text": ev.get("text", ""), "legacy_stage": self._stage},
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_output(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        return [CanonicalEventSpec(
            type=RunEventType.EXECUTION_OUTPUT,
            payload={
                "kind": "model_output", "text": ev.get("text", ""),
                "legacy_stage": ev.get("stage", self._stage),
            },
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_metric(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        # Legacy metric yalnızca TEK bir toplam 'tokens' alanı taşır (prompt/
        # completion ayrımı yok); bu yüzden total_tokens'a eşlenir,
        # prompt_tokens/completion_tokens'a DOKUNULMAZ (varsayılan 0 kalır) —
        # aksi halde projector'da çift sayım olurdu.
        payload = {
            "total_tokens": int(ev.get("tokens", 0)),
            "cost_usd": float(ev.get("cost_usd", 0.0)),
            "latency_s": float(ev.get("latency_s", 0.0)),
            "provider": ev.get("provider"),
            "model": ev.get("model"),
            "legacy_stage": ev.get("stage", self._stage),
        }
        return [CanonicalEventSpec(
            type=RunEventType.USAGE_RECORDED, payload=payload,
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_plan(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        return [CanonicalEventSpec(
            type=RunEventType.PLAN_COMPLETED,
            payload={"summary": ev.get("summary", ""), "files": list(ev.get("files") or [])},
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_diff(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        return [CanonicalEventSpec(
            type=RunEventType.CHANGE_PROPOSED,
            payload={
                "path": ev.get("path", ""), "is_new": bool(ev.get("is_new")),
                "diff": ev.get("diff", ""),
            },
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_verdict(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        return [CanonicalEventSpec(
            type=RunEventType.REVIEW_COMPLETED,
            payload={"verdict": ev.get("verdict", "UNKNOWN"), "note": ev.get("note", "")},
            execution_id=self._execution_id, source=LEGACY_SOURCE,
        )]

    def _on_proposal(self, ev: dict[str, Any]) -> list[CanonicalEventSpec]:
        specs: list[CanonicalEventSpec] = []
        closing = self._current_close_spec()
        if closing is not None:
            specs.append(closing)

        proposals = [
            {
                "path": p.get("path", ""),
                "new": p.get("new", ""),
                "diff": p.get("diff", ""),
                "is_new": bool(p.get("is_new")),
            }
            for p in (ev.get("proposals") or [])
        ]
        specs.append(CanonicalEventSpec(
            type=RunEventType.PROPOSAL_READY,
            payload={
                "proposals": proposals,
                "totals": dict(ev.get("totals") or {}),
                "verdict": ev.get("verdict"),
            },
            execution_id=None, source=LEGACY_SOURCE,
        ))
        if proposals:
            specs.append(CanonicalEventSpec(
                type=RunEventType.RUN_WAITING_USER, payload={},
                execution_id=None, source=LEGACY_SOURCE,
            ))
        return specs


_HANDLERS: dict[str, Callable[[LegacyEventAdapter, dict[str, Any]], list[CanonicalEventSpec]]] = {
    "stage": LegacyEventAdapter._on_stage,
    "info": LegacyEventAdapter._on_info,
    "output": LegacyEventAdapter._on_output,
    "metric": LegacyEventAdapter._on_metric,
    "plan": LegacyEventAdapter._on_plan,
    "diff": LegacyEventAdapter._on_diff,
    "verdict": LegacyEventAdapter._on_verdict,
    "proposal": LegacyEventAdapter._on_proposal,
}


class LegacyRunCoordinator:
    """RunRuntime etrafındaki, project_runner'ın legacy akışını kanonik
    Task/Run/Event yaşam döngüsüne bağlayan test edilebilir köprü.

    task_id/run_id/adapter'ı KENDİ ÜZERİNDE tutar; webhost/api/run.py bu
    sınıfı doğrudan somutlaştırmak yerine LegacyRunCoordinator.start(...)
    fabrika metoduyla oluşturmalıdır.
    """

    def __init__(self, runtime: RunRuntime, *, task_id: str, run_id: str, routing: dict[str, Any]):
        self._runtime = runtime
        self.task_id = task_id
        self.run_id = run_id
        self.routing = routing
        self._adapter = LegacyEventAdapter()

    @classmethod
    def start(
        cls, runtime: RunRuntime, *, project_root: str, task: str, routing: dict[str, Any] | None = None,
    ) -> "LegacyRunCoordinator":
        """Yeni bir Task/Run oluşturur ve run.created + run.started'ı kaydeder.

        RunRecord.routing, build_agents'ın FİİLEN kullanacağı EFEKTİF routing'i
        saklar (DEFAULT_ROUTING + kullanıcı override'ları) — kullanıcı hiçbir
        override vermese bile yalnızca kısmi/boş bir dict SAKLANMAZ.

        Task/Run OLUŞTURULDUKTAN SONRA run.created/run.started kalıcılığı
        başarısız olursa: en iyi çaba bir run.failed yerleştirmesi denenir
        (mağaza hâlâ yazılabilirse) ve ORİJİNAL hata AYNEN yukarı fırlatılır.
        Kanonik geçmiş SİLİNMEZ — başarısız bir başlangıcı gizlemek için asla
        bir kayıt yok sayılmaz/silinmez.
        """
        effective_routing = {**DEFAULT_ROUTING, **(routing or {})}
        task_record = runtime.create_task(project_root=project_root, prompt=task)
        run_record = runtime.create_run(task_id=task_record.task_id, routing=effective_routing)

        coordinator = cls(
            runtime, task_id=task_record.task_id, run_id=run_record.run_id,
            routing=dict(run_record.routing),
        )
        try:
            runtime.record(
                run_id=coordinator.run_id, type=RunEventType.RUN_CREATED, payload={},
                source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
            )
            runtime.record(
                run_id=coordinator.run_id, type=RunEventType.RUN_STARTED, payload={},
                source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
            )
        except Exception:
            try:
                runtime.record(
                    run_id=coordinator.run_id, type=RunEventType.RUN_FAILED,
                    payload={
                        "error_code": "legacy_lifecycle_start_failed",
                        "error_message": "Run yaşam döngüsü başlatma (run.created/run.started) başarısız.",
                    },
                    source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
                )
            except Exception:
                pass  # en iyi çaba; mağaza da yazılamıyor olabilir
            raise
        return coordinator

    # ---------------- legacy event akışı ----------------

    def handle_legacy_event(self, legacy_event: dict[str, Any]) -> list[RunEvent]:
        """Bir legacy event'i çevirir ve türeyen TÜM kanonik event'leri sırayla kaydeder.

        Her spec için: ÖNCE runtime.record() (dayanıklı COMMIT), YALNIZCA
        BAŞARILI olursa adapter.commit_recorded(spec) (durum ilerletme).
        Bir runtime.record() çağrısı başarısız olursa istisna AYNEN yukarı
        fırlatılır (yutulmaz) VE o spec için commit_recorded ÇAĞRILMAZ —
        adapter durumu, gerçekten commit olan ÖN EKLE tutarlı kalır. Çağıran
        (webhost/api/run.py) bunu "kanonik kalıcılık başarısız" host hatası
        olarak ele almalıdır; bu durumda orijinal legacy event ASLA arayüze
        iletilmemelidir.
        """
        specs = self._adapter.translate(legacy_event)
        recorded: list[RunEvent] = []
        for spec in specs:
            event, _run = self._runtime.record(
                run_id=self.run_id, type=spec.type, payload=spec.payload,
                execution_id=spec.execution_id, source=spec.source,
                correlation_id=self.run_id,
            )
            self._adapter.commit_recorded(spec)
            recorded.append(event)
        return recorded

    # ---------------- worker sonlanma yerleşimi (settlement) ----------------

    def finish_normal(self) -> RunEvent | None:
        """NORMAL DONE: legacy generator hatasız/iptalsiz tükendi.

        Run hâlâ WAITING_USER ise (bir proposal.ready + run.waiting_user
        zaten kaydedildiyse) run.completed EKLENMEZ — mantıksal Run hâlâ
        Apply/Reject bekliyordur. Run hâlâ RUNNING ise (proposal ÜRETİLMEDİ)
        run.completed eklenir. Run zaten TERMİNAL bir durumdaysa (yinelenen
        Qt sinyali/doğrudan çağrı) İDEMPOTENT bir no-op'tur.
        """
        current = self._runtime.get_run(self.run_id)
        if current.status != RunStatus.RUNNING:
            return None
        event, _run = self._runtime.record(
            run_id=self.run_id, type=RunEventType.RUN_COMPLETED, payload={},
            source=LIFECYCLE_SOURCE, correlation_id=self.run_id,
            expected_last_event_seq=current.last_event_seq,
        )
        return event

    def finish_failed(self, message: str) -> RunEvent | None:
        """Run zaten TERMİNAL değilse: açık execution varsa ÖNCE execution.failed
        DAYANIKLI biçimde COMMIT edilir, adapter durumu YALNIZCA BUNDAN SONRA
        ilerletilir; sonra run.failed eklenir.

        execution.failed kalıcılığı BAŞARISIZ olursa istisna yukarı fırlatılır
        VE adapter'ın açık execution durumu SESSİZCE TEMİZLENMEZ — dayanıklı
        gerçeklik (execution hâlâ yerleşmemiş/settled değil) adapter'da
        doğru yansır. Run zaten terminalse İDEMPOTENT no-op (None) döner.
        """
        current = self._runtime.get_run(self.run_id)
        if current.status in _TERMINAL_STATUSES:
            return None
        expected_seq = current.last_event_seq

        execution_id = self._adapter.current_execution_id
        if execution_id is not None:
            _exec_event, run_after_exec = self._runtime.record(
                run_id=self.run_id, type=RunEventType.EXECUTION_FAILED,
                payload={"error_message": message},
                execution_id=execution_id, source=LIFECYCLE_SOURCE,
                correlation_id=self.run_id, expected_last_event_seq=expected_seq,
            )
            # BURAYA yalnızca execution.failed BAŞARIYLA commit olduysa ulaşılır.
            self._adapter.mark_execution_closed(execution_id)
            expected_seq = run_after_exec.last_event_seq

        event, _run = self._runtime.record(
            run_id=self.run_id, type=RunEventType.RUN_FAILED,
            payload={"error_code": "legacy_worker_error", "error_message": message},
            source=LIFECYCLE_SOURCE, correlation_id=self.run_id,
            expected_last_event_seq=expected_seq,
        )
        return event

    def finish_cancelled(self) -> RunEvent | None:
        """Run zaten TERMİNAL değilse: açık execution varsa ÖNCE iptal olarak
        yapılandırılmış execution.failed DAYANIKLI biçimde COMMIT edilir,
        adapter durumu YALNIZCA BUNDAN SONRA ilerletilir; sonra run.cancelled
        eklenir. Aynı hata-önce-commit-sonra-durum ilkesi finish_failed ile
        AYNIDIR. Run zaten terminalse İDEMPOTENT no-op (None) döner.
        """
        current = self._runtime.get_run(self.run_id)
        if current.status in _TERMINAL_STATUSES:
            return None
        expected_seq = current.last_event_seq

        execution_id = self._adapter.current_execution_id
        if execution_id is not None:
            _exec_event, run_after_exec = self._runtime.record(
                run_id=self.run_id, type=RunEventType.EXECUTION_FAILED,
                payload={"error_message": "cancelled", "cancelled": True},
                execution_id=execution_id, source=LIFECYCLE_SOURCE,
                correlation_id=self.run_id, expected_last_event_seq=expected_seq,
            )
            self._adapter.mark_execution_closed(execution_id)
            expected_seq = run_after_exec.last_event_seq

        event, _run = self._runtime.record(
            run_id=self.run_id, type=RunEventType.RUN_CANCELLED, payload={},
            source=LIFECYCLE_SOURCE, correlation_id=self.run_id,
            expected_last_event_seq=expected_seq,
        )
        return event

    # ---------------- Apply/Reject yerleşimi ----------------

    def record_proposal_applied(
        self, *, applied_paths: list[str], checkpoint_id: str,
    ) -> tuple[RunEvent, RunRecord]:
        """Run WAITING_USER DEĞİLSE reddeder (InvalidRunStateError) — yalnızca
        webhost'un bellek-içi `_active` durumuna GÜVENMEZ. expected_last_event_seq,
        aynı gözlenen sıradan başlayan iki eşzamanlı karardan (Apply/Reject
        yarışı) yalnızca birinin yerleşebilmesini SAĞLAR."""
        current = self._runtime.get_run(self.run_id)
        if current.status != RunStatus.WAITING_USER:
            raise InvalidRunStateError(
                f"Run WAITING_USER durumunda değil (status={current.status}); "
                "proposal.applied reddedildi."
            )
        return self._runtime.record(
            run_id=self.run_id, type=RunEventType.PROPOSAL_APPLIED,
            payload={"applied": list(applied_paths), "checkpoint_id": checkpoint_id},
            source=LIFECYCLE_SOURCE, correlation_id=self.run_id,
            expected_last_event_seq=current.last_event_seq,
        )

    def record_proposal_rejected(self, *, rejected_paths: list[str]) -> tuple[RunEvent, RunRecord]:
        """record_proposal_applied ile AYNI durum/sıra korumasını uygular."""
        current = self._runtime.get_run(self.run_id)
        if current.status != RunStatus.WAITING_USER:
            raise InvalidRunStateError(
                f"Run WAITING_USER durumunda değil (status={current.status}); "
                "proposal.rejected reddedildi."
            )
        return self._runtime.record(
            run_id=self.run_id, type=RunEventType.PROPOSAL_REJECTED,
            payload={"rejected": list(rejected_paths)},
            source=LIFECYCLE_SOURCE, correlation_id=self.run_id,
            expected_last_event_seq=current.last_event_seq,
        )

    # ---------------- yardımcı ----------------

    def get_run(self) -> RunRecord:
        """webhost gibi çağıranların koşunun ŞU ANKİ kanonik durumunu (örn.
        WAITING_USER olup olmadığını) doğrudan RunStore erişimi olmadan
        kontrol edebilmesi için ince bir okuma yardımcısı."""
        return self._runtime.get_run(self.run_id)
