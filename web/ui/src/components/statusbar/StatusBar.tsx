/* StatusBar — alt durum çubuğu: proje adı, koşu HUD'u (token/maliyet count-up),
   aktif dosya dili. desktop.py HUD'unun halefi. */

import { Coins, Cpu } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useRun } from "@/state/run";
import { langForPath } from "@/lib/monaco";
import { CountUp } from "./CountUp";

export function StatusBar() {
  const name = useWorkspace((s) => s.name);
  const activeRel = useEditor((s) => s.activeRel);
  const dirty = useEditor((s) => s.tabs.find((t) => t.rel === s.activeRel)?.dirty);
  const totals = useRun((s) => s.totals);
  const status = useRun((s) => s.status);

  return (
    <footer
      className="flex h-6 shrink-0 items-center gap-3 border-t border-border-w bg-status px-3 text-muted"
      style={{ fontSize: "var(--t-caption)" }}
    >
      {name && <span className="text-text2">{name}</span>}
      {status === "running" && <span className="text-accent">● ekip çalışıyor…</span>}
      {totals && (
        <span className="flex items-center gap-2.5 text-text2">
          <span className="flex items-center gap-1">
            <Cpu size={11} className="text-faint" />
            <CountUp value={totals.tokens} format={(v) => `${Math.round(v)} tok`} />
          </span>
          <span className="flex items-center gap-1">
            <Coins size={11} className="text-faint" />
            <CountUp value={totals.cost_usd} format={(v) => `$${v.toFixed(4)}`} />
          </span>
          <span className="text-faint">{totals.latency_s.toFixed(1)} sn</span>
        </span>
      )}
      <div className="flex-1" />
      {activeRel && (
        <>
          {dirty && <span className="text-warn">● kaydedilmedi</span>}
          <span>{langForPath(activeRel)}</span>
        </>
      )}
    </footer>
  );
}
