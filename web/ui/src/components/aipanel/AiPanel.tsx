/* AiPanel — sağ panel: EKİP pipeline (imza) + Akış/Değişiklikler sekmeleri +
   composer + geçmiş çekmecesi. Öneri gelince Değişiklikler'e otomatik geçer. */

import { useEffect, useState } from "react";
import { Clock, ClipboardList, Activity, FileDiff, CircleAlert, CheckCircle2 } from "lucide-react";
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

export function AiPanel() {
  const [tab, setTab] = useState<Tab>("plan");
  const [historyOpen, setHistoryOpen] = useState(false);
  const diffCount = useRun((s) => s.diffs.length);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const totals = useRun((s) => s.totals);
  const meta = STAGE_META[runStage];
  const StageIcon = meta.Icon;

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
        <button
          onClick={() => setHistoryOpen(true)}
          title="Geçmiş koşular"
          aria-label="Geçmiş"
          className="icon-btn size-7"
        >
          <Clock size={14} />
        </button>
        </div>
      </div>

      <Pipeline />

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
            {badge > 0 && (
              <span
                className="rounded-[var(--r-pill)] bg-accentdim px-1.5 text-accent"
                style={{ fontSize: "10px", fontWeight: 700 }}
              >
                {badge}
              </span>
            )}
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
