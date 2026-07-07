/* Pipeline — İMZA görselleştirme: Planner → Coder → Reviewer canlı ekip hattı.
   Bu uygulamayı Cursor'dan ayıran şey ekibin GÖRÜNÜR olması: kim çalışıyor,
   hangi modelle, kaça, ne kadar sürede — an be an. */

import { motion } from "motion/react";
import { BrainCircuit, Code2, SearchCheck, Check, X, type LucideIcon } from "lucide-react";
import { useRun, StageInfo } from "@/state/run";
import { Role } from "@/bridge";

const ROLE_META: Record<Role, { label: string; Icon: LucideIcon }> = {
  planner: { label: "Planner", Icon: BrainCircuit },
  coder: { label: "Coder", Icon: Code2 },
  reviewer: { label: "Reviewer", Icon: SearchCheck },
};

function fmtCost(v?: number) {
  if (v === undefined) return "";
  return v < 0.001 ? `$${v.toFixed(5)}` : `$${v.toFixed(4)}`;
}

function StageNode({ info, routedTo }: { info: StageInfo; routedTo: string }) {
  const { label, Icon } = ROLE_META[info.role];
  const running = info.state === "running";
  const done = info.state === "done";
  const error = info.state === "error";

  return (
    <div className="flex items-center gap-2.5 py-1">
      {/* durum düğümü */}
      <div className="relative flex size-7 shrink-0 items-center justify-center">
        {running && (
          <motion.span
            className="absolute inset-0 rounded-full bg-accent/25"
            animate={{ scale: [1, 1.45, 1], opacity: [0.7, 0.15, 0.7] }}
            transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
        <span
          className={
            "relative flex size-7 items-center justify-center rounded-full border transition-colors " +
            (running
              ? "border-accent bg-accentdim text-accent"
              : done
                ? "border-ok/50 bg-ok/10 text-ok"
                : error
                  ? "border-err/50 bg-err/10 text-err"
                  : "border-border-w bg-card text-faint")
          }
        >
          {done ? <Check size={13} strokeWidth={2.5} /> :
           error ? <X size={13} strokeWidth={2.5} /> :
           <Icon size={14} strokeWidth={1.9} />}
        </span>
      </div>

      {/* etiket + model */}
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span
            className={running || done ? "text-text" : "text-muted"}
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            {label}
          </span>
          <span className="truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
            {info.model ?? info.provider ?? routedTo}
          </span>
        </div>
        {done && info.latency_s !== undefined && (
          <div className="flex gap-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <span>{info.latency_s.toFixed(1)} sn</span>
            <span>· {info.tokens} tok</span>
            <span>· {fmtCost(info.cost_usd)}</span>
          </div>
        )}
        {running && (
          <div className="text-accent" style={{ fontSize: "var(--t-caption)" }}>
            çalışıyor…
          </div>
        )}
      </div>
    </div>
  );
}

export function Pipeline() {
  const stages = useRun((s) => s.stages);
  const routing = useRun((s) => s.routing);
  const status = useRun((s) => s.status);

  const roles: Role[] = ["planner", "coder", "reviewer"];

  return (
    <div className="border-b border-border-w px-3 py-2.5">
      <div
        className="mb-1.5 flex items-center justify-between text-muted"
        style={{
          fontSize: "var(--t-overline)",
          fontWeight: "var(--w-overline)",
          letterSpacing: "var(--ls-overline)",
        }}
      >
        <span>EKİP</span>
        {status === "done" && <span className="text-ok">TAMAMLANDI</span>}
        {status === "failed" && <span className="text-err">HATA</span>}
        {status === "cancelled" && <span className="text-warn">DURDURULDU</span>}
      </div>
      <div className="relative">
        {/* bağlayıcı dikey çizgi */}
        <span className="absolute bottom-4 left-[13px] top-4 w-px bg-line" />
        {roles.map((r) => (
          <StageNode key={r} info={stages[r]} routedTo={routing[r]} />
        ))}
      </div>
    </div>
  );
}
