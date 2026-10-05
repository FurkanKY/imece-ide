/* AiPanel — sağ panel: EKİP pipeline (imza) + Akış/Değişiklikler sekmeleri +
   composer + geçmiş çekmecesi. Öneri gelince Değişiklikler'e otomatik geçer. */

import { useEffect, useId, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Clock, ClipboardList, Activity as ActivityIcon, FileDiff, CircleAlert, CheckCircle2, PanelRightClose, ShieldCheck, RotateCcw, Play, Radio, Info, Ban, ChevronRight } from "lucide-react";
import { IconButton } from "@/components/ui";
import { useRun } from "@/state/run";
import { Pipeline as LegacyPipeline } from "./Pipeline";
import { Chat } from "./Chat";
import { Changes } from "./Changes";
import { Composer } from "./Composer";
import { HistoryDrawer } from "./HistoryDrawer";
import { Plan } from "./Plan";
import { Activity } from "./Activity";
import { SharedDelivery } from "./SharedDelivery";
import { OwnerSession } from "./OwnerSession";
import { SharedProduct } from "./SharedProduct";

type Tab = "plan" | "work" | "review" | "activity" | "shared" | "owner" | "product";

const STAGE_META = {
  draft: { label: "Hazır", Icon: ClipboardList, tone: "text-muted" },
  planning: { label: "Planlanıyor", Icon: ClipboardList, tone: "text-accent" },
  working: { label: "Değişiklik hazırlanıyor", Icon: ActivityIcon, tone: "text-accent" },
  reviewing: { label: "İnceleniyor", Icon: FileDiff, tone: "text-warn" },
  ready: { label: "İnceleme hazır", Icon: FileDiff, tone: "text-warn" },
  applied: { label: "Uygulandı", Icon: CheckCircle2, tone: "text-ok" },
  restored: { label: "Geri alındı", Icon: CheckCircle2, tone: "text-muted" },
  error: { label: "Eylem gerekiyor", Icon: CircleAlert, tone: "text-err" },
  noChanges: { label: "Değişiklik yok", Icon: Info, tone: "text-muted" },
  // A4-A4: iptal edilen bir koşu artık "draft"a (hiç başlamamış) değil,
  // kendi ayrı terminal durumuna düşer (bkz. state/run.ts install()).
  cancelled: { label: "İptal edildi", Icon: Ban, tone: "text-muted" },
} as const;

const DECISION_META = {
  planning: { title: "Plan hazırlanıyor", description: "Kapsam ve riskler çıkarılıyor.", Icon: ClipboardList, tone: "text-accent", line: "border-l-accent" },
  working: { title: "Değişiklik hazırlanıyor", description: "Plan dosya değişikliklerine dönüştürülüyor.", Icon: ActivityIcon, tone: "text-accent", line: "border-l-accent" },
  reviewing: { title: "İnceleme sürüyor", description: "Değişiklikler kontrol ediliyor.", Icon: FileDiff, tone: "text-warn", line: "border-l-warn" },
  ready: { title: "İnceleme hazır", description: "Dosyaları uygula ya da vazgeç.", Icon: ShieldCheck, tone: "text-warn", line: "border-l-warn" },
  applied: { title: "Uygulandı", description: "Geri almak için checkpoint hazır.", Icon: CheckCircle2, tone: "text-ok", line: "border-l-ok" },
  restored: { title: "Geri alındı", description: "Dosyalar checkpoint durumuna döndü.", Icon: RotateCcw, tone: "text-muted", line: "border-l-border-w2" },
  // A5 (hata UX): bu, YALNIZCA errorTitle/errorDescription yokken (ör. eski
  // bir run.finished) kullanılan genel bir düşüş (fallback) metnidir --
  // normalde aşağıdaki `decision` hesaplaması backend'in eşlediği başlık/
  // açıklamayı kullanır (bkz. webhost/api/run.py _ERROR_MESSAGES).
  error: { title: "Koşu tamamlanmadı", description: "Beklenmeyen bir hata oluştu. Ayrıntılara bakıp tekrar deneyin.", Icon: CircleAlert, tone: "text-err", line: "border-l-err" },
  draft: { title: "Başlamaya hazır", description: "Bir görev yaz.", Icon: Play, tone: "text-muted", line: "border-l-border-w2" },
  noChanges: {
    title: "Değişiklik önerisi çıkmadı",
    description: "Ajan bir değişiklik yapmadı; akışta ne olduğunu görüp görevi düzenleyerek tekrar deneyebilirsin.",
    Icon: Info,
    tone: "text-muted",
    line: "border-l-border-w2",
  },
  // A4-A4: başarısızlık/değişiklik-yok kartlarından AYRI, kendi "İptal
  // edildi" terminal kartı.
  cancelled: {
    title: "İptal edildi",
    description: "Koşu kullanıcı isteğiyle durduruldu; bekleyen bir öneri yok. Görevi düzenleyip tekrar başlatabilirsin.",
    Icon: Ban,
    tone: "text-muted",
    line: "border-l-border-w2",
  },
} as const;

export function AiPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTabRaw] = useState<Tab>("work");
  // Sekme/panel DOM kimlikleri useId'den turer: render'lar arasinda SABIT
  // kalir, boylece aria-controls / aria-labelledby hedefleri her render'da
  // gecerli bir elemente isaret eder.
  const domId = useId();
  const tabId = (t: Tab) => `${domId}-tab-${t}`;
  const panelId = `${domId}-tabpanel`;
  // A4-A1 (Etkinlik sekmesi sıçraması): kullanıcı sekmeyi ELİYLE seçtiğinde
  // (aşağıdaki `selectTab`) otomatik geçiş bu koşu BOYUNCA devre dışı kalır
  // -- bir sonraki aşama değişikliği kullanıcıyı Etkinlik'ten (veya
  // seçtiği herhangi bir sekmeden) ATMAZ. Yeni bir koşu başlayınca (runId
  // değişince) otomatik rehberlik yeniden açılır -- bu en az şaşırtıcı
  // davranış: hiç dokunmayan kullanıcı için eski plan→çalışma→inceleme
  // akışı AYNEN sürer, elle seçim yapan kullanıcının tercihi ise KORUNUR.
  const [autoTab, setAutoTab] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [errorDetailsOpen, setErrorDetailsOpen] = useState(false);
  const diffCount = useRun((s) => s.diffs.length);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const runId = useRun((s) => s.runId);
  const engine = useRun((s) => s.engine);
  const totals = useRun((s) => s.totals);
  const rawError = useRun((s) => s.error);
  const errorTitle = useRun((s) => s.errorTitle);
  const errorDescription = useRun((s) => s.errorDescription);
  // Sekme tanimlarinin TEK kaynagi: sekme cubugu buradan render edilir,
  // klavye gezinmesi de sirayi buradan okur -- etiketler ve sira aynen
  // onceki haliyle korunur (Plan, Calisma, Inceleme, Etkinlik, Ortak aday,
  // Oturum, Ortak urun).
  const legacyVisible = engine === "pipeline" || engine === "legacy";
  const tabs = [
    ...(legacyVisible ? [{ id: "plan" as const, label: "Plan", Icon: ClipboardList, badge: 0 }] : []),
    { id: "work" as const, label: "Çalışma", Icon: ActivityIcon, badge: status === "running" ? 1 : 0 },
    { id: "review" as const, label: "Sonuç", Icon: FileDiff, badge: diffCount },
    { id: "activity", label: "Etkinlik", Icon: Radio, badge: 0 },
    { id: "shared", label: "Ortak aday", Icon: ShieldCheck, badge: 0 },
    { id: "owner", label: "Oturum", Icon: ShieldCheck, badge: 0 },
    { id: "product", label: "Ortak ürün", Icon: ClipboardList, badge: 0 },
  ] as const;
  const meta = STAGE_META[runStage];
  const StageIcon = meta.Icon;
  const decisionBase = !legacyVisible && runStage === "working"
    ? { ...DECISION_META.working, description: "Seçilen ajan görevi izole çalışma alanında yürütüyor." }
    : DECISION_META[runStage];
  // A5 (hata UX): backend eşlemesi varsa (normal yol) onu kullan; yoksa
  // DECISION_META.error'ın genel metnine düş.
  const decision = runStage === "error" && errorTitle
    ? { ...decisionBase, title: errorTitle, description: errorDescription ?? decisionBase.description }
    : decisionBase;
  const DecisionIcon = decision.Icon;

  const selectTab = (id: Tab) => {
    setAutoTab(false);
    setTabRaw(id);
  };

  // Sekme klavye gezinmesi (APG tabs deseni): yalnizca dort gezinme tusu
  // ele alinir; onlar icin preventDefault+stopPropagation cagrilir, DIGER
  // tuslar (Tab, Enter, Space, kisayollar) tarayiciya oldugu gibi gecer.
  // Ok tuslari bastan sona SARAR, Home/End ilk/son sekmeye gider. Secim elle
  // yapildigi icin (selectTab) otomatik asama yonlendirmesi kapanir.
  // Odak YALNIZCA burada tasinir: otomatik asama gecisleri odagi CALMAZ.
  const onTabKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>, current: Tab) => {
    const i = tabs.findIndex((t) => t.id === current);
    if (i < 0) return;
    let next: Tab | null = null;
    if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length].id;
    else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length].id;
    else if (e.key === "Home") next = tabs[0].id;
    else if (e.key === "End") next = tabs[tabs.length - 1].id;
    if (next === null) return; // yonetilmeyen tus: dokunma
    e.preventDefault();
    e.stopPropagation();
    selectTab(next);
    const el = document.getElementById(tabId(next));
    el?.focus();
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };

  // yeni bir koşu başlayınca otomatik rehberliği YENİDEN aç.
  useEffect(() => {
    setAutoTab(true);
    setErrorDetailsOpen(false);
  }, [runId]);

  // öneri hazır olunca Değişiklikler sekmesine geç (desktop.py _focus_view deseni)
  // -- YALNIZCA kullanıcı bu koşuda henüz elle bir sekme SEÇMEDİYSE (bkz.
  // yukarıdaki autoTab notu).
  useEffect(() => {
    if (!autoTab) return;
    if (runStage === "planning") setTabRaw("plan");
    if (runStage === "working" || runStage === "reviewing" || runStage === "error" || runStage === "noChanges" || runStage === "cancelled") setTabRaw("work");
    if (runStage === "ready" || runStage === "applied" || runStage === "restored") setTabRaw("review");
  }, [runStage, status, diffCount, autoTab]);

  return (
    <aside className="relative flex h-full w-full flex-col bg-side">
      {/* başlık */}
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-border-w px-3">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="shrink-0 text-muted"
          style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
            >Çalışma</span>
          <span className={"flex min-w-0 items-center gap-1 truncate " + meta.tone} style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
            <StageIcon size={12} strokeWidth={2} /> {meta.label}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {totals && <span className="text-faint" title="Toplam maliyet" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{totals.cost_usd == null ? "—" : `$${totals.cost_usd.toFixed(4)}`}</span>}
        <IconButton icon={Clock} label="Geçmiş koşular" onClick={() => setHistoryOpen(true)} />
        <IconButton icon={PanelRightClose} label="AI panelini kapat" onClick={onClose} />
        </div>
      </div>

      {legacyVisible && <div className="max-h-48 overflow-y-auto"><LegacyPipeline /></div>}

      <div className="shrink-0 border-b border-border-w px-3 py-3">
        <div className={"flex items-start gap-2 border-l-2 py-0.5 pl-2.5 " + decision.line}>
          <DecisionIcon size={14} className={"mt-0.5 shrink-0 " + decision.tone} strokeWidth={2} />
          <div className="min-w-0 flex-1">
            <p className={decision.tone} style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{decision.title}</p>
            <p className="mt-0.5 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.35 }}>{decision.description}</p>
            {/* A5 (hata UX): ham hata metni burada, isteğe bağlı olarak
                genişletilir -- terminal kart yalnızca Türkçe özet/eylem
                metnini gösterir, ham metin (İngilizce olabilir, uzun
                olabilir) gizli ama HER ZAMAN erişilebilir kalır. */}
            {runStage === "error" && rawError && (
              <button
                onClick={() => setErrorDetailsOpen((o) => !o)}
                className="pressable mt-1 flex items-center gap-1 text-faint hover:text-muted"
                style={{ fontSize: "var(--t-caption)" }}
              >
                <ChevronRight size={11} className={"transition-transform " + (errorDetailsOpen ? "rotate-90" : "")} />
                Ayrıntılar
              </button>
            )}
            {runStage === "error" && rawError && errorDetailsOpen && (
              <pre
                className="material-card selectable mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-[var(--r-sm)] border border-border-w p-2 text-text2"
                style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}
              >
                {rawError}
              </pre>
            )}
          </div>
        </div>
      </div>

      {/* sekmeler — role=tablist + roving tabIndex (aktif 0, digerleri -1) */}
      <div
        role="tablist"
        aria-label="Çalışma panelleri"
        className="flex shrink-0 overflow-x-auto border-b border-border-w px-2"
      >
        {tabs.map(({ id, label, Icon, badge }) => {
          const active = tab === id;
          return (
            <button
              key={id}
              id={tabId(id)}
              role="tab"
              aria-selected={active}
              aria-controls={panelId}
              tabIndex={active ? 0 : -1}
              onClick={() => selectTab(id)}
              onKeyDown={(e) => onTabKeyDown(e, id)}
              className={
                "pressable relative flex shrink-0 items-center justify-center gap-1.5 border-b-2 px-2 py-2 " +
                (active ? "border-accent text-text" : "border-transparent text-muted hover:text-text2")
              }
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
            >
              <Icon size={13} strokeWidth={1.9} />
              {label}
              {badge > 0 && <span className="text-accent" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{badge}</span>}
            </button>
          );
        })}
      </div>

      {/* içerik — TEK panel mount edilir: Owner/SharedProduct gibi bilesenlerin
          timer temizligi unmount'a bagli oldugundan hepsi birden mount edilmez.
          Sekmelerin hepsi ayni aria-controls hedefini paylasir; etiket, o an
          etkin olan sekmeye (aria-labelledby) baglidir. */}
      <div
        role="tabpanel"
        id={panelId}
        aria-labelledby={tabId(tab)}
        tabIndex={0}
        className="min-h-0 flex-1"
      >
        {tab === "plan" && legacyVisible ? <Plan /> : tab === "work" ? <Chat /> : tab === "activity" ? <Activity /> : tab === "shared" ? <SharedDelivery /> : tab === "owner" ? <OwnerSession /> : tab === "product" ? <SharedProduct onNavigate={selectTab} /> : <Changes />}
      </div>

      <Composer />
      <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </aside>
  );
}
