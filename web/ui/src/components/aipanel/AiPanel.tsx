/* AiPanel — sağ panel: EKİP pipeline (imza) + Akış/Değişiklikler sekmeleri +
   composer + geçmiş çekmecesi. Öneri gelince Değişiklikler'e otomatik geçer. */

import { useEffect, useState } from "react";
import { Clock, ClipboardList, Activity, FileDiff, CircleAlert, CheckCircle2, PanelRightClose, ShieldCheck, RotateCcw, Play } from "lucide-react";
import { IconButton } from "@/components/ui";
import { useRun } from "@/state/run";
import { Pipeline } from "./Pipeline";
import { Chat } from "./Chat";
import { Changes } from "./Changes";
import { Composer } from "./Composer";
import { HistoryDrawer } from "./HistoryDrawer";
import { Plan } from "./Plan";

type Tab = "plan" | "work" | "review";

const STAGE_META = {
  draft: { label: "Hazır", Icon: ClipboardList, tone: "text-muted" },
  planning: { label: "Planlanıyor", Icon: ClipboardList, tone: "text-accent" },
  working: { label: "Değişiklik hazırlanıyor", Icon: Activity, tone: "text-accent" },
  reviewing: { label: "İnceleniyor", Icon: FileDiff, tone: "text-warn" },
  ready: { label: "İnceleme hazır", Icon: FileDiff, tone: "text-warn" },
  applied: { label: "Uygulandı", Icon: CheckCircle2, tone: "text-ok" },
  restored: { label: "Geri alındı", Icon: CheckCircle2, tone: "text-muted" },
  error: { label: "Eylem gerekiyor", Icon: CircleAlert, tone: "text-err" },
} as const;

const DECISION_META = {
  planning: { title: "Plan hazırlanıyor", description: "Kapsam ve riskler çıkarılıyor.", Icon: ClipboardList, tone: "text-accent", line: "border-l-accent" },
  working: { title: "Değişiklik hazırlanıyor", description: "Plan dosya değişikliklerine dönüştürülüyor.", Icon: Activity, tone: "text-accent", line: "border-l-accent" },
  reviewing: { title: "İnceleme sürüyor", description: "Değişiklikler kontrol ediliyor.", Icon: FileDiff, tone: "text-warn", line: "border-l-warn" },
  ready: { title: "İnceleme hazır", description: "Dosyaları uygula ya da vazgeç.", Icon: ShieldCheck, tone: "text-warn", line: "border-l-warn" },
  applied: { title: "Uygulandı", description: "Geri almak için checkpoint hazır.", Icon: CheckCircle2, tone: "text-ok", line: "border-l-ok" },
  restored: { title: "Geri alındı", description: "Dosyalar checkpoint durumuna döndü.", Icon: RotateCcw, tone: "text-muted", line: "border-l-border-w2" },
  error: { title: "Koşu tamamlanmadı", description: "Görevi düzenleyip tekrar çalıştır.", Icon: CircleAlert, tone: "text-err", line: "border-l-err" },
  draft: { title: "Başlamaya hazır", description: "Bir görev yaz.", Icon: Play, tone: "text-muted", line: "border-l-border-w2" },
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
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-border-w px-3">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="shrink-0 text-muted"
          style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
          >AI ekibi</span>
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

      <div className="shrink-0 border-b border-border-w px-3 py-3">
        <div className={"flex items-start gap-2 border-l-2 py-0.5 pl-2.5 " + decision.line}>
          <DecisionIcon size={14} className={"mt-0.5 shrink-0 " + decision.tone} strokeWidth={2} />
          <div className="min-w-0">
            <p className={decision.tone} style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{decision.title}</p>
            <p className="mt-0.5 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.35 }}>{decision.description}</p>
          </div>
        </div>
      </div>

      {/* sekmeler */}
      <div className="flex shrink-0 border-b border-border-w px-2">
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
              "pressable relative flex flex-1 items-center justify-center gap-1.5 border-b-2 py-2 " +
              (tab === id ? "border-accent text-text" : "border-transparent text-muted hover:text-text2")
            }
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            <Icon size={13} strokeWidth={1.9} />
            {label}
            {badge > 0 && <span className="text-accent" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{badge}</span>}
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
