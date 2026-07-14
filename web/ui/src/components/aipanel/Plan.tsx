/* Plan — Run Control'un ilk bölümü: kullanıcının onaylayacağı niyet ve Planner özeti.
   R1 mevcut serbest metin olaylarını gösterir; R2'de yapılandırılmış plan event'i gelir. */

import { ClipboardList, FileSearch, Lightbulb, FileCode2, ShieldAlert } from "lucide-react";
import { useRun } from "@/state/run";
import { EmptyState, StatusDot } from "@/components/ui";
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
        title="Bir görev yazın"
        description="Plan ve değişiklikler burada görünür."
        className="h-full"
      />
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <section className="border-b border-border-w pb-3">
        <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
          <ClipboardList size={12} /> Görev
          {runStage !== "draft" && <span className={"ml-auto " + (runStage === "error" ? "text-err" : runStage === "ready" ? "text-warn" : "text-accent")} style={{ fontSize: "var(--t-caption)" }}>{runStage === "ready" ? "İncelemede" : runStage === "error" ? "Durdu" : "Akışta"}</span>}
        </div>
        <p className="selectable text-text" style={{ fontSize: "var(--t-body)", lineHeight: 1.5 }}>{task}</p>
      </section>

      <section className="mt-4 border-l-2 border-l-accent pl-3">
        <div className="flex items-center gap-2 text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
          <FileSearch size={12} /> Plan
          {planner.state === "running" && (
            <span className="ml-auto flex items-center gap-1.5 text-accent" style={{ fontSize: "var(--t-caption)", letterSpacing: 0 }}>
              <StatusDot tone="accent" pulse size={5} /> hazırlanıyor…
            </span>
          )}
          {planner.state === "done" && <span className="ml-auto text-ok" style={{ fontSize: "var(--t-caption)" }}>Hazır</span>}
        </div>
        {plan?.summary || planner.output ? (
          <div className="max-h-80 overflow-y-auto pt-2"><Markdown text={plan?.summary || planner.output} /></div>
        ) : (
          <div className="flex items-start gap-2 pt-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <Lightbulb size={13} className="mt-0.5 shrink-0" />
            <span>{runStage === "planning" ? "Proje bağlamı inceleniyor." : "Plan, koşu başlayınca burada görünür."}</span>
          </div>
        )}
      </section>

      {plan && (
        <div className="mt-5 space-y-4">
          <section className="border-t border-border-w pt-3">
            <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
              <FileCode2 size={12} /> Kapsamdaki dosyalar
              <span className="ml-auto text-faint" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{plan.files.length}</span>
            </div>
            {plan.files.length ? (
              <div className="divide-y divide-[var(--line)] border-y border-[var(--line)]">
                {plan.files.map((file) => <p key={file} className="py-1.5 text-text2" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{file}</p>)}
              </div>
            ) : <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>Dosya seçilmedi.</p>}
          </section>
          {(plan.assumptions.length > 0 || plan.risks.length > 0) && (
            <section className="border-l-2 border-l-warn pl-3">
              <div className="mb-2 flex items-center gap-1.5 text-warn" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}><ShieldAlert size={12} /> Notlar</div>
              {plan.assumptions.length > 0 && (
                <div>
                  <p className="text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Varsayım</p>
                  {plan.assumptions.map((text, i) => <p key={i} className="mt-1 text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>• {text}</p>)}
                </div>
              )}
              {plan.risks.length > 0 && (
                <div className={plan.assumptions.length > 0 ? "mt-2" : ""}>
                  <p className="text-warn" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Risk</p>
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
