/* AiPanel — sağ panel: EKİP pipeline (imza) + Akış/Değişiklikler sekmeleri +
   composer + geçmiş çekmecesi. Öneri gelince Değişiklikler'e otomatik geçer. */

import { useEffect, useState } from "react";
import { Clock, MessageSquareText, FileDiff } from "lucide-react";
import { useRun } from "@/state/run";
import { Pipeline } from "./Pipeline";
import { Chat } from "./Chat";
import { Changes } from "./Changes";
import { Composer } from "./Composer";
import { HistoryDrawer } from "./HistoryDrawer";

type Tab = "chat" | "changes";

export function AiPanel() {
  const [tab, setTab] = useState<Tab>("chat");
  const [historyOpen, setHistoryOpen] = useState(false);
  const diffCount = useRun((s) => s.diffs.length);
  const status = useRun((s) => s.status);

  // öneri hazır olunca Değişiklikler sekmesine geç (desktop.py _focus_view deseni)
  useEffect(() => {
    if (status === "done" && diffCount > 0) setTab("changes");
    if (status === "running") setTab("chat");
  }, [status, diffCount]);

  return (
    <aside className="relative flex h-full w-full flex-col bg-side">
      {/* başlık */}
      <div className="material-panel flex h-10 shrink-0 items-center justify-between border-b border-border-w px-3">
        <span
          className="text-muted"
          style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
        >
          AI EKİBİ
        </span>
        <button
          onClick={() => setHistoryOpen(true)}
          title="Geçmiş koşular"
          aria-label="Geçmiş"
          className="icon-btn size-7"
        >
          <Clock size={14} />
        </button>
      </div>

      <Pipeline />

      {/* sekmeler */}
      <div className="flex shrink-0 border-b border-border-w bg-panel/40 px-2 py-1">
        {(
          [
            { id: "chat", label: "Akış", Icon: MessageSquareText, badge: 0 },
            { id: "changes", label: "Değişiklikler", Icon: FileDiff, badge: diffCount },
          ] as const
        ).map(({ id, label, Icon, badge }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={
              "pressable relative flex flex-1 items-center justify-center gap-1.5 rounded-[var(--r-sm)] py-1.5 " +
              (tab === id ? "bg-card2 text-text" : "text-muted hover:text-text2")
            }
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            <Icon size={13} strokeWidth={1.9} />
            {label}
            {badge > 0 && (
              <span
                className="rounded-[var(--r-pill)] bg-accentdim px-1.5 text-accent"
                style={{ fontSize: "10px", fontWeight: 700 }}
              >
                {badge}
              </span>
            )}
            {tab === id && <span className="absolute inset-x-4 bottom-0 h-[2px] rounded-t bg-accent" />}
          </button>
        ))}
      </div>

      {/* içerik */}
      <div className="min-h-0 flex-1">
        {tab === "chat" ? <Chat /> : <Changes />}
      </div>

      <Composer />
      <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </aside>
  );
}
