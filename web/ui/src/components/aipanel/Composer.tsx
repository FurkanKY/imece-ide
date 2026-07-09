/* Composer — görev kutusu + rol→model seçimleri + Çalıştır/Durdur.
   Enter davranışı prefs.enterToSend'e bağlı (Shift+Enter yeni satır). */

import { BrainCircuit, Code2, SearchCheck, Play, Square, ClipboardCheck, type LucideIcon } from "lucide-react";
import { useRun } from "@/state/run";
import { useSettings } from "@/state/settings";
import { Role } from "@/bridge";
import { Select } from "@/components/ui/Select";

const ROLE_ICONS: Record<Role, LucideIcon> = {
  planner: BrainCircuit,
  coder: Code2,
  reviewer: SearchCheck,
};

function RoleSelect({ role }: { role: Role }) {
  const providers = useRun((s) => s.providers);
  const value = useRun((s) => s.routing[role]);
  const setRouting = useRun((s) => s.setRouting);
  const Icon = ROLE_ICONS[role];

  return (
    <Select
      value={value}
      options={providers}
      onChange={(v) => setRouting(role, v)}
      ariaLabel={role}
      icon={<Icon size={13} className="shrink-0 text-muted" strokeWidth={1.9} />}
    />
  );
}

export function Composer() {
  const task = useRun((s) => s.task);
  const setTask = useRun((s) => s.setTask);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const start = useRun((s) => s.start);
  const cancel = useRun((s) => s.cancel);
  const enterToSend = useSettings((s) => s.prefs?.enterToSend ?? true);
  const running = status === "running";
  const reviewReady = runStage === "ready";
  const locked = running || reviewReady;
  const helper = running
    ? "Ekip çalışıyor — gerekirse durdurun."
    : reviewReady
      ? "İnceleme hazır — dosyaları uygulayın veya vazgeçin."
      : runStage === "error"
        ? "Bu tur tamamlanamadı — görevi düzenleyin veya yeniden çalıştırın."
        : null;

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && enterToSend) {
      e.preventDefault();
      if (!locked) void start();
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!locked) void start();
    }
  };

  return (
    <div className="material-panel border-t border-border-w p-2.5">
      <div className="mb-2 flex gap-1.5">
        <RoleSelect role="planner" />
        <RoleSelect role="coder" />
        <RoleSelect role="reviewer" />
      </div>
      {helper && (
        <div className="mb-1.5 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)" }}>
          {reviewReady && <ClipboardCheck size={12} className="text-warn" />} {helper}
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={onKey}
          placeholder={locked ? "" : "Göreviniz… (ör. utils.py'deki tarih biçimini ISO 8601 yap)"}
          rows={2}
          spellCheck={false}
          readOnly={locked}
          aria-label={reviewReady ? "İnceleme tamamlanmayı bekliyor" : "Ekip görevi"}
          className="selectable min-h-[54px] w-full resize-none rounded-[var(--r-md)] border border-border-w2 bg-field px-3 py-2 text-text outline-none transition-colors placeholder:text-faint focus:border-accent"
          style={{ fontSize: "var(--t-body)" }}
        />
        {running ? (
          <button
            onClick={() => void cancel()}
            title="Durdur"
            className="pressable flex size-9 shrink-0 items-center justify-center rounded-[var(--r-md)] border border-err/50 text-err hover:bg-err/15"
          >
            <Square size={15} strokeWidth={2.2} />
          </button>
        ) : reviewReady ? (
          <button
            disabled
            title="Önce inceleme kararını verin"
            aria-label="İnceleme kararı bekleniyor"
            className="flex size-9 shrink-0 items-center justify-center rounded-[var(--r-md)] border border-border-w text-faint opacity-60"
          >
            <ClipboardCheck size={15} strokeWidth={2.2} />
          </button>
        ) : (
          <button
            onClick={() => void start()}
            title="Çalıştır (Enter)"
            className="pressable flex size-9 shrink-0 items-center justify-center rounded-[var(--r-md)] bg-accent text-on-accent hover:bg-accent2"
            style={{ boxShadow: "var(--shadow-1)" }}
          >
            <Play size={15} strokeWidth={2.2} className="ml-0.5" />
          </button>
        )}
      </div>
    </div>
  );
}
