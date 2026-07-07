/* Composer — görev kutusu + rol→model seçimleri + Çalıştır/Durdur.
   Enter davranışı prefs.enterToSend'e bağlı (Shift+Enter yeni satır). */

import { BrainCircuit, Code2, SearchCheck, Play, Square, type LucideIcon } from "lucide-react";
import { useRun } from "@/state/run";
import { useSettings } from "@/state/settings";
import { Role } from "@/bridge";

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
    <label
      className="flex min-w-0 flex-1 items-center gap-1.5 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1"
      title={role}
    >
      <Icon size={13} className="shrink-0 text-muted" strokeWidth={1.9} />
      <select
        value={value}
        onChange={(e) => setRouting(role, e.target.value)}
        className="w-full min-w-0 cursor-pointer appearance-none bg-transparent text-text2 outline-none"
        style={{ fontSize: "var(--t-caption)" }}
      >
        {providers.map((p) => (
          <option key={p} value={p} className="bg-panel text-text">
            {p}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Composer() {
  const task = useRun((s) => s.task);
  const setTask = useRun((s) => s.setTask);
  const status = useRun((s) => s.status);
  const start = useRun((s) => s.start);
  const cancel = useRun((s) => s.cancel);
  const enterToSend = useSettings((s) => s.prefs?.enterToSend ?? true);
  const running = status === "running";

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && enterToSend) {
      e.preventDefault();
      if (!running) void start();
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!running) void start();
    }
  };

  return (
    <div className="border-t border-border-w bg-composer p-2.5">
      <div className="mb-2 flex gap-1.5">
        <RoleSelect role="planner" />
        <RoleSelect role="coder" />
        <RoleSelect role="reviewer" />
      </div>
      <div className="flex items-end gap-2">
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={onKey}
          placeholder="Göreviniz… (ör. utils.py'deki tarih biçimini ISO 8601 yap)"
          rows={2}
          spellCheck={false}
          className="selectable min-h-[54px] w-full resize-none rounded-[var(--r-md)] border border-border-w2 bg-field px-3 py-2 text-text outline-none transition-colors placeholder:text-faint focus:border-accent"
          style={{ fontSize: "var(--t-body)" }}
        />
        {running ? (
          <button
            onClick={() => void cancel()}
            title="Durdur"
            className="flex size-9 shrink-0 items-center justify-center rounded-[var(--r-md)] border border-err/50 text-err transition-colors hover:bg-err/15"
          >
            <Square size={15} strokeWidth={2.2} />
          </button>
        ) : (
          <button
            onClick={() => void start()}
            title="Çalıştır (Enter)"
            className="flex size-9 shrink-0 items-center justify-center rounded-[var(--r-md)] bg-accent text-on-accent transition-all hover:bg-accent2"
            style={{ boxShadow: "var(--shadow-1)" }}
          >
            <Play size={15} strokeWidth={2.2} className="ml-0.5" />
          </button>
        )}
      </div>
    </div>
  );
}
