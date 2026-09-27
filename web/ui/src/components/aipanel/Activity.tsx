/* Activity ("Etkinlik") — F1: canlı ajan etkinliği. run.activity kanalından
   gelen öğeleri (araç çağrıları, doğrulama kontrolleri, plan/inceleme
   aşamaları, kullanım notları) rol bazlı gruplayıp kronolojik listeler;
   her öğe genişletilebilir (detail — komut çıktısı kuyruğu vb). */

import { useMemo, useState } from "react";
import {
  BrainCircuit, Code2, SearchCheck, Wrench, TerminalSquare, ChevronRight,
  Loader2, Check, X, Info, type LucideIcon,
} from "lucide-react";
import { useActivity } from "@/state/activity";
import { ActivityItem, ActivityRole } from "@/bridge";
import { EmptyState } from "@/components/ui";

const ROLE_META: Record<ActivityRole, { label: string; Icon: LucideIcon }> = {
  planner: { label: "Planner", Icon: BrainCircuit },
  worker: { label: "Coder", Icon: Code2 },
  verification: { label: "Doğrulama", Icon: TerminalSquare },
  reviewer: { label: "Reviewer", Icon: SearchCheck },
  fix: { label: "Otomatik düzeltme", Icon: Wrench },
  system: { label: "Sistem", Icon: Info },
};

function StatusIcon({ status }: { status: ActivityItem["status"] }) {
  if (status === "running") return <Loader2 size={13} className="animate-spin text-accent" strokeWidth={2} />;
  if (status === "ok") return <Check size={13} className="text-ok" strokeWidth={2.2} />;
  if (status === "error") return <X size={13} className="text-err" strokeWidth={2.2} />;
  return <Info size={13} className="text-faint" strokeWidth={2} />;
}

function Row({ item }: { item: ActivityItem }) {
  const [open, setOpen] = useState(false);
  const hasDetail = !!item.detail;
  const time = useMemo(() => {
    const d = new Date(item.ts);
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("tr-TR", { hour12: false });
  }, [item.ts]);

  return (
    <div className="border-l border-line pl-3">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        disabled={!hasDetail}
        className={"pressable flex w-full items-start gap-2 py-1 text-left " + (hasDetail ? "" : "cursor-default")}
      >
        {hasDetail ? (
          <ChevronRight
            size={12}
            className={"mt-0.5 shrink-0 text-faint transition-transform " + (open ? "rotate-90" : "")}
          />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className="mt-0.5 shrink-0"><StatusIcon status={item.status} /></span>
        <span className="min-w-0 flex-1 truncate text-text" style={{ fontSize: "var(--t-label)" }}>
          {item.title}
        </span>
        <span className="shrink-0 text-faint" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>
          {time}
        </span>
      </button>
      {open && hasDetail && (
        <pre
          className="material-card mb-1.5 ml-5 max-h-48 overflow-auto whitespace-pre-wrap rounded-[var(--r-sm)] border border-border-w p-2 text-text2"
          style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}
        >
          {item.detail}
        </pre>
      )}
    </div>
  );
}

function RoleGroup({ role, items }: { role: ActivityRole; items: ActivityItem[] }) {
  const meta = ROLE_META[role];
  const running = items.some((i) => i.status === "running");
  return (
    <section className="mb-3">
      <div className="mb-1 flex items-center gap-1.5 text-muted">
        <meta.Icon size={13} className={running ? "text-accent" : "text-faint"} strokeWidth={1.9} />
        <span style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>{meta.label}</span>
        <span className="text-faint" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>
          {items.length}
        </span>
      </div>
      {items.map((item) => <Row key={item.id} item={item} />)}
    </section>
  );
}

const ROLE_ORDER: ActivityRole[] = ["planner", "worker", "verification", "reviewer", "fix", "system"];

export function Activity() {
  const items = useActivity((s) => s.items);

  const grouped = useMemo(() => {
    const byRole = new Map<ActivityRole, ActivityItem[]>();
    for (const item of items) {
      const list = byRole.get(item.role) ?? [];
      list.push(item);
      byRole.set(item.role, list);
    }
    return ROLE_ORDER.map((role) => ({ role, items: byRole.get(role) ?? [] })).filter((g) => g.items.length > 0);
  }, [items]);

  if (grouped.length === 0) {
    return (
      <EmptyState
        icon={TerminalSquare}
        title="Henüz bir etkinlik yok"
        description="Bir koşu başladığında her ajanın ne yaptığını burada canlı olarak görürsünüz."
      />
    );
  }

  return (
    <div className="h-full overflow-y-auto px-3 py-3">
      {grouped.map((g) => <RoleGroup key={g.role} role={g.role} items={g.items} />)}
    </div>
  );
}
