/* AiPanel — sağ panel: EKİP pipeline (imza) + Akış/Değişiklikler sekmeleri +
   composer + geçmiş çekmecesi. Öneri gelince Değişiklikler'e otomatik geçer. */

import { useEffect, useState } from "react";
import { Clock, ClipboardList, Activity, FileDiff, CircleAlert, CheckCircle2, PanelRightClose, ShieldCheck, RotateCcw, Play } from "lucide-react";
import { IconButton, Badge } from "@/components/ui";
import { useRun } from "@/state/run";
import { Pipeline } from "./Pipeline";
import { Chat } from "./Chat";
import { Changes } from "./Changes";
import { Composer } from "./Composer";
import { HistoryDrawer } from "./HistoryDrawer";
import { Plan } from "./Plan";

type Tab = "plan" | "work" | "review";

const STAGE_META = {
  draft: { label: "TASLAK", Icon: ClipboardList, tone: "text-muted" },
  planning: { label: "PLANLANIYOR", Icon: ClipboardList, tone: "text-accent" },
  working: { label: "ÇALIŞIYOR", Icon: Activity, tone: "text-accent" },
  reviewing: { label: "İNCELENİYOR", Icon: FileDiff, tone: "text-warn" },
  ready: { label: "KARAR BEKLİYOR", Icon: FileDiff, tone: "text-warn" },
  applied: { label: "UYGULANDI", Icon: CheckCircle2, tone: "text-ok" },
  restored: { label: "GERİ ALINDI", Icon: CheckCircle2, tone: "text-muted" },
  error: { label: "EYLEM GEREKİYOR", Icon: CircleAlert, tone: "text-err" },
} as const;

const DECISION_META = {
  planning: { title: "Plan hazırlanıyor", description: "Planner görevin kapsamını ve risklerini çıkarıyor.", Icon: ClipboardList, tone: "text-accent", surface: "border-accent/25 bg-accentdim/35" },
  working: { title: "Öneri üretiliyor", description: "Coder planı dosya değişikliklerine dönüştürüyor.", Icon: Activity, tone: "text-accent", surface: "border-accent/25 bg-accentdim/35" },
  reviewing: { title: "Güvenlik incelemesi", description: "Reviewer değişikliklerin etkisini ve sonucu denetliyor.", Icon: FileDiff, tone: "text-warn", surface: "border-warn/25 bg-warn/5" },
  ready: { title: "Sıradaki karar sizde", description: "Önerilen dosyaları inceleyin; uygun olanları uygulayın veya vazgeçin.", Icon: ShieldCheck, tone: "text-warn", surface: "border-warn/30 bg-warn/5" },
  applied: { title: "Değişiklikler uygulandı", description: "Bu turun checkpoint'i hazır; gerekirse tek adımda geri alabilirsiniz.", Icon: CheckCircle2, tone: "text-ok", surface: "border-ok/25 bg-ok/5" },
  restored: { title: "Checkpoint'e dönüldü", description: "Dosyalar güvenli önceki hâline geri yüklendi.", Icon: RotateCcw, tone: "text-muted", surface: "border-border-w bg-card/60" },
  error: { title: "Eylem gerekiyor", description: "Bu tur tamamlanamadı. Görevi düzenleyip yeniden çalıştırabilirsiniz.", Icon: CircleAlert, tone: "text-err", surface: "border-err/30 bg-err/5" },
  draft: { title: "Ekibiniz hazır", description: "Bir hedef verin; plan, öneri ve inceleme sırayla ilerler.", Icon: Play, tone: "text-muted", surface: "border-border-w bg-card/40" },
} as const;

export function AiPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("plan");
  const [historyOpen, setHistoryOpen] = useState(false);
  const diffCount = useRun((s) => s.diffs.length);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const totals = useRun((s) => s.totals);
  const meta = STAGE_META[runStage];
  const StageIcon = meta.Icon;
  const decision = DECISION_META[runStage];
  const DecisionIcon = decision.Icon;

  // öneri hazır olunca Değişiklikler sekmesine geç (desktop.py _focus_view deseni)
  useEffect(() => {
    if (runStage === "planning") setTab("plan");
    if (runStage === "working" || runStage === "reviewing" || runStage === "error") setTab("work");
    if (runStage === "ready" || runStage === "applied" || runStage === "restored") setTab("review");
  }, [runStage, status, diffCount]);

  return (
    <aside className="relative flex h-full w-full flex-col bg-side">
      {/* başlık */}
      <div className="material-panel flex h-10 shrink-0 items-center justify-between border-b border-border-w px-3">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="shrink-0 text-muted"
          style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
          >AI EKİBİ</span>
          <span className={"flex min-w-0 items-center gap-1 truncate " + meta.tone} style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
            <StageIcon size={12} strokeWidth={2} /> {meta.label}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {totals && <span className="text-faint" title="Toplam maliyet" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>${totals.cost_usd.toFixed(4)}</span>}
        <IconButton icon={Clock} label="Geçmiş koşular" onClick={() => setHistoryOpen(true)} />
        <IconButton icon={PanelRightClose} label="AI panelini kapat" onClick={onClose} />
        </div>
      </div>

      <Pipeline />

      <div className="shrink-0 border-b border-border-w px-3 py-2">
        <div className={"flex items-start gap-2 rounded-[var(--r-sm)] border px-2.5 py-2 " + decision.surface}>
          <DecisionIcon size={14} className={"mt-0.5 shrink-0 " + decision.tone} strokeWidth={2} />
          <div className="min-w-0">
            <p className={decision.tone} style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{decision.title}</p>
            <p className="mt-0.5 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.35 }}>{decision.description}</p>
          </div>
        </div>
      </div>

      {/* sekmeler */}
      <div className="flex shrink-0 border-b border-border-w bg-panel/40 px-2 py-1">
        {(
          [
            { id: "plan", label: "Plan", Icon: ClipboardList, badge: 0 },
            { id: "work", label: "Çalışma", Icon: Activity, badge: status === "running" ? 1 : 0 },
            { id: "review", label: "İnceleme", Icon: FileDiff, badge: diffCount },
          ] as const
        ).map(({ id, label, Icon, badge }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={
              "pressable relative flex flex-1 items-center justify-center gap-1.5 rounded-[var(--r-sm)] py-1.5 " +
              (tab === id ? "bg-card2 text-text" : "text-muted hover:text-text2")
            }
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            <Icon size={13} strokeWidth={1.9} />
            {label}
            {badge > 0 && <Badge tone="accent">{badge}</Badge>}
            {tab === id && <span className="absolute inset-x-4 bottom-0 h-[2px] rounded-t bg-accent" />}
          </button>
        ))}
      </div>

      {/* içerik */}
      <div className="min-h-0 flex-1">
        {tab === "plan" ? <Plan /> : tab === "work" ? <Chat /> : <Changes />}
      </div>

      <Composer />
      <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </aside>
  );
}
