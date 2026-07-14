/* Pipeline — ekip akışını süslü bir ilerleme kartı yerine kanıt zinciri olarak
   gösterir: her rolün kapsamı, mevcut durumu ve tamamlanmışsa ölçümü okunur. */

import { BrainCircuit, Code2, SearchCheck, Check, X, type LucideIcon } from "lucide-react";
import { useRun, StageInfo } from "@/state/run";
import { Role } from "@/bridge";

const ROLE_META: Record<Role, { label: string; intent: string; running: string; done: string; Icon: LucideIcon }> = {
  planner: { label: "Planner", intent: "Kapsam", running: "Kapsamı çıkarıyor", done: "Plan hazır", Icon: BrainCircuit },
  coder: { label: "Coder", intent: "Öneri", running: "Değişiklik üretiyor", done: "Öneri hazır", Icon: Code2 },
  reviewer: { label: "Reviewer", intent: "İnceleme", running: "Riski denetliyor", done: "İnceleme bitti", Icon: SearchCheck },
};

function StageRow({ index, info, routedTo }: { index: number; info: StageInfo; routedTo: string }) {
  const { label, intent, running: runningLabel, done: doneLabel, Icon } = ROLE_META[info.role];
  const running = info.state === "running";
  const done = info.state === "done";
  const error = info.state === "error";
  const tone = error ? "text-err" : running ? "text-accent" : done ? "text-ok" : "text-faint";
  const state = error ? "Tamamlanamadı" : running ? runningLabel : done ? doneLabel : "Sırada";

  return (
    <div className="grid grid-cols-[24px_18px_1fr] gap-2.5 border-l border-line py-1.5 pl-3">
      <span className={tone} style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>0{index}</span>
      <span className={tone}>{done ? <Check size={14} strokeWidth={2.2} /> : error ? <X size={14} strokeWidth={2.2} /> : <Icon size={14} strokeWidth={1.8} />}</span>
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className={running || done ? "text-text" : "text-muted"} style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{label}</span>
          <span className="text-faint" style={{ fontSize: "var(--t-caption)" }}>{intent}</span>
        </div>
        <div className={"mt-0.5 flex min-w-0 items-center gap-1.5 " + tone} style={{ fontSize: "var(--t-caption)" }}>
          <span className="min-w-0 flex-1 truncate">{state}{done && info.model ? ` · ${info.model}` : !done && !running ? ` · ${info.provider || routedTo}` : ""}</span>
          {done && info.latency_s !== undefined && <span className="shrink-0 text-faint" style={{ fontFamily: "var(--font-mono)" }}>{info.latency_s.toFixed(1)} sn · {info.tokens} tok</span>}
        </div>
      </div>
    </div>
  );
}

export function Pipeline() {
  const stages = useRun((s) => s.stages);
  const routing = useRun((s) => s.routing);
  const status = useRun((s) => s.status);
  const summary = status === "done" ? "İnceleme tamamlandı" : status === "failed" ? "Akış durdu" : status === "cancelled" ? "Akış durduruldu" : "Kanıt zinciri";
  const roles: Role[] = ["planner", "coder", "reviewer"];

  return (
    <section className="border-b border-border-w px-3 py-3">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-text2" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Ekip akışı</span>
        <span className="text-faint" style={{ fontSize: "var(--t-caption)" }}>{summary}</span>
      </div>
      {roles.map((role, i) => <StageRow key={role} index={i + 1} info={stages[role]} routedTo={routing[role]} />)}
    </section>
  );
}
