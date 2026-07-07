/* HistoryDrawer — geçmiş koşular (history.list): göreli zaman + verdict noktası +
   görev; tıkla → görevi composer'a geri yükle. history_panel.py'nin halefi. */

import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { X, Clock } from "lucide-react";
import { bridge, HistoryItem } from "@/bridge";
import { useRun } from "@/state/run";
import { toast } from "@/components/toasts/toasts";

function relTime(ts: number): string {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return `${Math.floor(d)} sn önce`;
  if (d < 3600) return `${Math.floor(d / 60)} dk önce`;
  if (d < 86400) return `${Math.floor(d / 3600)} sa önce`;
  if (d < 172800) return "dün";
  const dt = new Date(ts * 1000);
  return `${String(dt.getDate()).padStart(2, "0")}.${String(dt.getMonth() + 1).padStart(2, "0")}`;
}

export function HistoryDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const setTask = useRun((s) => s.setTask);

  useEffect(() => {
    if (!open) return;
    bridge
      .call("history.list", {})
      .then((r) => setItems(r.items))
      .catch(() => toast.err("Geçmiş yüklenemedi."));
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0, x: 16 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 16 }}
          transition={{ duration: 0.16, ease: [0.33, 1, 0.68, 1] }}
          className="absolute inset-0 z-20 flex flex-col bg-side"
        >
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-border-w px-3">
            <span
              className="flex items-center gap-1.5 text-muted"
              style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
            >
              <Clock size={12} /> GEÇMİŞ
            </span>
            <button onClick={onClose} aria-label="Kapat" className="rounded p-1 text-faint hover:bg-card hover:text-text2">
              <X size={14} />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {items.length === 0 ? (
              <p className="px-2 py-3 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                Henüz koşu yok — bir görev çalıştırınca burada birikir.
              </p>
            ) : (
              items.map((it, i) => (
                <button
                  key={i}
                  onClick={() => {
                    setTask(it.task);
                    onClose();
                  }}
                  className="mb-1 w-full rounded-[var(--r-sm)] border border-border-w bg-card px-2.5 py-2 text-left transition-colors hover:bg-card2"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={
                        "size-1.5 shrink-0 rounded-full " +
                        (it.verdict === "APPROVED" ? "bg-ok" : it.verdict === "NEEDS_FIX" ? "bg-warn" : "bg-faint")
                      }
                    />
                    <span className="min-w-0 flex-1 truncate text-text2" style={{ fontSize: "var(--t-label)" }}>
                      {it.task}
                    </span>
                  </div>
                  <div className="mt-0.5 flex gap-2 pl-3.5 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                    <span>{relTime(it.ts)}</span>
                    <span>· {it.files.length} dosya</span>
                    <span>· {it.tokens} tok</span>
                    <span>· ${it.cost_usd.toFixed(4)}</span>
                  </div>
                </button>
              ))
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
