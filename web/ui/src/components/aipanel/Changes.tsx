/* Changes — önerilen dosya değişiklikleri: checkbox'lı liste, satıra tık →
   merkez diff (Cursor deseni), Uygula / Vazgeç. changes_panel.py'nin halefi. */

import { FileDiff, FilePlus2, Check, X } from "lucide-react";
import { useRun } from "@/state/run";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";

export function Changes() {
  const diffs = useRun((s) => s.diffs);
  const verdict = useRun((s) => s.verdict);
  const status = useRun((s) => s.status);
  const toggle = useRun((s) => s.toggleDiff);
  const apply = useRun((s) => s.apply);
  const reject = useRun((s) => s.reject);
  const openDiff = useEditor((s) => s.openDiff);
  const activeDiff = useEditor((s) => s.diff?.path ?? null);

  if (diffs.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <FileDiff size={26} className="text-faint" strokeWidth={1.5} />
        <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
          Öneri bekleniyor — koşu bitince dosya değişiklikleri burada listelenir.
        </p>
      </div>
    );
  }

  const checkedCount = diffs.filter((d) => d.checked).length;
  const canApply = status !== "running" && checkedCount > 0;

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {diffs.map((d) => {
          const name = d.path.split("/").pop() ?? d.path;
          const { Icon, color } = fileIcon(name);
          const active = activeDiff === d.path;
          return (
            <div
              key={d.path}
              role="button"
              tabIndex={0}
              onClick={() => void openDiff(d.path)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); void openDiff(d.path); }
              }}
              className={
                "flex cursor-pointer items-center gap-2 rounded-[var(--r-sm)] px-2 py-1.5 " +
                (active ? "bg-accentdim" : "hover:bg-card")
              }
            >
              <input
                type="checkbox"
                checked={d.checked}
                onChange={() => toggle(d.path)}
                onClick={(e) => e.stopPropagation()}
                className="size-3.5 shrink-0 accent-[var(--accent)]"
              />
              <Icon size={14} strokeWidth={1.8} style={{ color }} className="shrink-0" />
              <span className="min-w-0 flex-1 truncate text-text2" style={{ fontSize: "var(--t-label)" }}>
                {d.path}
              </span>
              {d.isNew && (
                <span className="flex shrink-0 items-center gap-1 rounded-[var(--r-pill)] border border-ok/40 px-1.5 text-ok" style={{ fontSize: "10px", fontWeight: 700 }}>
                  <FilePlus2 size={10} /> YENİ
                </span>
              )}
            </div>
          );
        })}
      </div>

      <div className="material-panel border-t border-border-w p-2.5">
        {verdict && (
          <div
            className={
              "mb-2 inline-flex items-center rounded-[var(--r-pill)] border px-2 py-0.5 " +
              (verdict === "APPROVED" ? "border-ok/50 text-ok" : "border-warn/50 text-warn")
            }
            style={{ fontSize: "10px", fontWeight: 800, letterSpacing: "0.06em" }}
          >
            {verdict === "APPROVED" ? "ONAYLANDI" : "DÜZELTME GEREK"}
          </div>
        )}
        <div className="flex gap-2">
          <button
            onClick={() => void apply()}
            disabled={!canApply}
            className="pressable flex flex-1 items-center justify-center gap-1.5 rounded-[var(--r-sm)] bg-accent px-3 py-1.5 text-on-accent hover:bg-accent2 disabled:opacity-40"
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            <Check size={14} strokeWidth={2.2} /> Uygula ({checkedCount})
          </button>
          <button
            onClick={() => void reject()}
            className="pressable flex items-center justify-center gap-1.5 rounded-[var(--r-sm)] border border-border-w2 px-3 py-1.5 text-text2 hover:bg-card"
            style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          >
            <X size={14} /> Vazgeç
          </button>
        </div>
      </div>
    </div>
  );
}
