/* StatusBar — alt durum çubuğu: proje adı, git dalı (P6), koşu HUD'u
   (token/maliyet count-up), aktif dosya dili. */

import { Coins, Cpu, GitBranch } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useRun } from "@/state/run";
import { useScm } from "@/state/scm";
import { useUi } from "@/state/ui";
import { langForPath } from "@/lib/monaco";
import { useLsp } from "@/lib/lsp";
import { CountUp } from "./CountUp";

/** P7: Python dil sunucusu durumu — yalnız .py dosyası aktifken görünür. */
function LspBadge({ activeRel }: { activeRel: string }) {
  const status = useLsp((s) => s.status);
  if (langForPath(activeRel) !== "python" || status === "off") return null;
  return status === "starting" ? (
    <span className="text-faint">dil sunucusu hazırlanıyor…</span>
  ) : (
    <span title="basedpyright hazır" style={{ color: "var(--green)" }}>⏻ Py</span>
  );
}

export function StatusBar() {
  const name = useWorkspace((s) => s.name);
  const activeRel = useEditor((s) => s.activeRel);
  const dirty = useEditor((s) => s.tabs.find((t) => t.rel === s.activeRel)?.dirty);
  const totals = useRun((s) => s.totals);
  const status = useRun((s) => s.status);
  const isRepo = useScm((s) => s.isRepo);
  const branch = useScm((s) => s.branch);
  const ahead = useScm((s) => s.ahead);
  const changeCount = useScm((s) => s.staged.length + s.unstaged.length);
  const showSideView = useUi((s) => s.showSideView);

  return (
    <footer
      className="flex h-6 shrink-0 items-center gap-3 border-t border-border-w bg-status px-3 text-muted"
      style={{ fontSize: "var(--t-caption)" }}
    >
      {name && <span className="text-text2">{name}</span>}
      {isRepo && (
        <button
          onClick={() => showSideView("scm")}
          title="Kaynak denetimi (Ctrl+Shift+G)"
          className="flex items-center gap-1 rounded px-1 transition-colors hover:bg-card hover:text-text2"
        >
          <GitBranch size={11} />
          <span className="max-w-[160px] truncate">{branch}</span>
          {ahead > 0 && <span className="text-faint">↑{ahead}</span>}
          {changeCount > 0 && (
            <span style={{ color: "var(--amber)" }}>●{changeCount}</span>
          )}
        </button>
      )}
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
          <LspBadge activeRel={activeRel} />
          <span>{langForPath(activeRel)}</span>
        </>
      )}
    </footer>
  );
}
