/* Plan — Run Control'un ilk bölümü: kullanıcının onaylayacağı niyet ve Planner özeti.
   R1 mevcut serbest metin olaylarını gösterir; R2'de yapılandırılmış plan event'i gelir. */

import { ClipboardList, FileSearch, Lightbulb, FileCode2, ShieldAlert } from "lucide-react";
import { useRun } from "@/state/run";
import { Badge, EmptyState, StatusDot } from "@/components/ui";
import { Markdown } from "./Markdown";

export function Plan() {
  const task = useRun((s) => s.task);
  const planner = useRun((s) => s.stages.planner);
  const plan = useRun((s) => s.plan);
  const runStage = useRun((s) => s.runStage);

  if (!task) {
    return (
      <EmptyState
        icon={ClipboardList}
        title="Önce hedefi belirleyin"
        description="Ekip, görevinizi planlayıp değişiklikleri incelemeniz için hazırlar."
        className="h-full"
      />
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <section className="material-card rounded-[var(--r-md)] border border-border-w p-3">
        <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
          <ClipboardList size={12} /> GÖREV
          {runStage !== "draft" && <Badge tone={runStage === "error" ? "err" : runStage === "ready" ? "warn" : "accent"} className="ml-auto">{runStage === "ready" ? "İNCELEMEDE" : runStage === "error" ? "DURDU" : "AKIŞTA"}</Badge>}
        </div>
        <p className="selectable text-text" style={{ fontSize: "var(--t-body)", lineHeight: 1.5 }}>{task}</p>
      </section>

      <section className="mt-2 rounded-[var(--r-md)] border border-border-w bg-panel/50">
        <div className="flex items-center gap-2 border-b border-border-w px-3 py-2 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
          <FileSearch size={12} /> PLANNER PLANI
          {planner.state === "running" && (
            <span className="ml-auto flex items-center gap-1.5 text-accent" style={{ fontSize: "var(--t-caption)", letterSpacing: 0 }}>
              <StatusDot tone="accent" pulse size={5} /> hazırlanıyor…
            </span>
          )}
          {planner.state === "done" && <Badge tone="ok" className="ml-auto">HAZIR</Badge>}
        </div>
        {plan?.summary || planner.output ? (
          <div className="max-h-80 overflow-y-auto px-3 py-2"><Markdown text={plan?.summary || planner.output} /></div>
        ) : (
          <div className="flex items-start gap-2 px-3 py-3 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <Lightbulb size={13} className="mt-0.5 shrink-0" />
            <span>{runStage === "planning" ? "Planner proje bağlamını inceliyor." : "Plan, run başladığında burada görünür."}</span>
          </div>
        )}
      </section>

      {plan && (
        <div className="mt-2 space-y-2">
          <section className="material-card rounded-[var(--r-md)] border border-border-w p-3">
            <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
              <FileCode2 size={12} /> BAĞLAMA GİREN DOSYALAR
              <Badge tone="neutral" className="ml-auto">{plan.files.length}</Badge>
            </div>
            {plan.files.length ? (
              <div className="flex flex-wrap gap-1.5">
                {plan.files.map((file) => <span key={file} className="rounded-[var(--r-pill)] border border-border-w bg-field px-2 py-0.5 text-text2" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{file}</span>)}
              </div>
            ) : <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>Planner dosya seçmedi.</p>}
          </section>
          {(plan.assumptions.length > 0 || plan.risks.length > 0) && (
            <section className="rounded-[var(--r-md)] border border-warn/30 bg-warn/5 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-warn" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}><ShieldAlert size={12} /> KONTROL NOKTALARI</div>
              {plan.assumptions.length > 0 && (
                <div>
                  <p className="text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>VARSAYIM</p>
                  {plan.assumptions.map((text, i) => <p key={i} className="mt-1 text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>• {text}</p>)}
                </div>
              )}
              {plan.risks.length > 0 && (
                <div className={plan.assumptions.length > 0 ? "mt-2" : ""}>
                  <p className="text-warn" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>RİSK</p>
                  {plan.risks.map((text, i) => <p key={i} className="mt-1 text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>• {text}</p>)}
                </div>
              )}
            </section>
          )}
        </div>
      )}
    </div>
  );
}
