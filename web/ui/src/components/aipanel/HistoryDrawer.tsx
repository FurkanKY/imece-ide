/* HistoryDrawer — geçmiş koşular (history.list): göreli zaman + verdict noktası +
   görev; tıkla → görevi composer'a geri yükle. history_panel.py'nin halefi. */

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { X, Clock, RotateCcw, ShieldCheck, FileText, Download } from "lucide-react";
import { bridge, Checkpoint, HistoryItem, Receipt } from "@/bridge";
import { useRun } from "@/state/run";
import { toast } from "@/components/toasts/toasts";
import { useSettings } from "@/state/settings";

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
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const setTask = useRun((s) => s.setTask);
  const restoreCheckpoint = useRun((s) => s.restoreCheckpoint);
  const activeCheckpointId = useRun((s) => s.checkpointId);
  const restoredCheckpointId = useRun((s) => s.lastRestoredCheckpointId);
  const checkpointBusy = useRun((s) => s.checkpointBusy);
  const reduceMotion = useReducedMotion();
  const animationsEnabled = useSettings((s) => s.prefs?.animations ?? true);
  const animate = animationsEnabled && !reduceMotion;

  useEffect(() => {
    if (!open) return;
    void Promise.all([bridge.call("history.list", {}), bridge.call("checkpoint.list", {})])
      .then(([history, checkpoints]) => { setItems(history.items); setCheckpoints(checkpoints.checkpoints); })
      .catch(() => toast.err("Geçmiş yüklenemedi."));
  }, [open]);

  const openReceipt = async (receiptId: string) => {
    try {
      const result = await bridge.call("receipt.get", { receiptId });
      setReceipt(result.receipt);
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Makbuz yüklenemedi.");
    }
  };

  const exportReceipt = async () => {
    if (!receipt) return;
    try {
      const { path } = await bridge.call("app.pickFolder", { title: "Makbuzu kaydetmek için klasör seç" });
      if (!path) return;
      const result = await bridge.call("receipt.export", { receiptId: receipt.id, directory: path });
      toast.ok(`Makbuz dışa aktarıldı: ${result.path}`);
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Makbuz dışa aktarılamadı.");
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0, transform: animate ? "translateX(16px)" : "none" }}
          animate={{ opacity: 1, transform: "translateX(0)" }}
          exit={{ opacity: 0, transform: animate ? "translateX(16px)" : "none" }}
          transition={{ duration: 0.16, ease: [0.33, 1, 0.68, 1] }}
          className="material-panel absolute inset-0 z-20 flex flex-col"
          style={{ background: "var(--panel)" }}
        >
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-border-w px-3">
            <span
              className="flex items-center gap-1.5 text-muted"
              style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
            >
              <Clock size={12} /> GEÇMİŞ
            </span>
            <button onClick={onClose} aria-label="Kapat" className="icon-btn size-7">
              <X size={14} />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {receipt && (
              <section className="mb-3 rounded-[var(--r-sm)] border border-accent/40 bg-accentdim/25 p-2.5">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}><FileText size={12} /> Değişiklik makbuzu</span>
                  <div className="flex items-center gap-1">
                    <button className="icon-btn size-6" aria-label="Makbuzu dışa aktar" title="Markdown dışa aktar" onClick={() => void exportReceipt()}><Download size={12} /></button>
                    <button className="icon-btn size-6" aria-label="Makbuzu kapat" onClick={() => setReceipt(null)}><X size={12} /></button>
                  </div>
                </div>
                <p className="text-text2" style={{ fontSize: "var(--t-caption)" }}>{receipt.task}</p>
                <p className="mt-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>{receipt.plan?.summary || "Plan özeti yok."}</p>
                <div className="mt-2 grid grid-cols-2 gap-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                  <span>Karar: {receipt.review.verdict}</span><span>{receipt.proposals.length} dosya önerisi</span>
                  <span>{receipt.metrics.tokens} tok · ${receipt.metrics.cost_usd.toFixed(4)}</span><span>Durum: {receipt.status}</span>
                </div>
                <p className="mt-2 border-t border-border-w pt-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>{receipt.verification.detail}</p>
              </section>
            )}
            {checkpoints.length > 0 && (
              <section className="mb-3">
                <div className="mb-1 flex items-center gap-1.5 px-1 text-muted" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}><ShieldCheck size={11} /> CHECKPOINTLER</div>
                <p className="mb-1.5 px-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>Magent checkpoint'i · Git commit'i değildir.</p>
                {checkpoints.map((checkpoint) => {
                  const state = checkpoint.id === activeCheckpointId
                    ? "GERİ ALINABİLİR"
                    : checkpoint.id === restoredCheckpointId
                      ? "GERİ DÖNÜLDÜ"
                      : null;
                  return (
                    <div key={checkpoint.id} className="material-card mb-1 flex items-center gap-2 rounded-[var(--r-sm)] border border-border-w px-2.5 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <p className="text-text2" style={{ fontSize: "var(--t-label)" }}>{checkpoint.files.length} dosya · {relTime(checkpoint.ts)}</p>
                          {state && <span className="rounded-[var(--r-pill)] border border-ok/40 px-1.5 text-ok" style={{ fontSize: "9px", fontWeight: 700 }}>{state}</span>}
                        </div>
                        <p className="truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>{checkpoint.files.join(", ")}</p>
                      </div>
                      <button
                        disabled={checkpointBusy}
                        onClick={() => { void restoreCheckpoint(checkpoint.id); }}
                        className="pressable flex shrink-0 items-center gap-1 rounded-[var(--r-sm)] border border-warn/30 px-2 py-1 text-warn hover:bg-warn/10 disabled:opacity-40"
                        style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
                      >
                        <RotateCcw size={12} /> Geri dön
                      </button>
                    </div>
                  );
                })}
              </section>
            )}
            {items.length === 0 ? (
              <p className="px-2 py-3 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                Henüz koşu yok — bir görev çalıştırınca burada birikir.
              </p>
            ) : (
              items.map((it, i) => (
                <div key={i} className="material-card pressable mb-1 rounded-[var(--r-sm)] border border-border-w hover:bg-card2">
                  <button
                    onClick={() => { setTask(it.task); onClose(); }}
                    className="w-full px-2.5 py-2 text-left"
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
                  {it.receipt_id && <button onClick={() => void openReceipt(it.receipt_id!)} className="mb-2 ml-3.5 flex items-center gap-1 text-accent hover:text-text" style={{ fontSize: "var(--t-caption)" }}><FileText size={11} /> Makbuz</button>}
                </div>
              ))
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
