/* Plan — Run Control'un ilk bölümü: kullanıcının onaylayacağı niyet ve Planner özeti.
   R1 mevcut serbest metin olaylarını gösterir; R2'de yapılandırılmış plan event'i gelir. */

import { ClipboardList, FileSearch, Lightbulb } from "lucide-react";
import { useRun } from "@/state/run";
import { Markdown } from "./Markdown";

export function Plan() {
  const task = useRun((s) => s.task);
  const planner = useRun((s) => s.stages.planner);
  const runStage = useRun((s) => s.runStage);

  if (!task) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <ClipboardList size={27} className="text-faint" strokeWidth={1.5} />
        <p className="text-muted" style={{ fontSize: "var(--t-label)" }}>Önce hedefi belirleyin.</p>
        <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
          Ekip, görevinizi planlayıp değişiklikleri incelemeniz için hazırlar.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <section className="material-card rounded-[var(--r-md)] border border-border-w p-3">
        <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
          <ClipboardList size={12} /> GÖREV
        </div>
        <p className="selectable text-text" style={{ fontSize: "var(--t-body)", lineHeight: 1.5 }}>{task}</p>
      </section>

      <section className="mt-2 rounded-[var(--r-md)] border border-border-w bg-panel/50">
        <div className="flex items-center gap-2 border-b border-border-w px-3 py-2 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
          <FileSearch size={12} /> PLANNER PLANI
          {planner.state === "running" && <span className="ml-auto text-accent" style={{ fontSize: "var(--t-caption)", letterSpacing: 0 }}>hazırlanıyor…</span>}
        </div>
        {planner.output ? (
          <div className="max-h-80 overflow-y-auto px-3 py-2"><Markdown text={planner.output} /></div>
        ) : (
          <div className="flex items-start gap-2 px-3 py-3 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <Lightbulb size={13} className="mt-0.5 shrink-0" />
            <span>{runStage === "planning" ? "Planner proje bağlamını inceliyor." : "Plan, run başladığında burada görünür."}</span>
          </div>
        )}
      </section>
    </div>
  );
}
