/* Chat — koşu akışı: kullanıcı görevi, aşama kartları (açılır-kapanır çıktı,
   metrik altbilgisi), bilgi/hata satırları, verdict rozeti, özet. */

import { useEffect, useRef, useState } from "react";
import { ChevronRight, Info, AlertCircle, CheckCircle2, BrainCircuit, Code2, SearchCheck, Activity } from "lucide-react";
import { useRun, STAGE_ROLE, StageInfo } from "@/state/run";
import { Role } from "@/bridge";
import { Markdown } from "./Markdown";

const STAGE_META: Record<string, { label: string; Icon: typeof Code2 }> = {
  plan: { label: "Plan", Icon: BrainCircuit },
  code: { label: "Kod", Icon: Code2 },
  review: { label: "İnceleme", Icon: SearchCheck },
};

function fmtCost(v?: number) {
  if (v === undefined) return "";
  return v < 0.001 ? `$${v.toFixed(5)}` : `$${v.toFixed(4)}`;
}

function StageCard({ stage, info }: { stage: string; info: StageInfo }) {
  const [open, setOpen] = useState(info.state === "running");
  const meta = STAGE_META[stage];
  const running = info.state === "running";

  return (
    <div
      className={
        "material-card rounded-[var(--r-md)] border " +
        (running ? "border-accent/40" : "border-border-w")
      }
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="pressable flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <ChevronRight
          size={13}
          className={"shrink-0 text-faint transition-transform " + (open ? "rotate-90" : "")}
        />
        <meta.Icon size={14} className={running ? "text-accent" : "text-muted"} strokeWidth={1.9} />
        <span className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>
          {meta.label}
        </span>
        <span className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {info.model ?? info.provider}
        </span>
        <span className="flex-1" />
        {info.state === "done" && info.latency_s !== undefined && (
          <span className="shrink-0 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            {info.latency_s.toFixed(1)}sn · {info.tokens}tok · {fmtCost(info.cost_usd)}
          </span>
        )}
        {running && (
          <span className="shrink-0 text-accent" style={{ fontSize: "var(--t-caption)" }}>●</span>
        )}
      </button>
      {open && info.output && (
        running ? (
          // akış sırasında düz mono (hızlı); bitince markdown işlenir
          <pre
            className="selectable max-h-56 overflow-auto whitespace-pre-wrap border-t border-border-w px-3 py-2 text-text2"
            style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-mono)" }}
          >
            {info.output}
          </pre>
        ) : (
          <div className="max-h-72 overflow-auto border-t border-border-w px-3 py-2">
            <Markdown text={info.output} />
          </div>
        )
      )}
    </div>
  );
}

export function Chat() {
  const flow = useRun((s) => s.flow);
  const stages = useRun((s) => s.stages);
  const verdict = useRun((s) => s.verdict);
  const verdictNote = useRun((s) => s.verdictNote);
  const endRef = useRef<HTMLDivElement>(null);

  // yeni içerik gelince yumuşak kaydır
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [flow, stages, verdict]);

  if (flow.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <BrainCircuit size={28} className="text-faint" strokeWidth={1.5} />
        <p className="text-muted" style={{ fontSize: "var(--t-label)" }}>
          Ekip hazır.
        </p>
        <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
          Alttan bir görev yaz — Planner böler, Coder yazar, Reviewer denetler.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-3">
      <div className="flex items-center gap-1.5 px-1 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
        <Activity size={12} /> ÇALIŞMA ZAMAN ÇİZELGESİ
      </div>
      {flow.map((item) => {
        if (item.kind === "task") {
          return null; // görev ve plan, Plan bölümünün sahipliğindedir
        }
        if (item.kind === "stage" && item.stage) {
          const role = STAGE_ROLE[item.stage] as Role | undefined;
          if (!role) return null;
          return <StageCard key={item.id} stage={item.stage} info={stages[role]} />;
        }
        if (item.kind === "info") {
          return (
            <div key={item.id} className="flex items-center gap-2 px-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>
              <Info size={12} className="shrink-0" /> {item.text}
            </div>
          );
        }
        if (item.kind === "error") {
          return (
            <div key={item.id} className="flex items-start gap-2 rounded-[var(--r-md)] border border-err/40 bg-err/10 px-3 py-2 text-err" style={{ fontSize: "var(--t-label)" }}>
              <AlertCircle size={14} className="mt-0.5 shrink-0" />
              <span className="selectable">{item.text}</span>
            </div>
          );
        }
        // summary
        return (
          <div key={item.id} className="flex items-center gap-2 rounded-[var(--r-md)] border border-ok/40 bg-ok/10 px-3 py-2 text-ok" style={{ fontSize: "var(--t-label)" }}>
            <CheckCircle2 size={14} className="shrink-0" /> {item.text}
          </div>
        );
      })}

      {verdict && (
        <div
          className={
            "flex items-center gap-2 self-start rounded-[var(--r-pill)] border px-2.5 py-1 " +
            (verdict === "APPROVED"
              ? "border-ok/50 text-ok"
              : "border-warn/50 text-warn")
          }
          style={{ fontSize: "var(--t-caption)", fontWeight: 700 }}
          title={verdictNote}
        >
          İNCELEME: {verdict === "APPROVED" ? "ONAYLANDI" : verdict === "NEEDS_FIX" ? "DÜZELTME GEREK" : verdict}
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}
