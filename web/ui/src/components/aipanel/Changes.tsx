/* Changes — önerilen dosya değişiklikleri: checkbox'lı liste, satıra tık →
   merkez diff (Cursor deseni), Uygula / Vazgeç. changes_panel.py'nin halefi. */

import { FileDiff, FilePlus2, Check, X, RotateCcw, ShieldCheck } from "lucide-react";
import { useRun } from "@/state/run";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";
import { Badge, Button, EmptyState } from "@/components/ui";

export function Changes() {
  const diffs = useRun((s) => s.diffs);
  const verdict = useRun((s) => s.verdict);
  const verdictNote = useRun((s) => s.verdictNote);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const checkpointId = useRun((s) => s.checkpointId);
  const checkpointBusy = useRun((s) => s.checkpointBusy);
  const toggle = useRun((s) => s.toggleDiff);
  const apply = useRun((s) => s.apply);
  const reject = useRun((s) => s.reject);
  const restoreCheckpoint = useRun((s) => s.restoreCheckpoint);
  const openDiff = useEditor((s) => s.openDiff);
  const activeDiff = useEditor((s) => s.diff?.path ?? null);

  if (diffs.length === 0) {
    const ok = runStage === "applied" || runStage === "restored";
    const [EmptyIcon, title, description] =
      runStage === "applied"
        ? ([ShieldCheck, "Değişiklikler uygulandı",
            "Bu turun checkpoint'i geri alma için hazır."] as const)
        : runStage === "restored"
          ? ([RotateCcw, "Checkpoint geri yüklendi",
              "Dosyalar checkpoint anına döndürüldü. Yeni bir tur başlatabilirsiniz."] as const)
          : runStage === "ready"
            ? ([FileDiff, "Uygulanacak dosya yok",
                "Ekip bu tur için dosya değişikliği önermedi."] as const)
            : ([FileDiff, "Öneri bekleniyor",
                "Ekip incelemeyi bitirdiğinde dosya değişiklikleri burada listelenir."] as const);
    return (
      <EmptyState
        icon={EmptyIcon}
        tone={ok ? "ok" : "neutral"}
        title={title}
        description={description}
        className="h-full"
        action={
          runStage === "applied" && checkpointId ? (
            <Button
              variant="warn-outline"
              size="sm"
              icon={RotateCcw}
              loading={checkpointBusy}
              aria-busy={checkpointBusy}
              onClick={() => void restoreCheckpoint()}
            >
              Geri Al
            </Button>
          ) : undefined
        }
      />
    );
  }

  const checkedCount = diffs.filter((d) => d.checked).length;
  const canApply = status !== "running" && checkedCount > 0 && !checkpointBusy;

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-border-w px-3 py-2">
        <div className="flex items-start gap-2">
          <Badge tone={verdict === "APPROVED" ? "ok" : "warn"}>
            {verdict === "APPROVED" ? "İNCELEME ONAYI" : "İNCELEME NOTU"}
          </Badge>
          <div className="min-w-0 flex-1">
            <p className="text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.35 }}>
              {verdictNote || `${checkedCount}/${diffs.length} dosya seçili; uygulamadan önce son kontrolünüz alınır.`}
            </p>
            <p className="mt-0.5 text-faint" style={{ fontSize: "var(--t-caption)" }}>{checkedCount}/{diffs.length} dosya uygulamaya dahil</p>
          </div>
        </div>
      </div>
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
                aria-label={`${d.path} değişikliğini seç`}
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
                <Badge tone="ok" className="shrink-0">
                  <FilePlus2 size={10} /> YENİ
                </Badge>
              )}
            </div>
          );
        })}
      </div>

      <div className="material-panel border-t border-border-w p-2.5">
        <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)" }}>
          <ShieldCheck size={12} className="text-ok" /> Uygulamadan önce checkpoint oluşturulur.
        </div>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            icon={Check}
            block
            onClick={() => void apply()}
            disabled={!canApply}
            loading={checkpointBusy}
            aria-busy={checkpointBusy}
          >
            {checkpointBusy ? "Uygulanıyor…" : `Uygula · checkpoint oluştur (${checkedCount})`}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={X}
            onClick={() => void reject()}
            disabled={checkpointBusy}
          >
            Vazgeç
          </Button>
        </div>
      </div>
    </div>
  );
}
