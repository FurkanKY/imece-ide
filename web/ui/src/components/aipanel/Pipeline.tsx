/* Pipeline — İMZA görselleştirme: Planner → Coder → Reviewer canlı ekip hattı.
   Bu uygulamayı Cursor'dan ayıran şey ekibin GÖRÜNÜR olması: kim çalışıyor,
   hangi modelle, kaça, ne kadar sürede — an be an. */

import { motion, useReducedMotion } from "motion/react";
import { BrainCircuit, Code2, SearchCheck, Check, X, type LucideIcon } from "lucide-react";
import { useRun, StageInfo } from "@/state/run";
import { Role } from "@/bridge";
import { Badge, StatusDot } from "@/components/ui";
import { useSettings } from "@/state/settings";

const ROLE_META: Record<Role, { label: string; intent: string; running: string; done: string; Icon: LucideIcon }> = {
  planner: { label: "Planner", intent: "Kapsam", running: "Kapsamı çıkarıyor", done: "Plan hazır", Icon: BrainCircuit },
  coder: { label: "Coder", intent: "Öneri", running: "Değişiklik üretiyor", done: "Öneri hazır", Icon: Code2 },
  reviewer: { label: "Reviewer", intent: "İnceleme", running: "Riski denetliyor", done: "İnceleme bitti", Icon: SearchCheck },
};

function StageNode({ info, routedTo }: { info: StageInfo; routedTo: string }) {
  const { label, intent, running: runningLabel, done: doneLabel, Icon } = ROLE_META[info.role];
  const running = info.state === "running";
  const done = info.state === "done";
  const error = info.state === "error";
  const reduceMotion = useReducedMotion();
  const animationsEnabled = useSettings((s) => s.prefs?.animations ?? true);
  const pulse = animationsEnabled && !reduceMotion;

  return (
    <div className="flex items-center gap-2.5 py-1">
      {/* durum düğümü */}
      <div className="relative flex size-7 shrink-0 items-center justify-center">
        {running && pulse && (
          <motion.span
            className="absolute inset-0 rounded-full bg-accent/25"
            animate={{ transform: ["scale(1)", "scale(1.45)", "scale(1)"], opacity: [0.7, 0.15, 0.7] }}
            transition={{ duration: 1.4, repeat: Infinity, ease: [0.77, 0, 0.175, 1] }}
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
          <span className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
            {intent}
          </span>
        </div>
        {done && info.latency_s !== undefined && (
          <div className="flex min-w-0 items-center gap-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <span className="min-w-0 flex-1 truncate" title={info.model ?? info.provider ?? routedTo}>
              <span className="text-ok">{doneLabel}</span> · {info.model ?? info.provider ?? routedTo}
            </span>
            <span className="shrink-0">{info.latency_s.toFixed(1)} sn · {info.tokens} tok</span>
          </div>
        )}
        {running && (
          <div className="flex items-center gap-1.5 text-accent" style={{ fontSize: "var(--t-caption)" }}>
            <StatusDot tone="accent" pulse size={5} /> {runningLabel}…
          </div>
        )}
        {!running && !done && !error && (
          <div className="truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>{info.provider || routedTo} · sırada</div>
        )}
        {error && <div className="text-err" style={{ fontSize: "var(--t-caption)" }}>Bu aşama tamamlanamadı</div>}
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
        {status === "done" && <Badge tone="ok">EKİP TAMAMLADI</Badge>}
        {status === "failed" && <Badge tone="err">HATA</Badge>}
        {status === "cancelled" && <Badge tone="warn">DURDURULDU</Badge>}
      </div>
      <div className="relative">
        {/* bağlayıcı dikey çizgi (+ koşu sırasında akan accent) */}
        <span className="absolute bottom-4 left-[13px] top-4 w-px bg-line" />
        {status === "running" && (
          <span className="flow-line absolute bottom-4 left-[13px] top-4 w-px" />
        )}
        {roles.map((r) => (
          <StageNode key={r} info={stages[r]} routedTo={routing[r]} />
        ))}
      </div>
    </div>
  );
}
