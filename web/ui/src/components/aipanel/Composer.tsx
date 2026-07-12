/* Composer — görev kutusu + rol→model seçimleri + Çalıştır/Durdur.
   Enter davranışı prefs.enterToSend'e bağlı (Shift+Enter yeni satır). */

import { useEffect, useRef } from "react";
import { BrainCircuit, Code2, SearchCheck, Play, Square, ClipboardCheck, type LucideIcon } from "lucide-react";
import { useRun } from "@/state/run";
import { useUi } from "@/state/ui";
import { useSettings } from "@/state/settings";
import { useKeys } from "@/state/keys";
import { Role } from "@/bridge";
import { Select } from "@/components/ui/Select";
import { Button } from "@/components/ui";

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
  const focusNonce = useUi((s) => s.composerFocusNonce);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const running = status === "running";
  const reviewReady = runStage === "ready";
  const locked = running || reviewReady;

  // command center "Görev ver" → composer'a odaklan (kilitli değilse imleç sona gider)
  useEffect(() => {
    if (focusNonce === 0) return;
    const ta = taRef.current;
    if (!ta || locked) return;
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }, [focusNonce, locked]);
  // beta onboarding: seçili routing'de anahtarı/CLI'ı eksik sağlayıcı uyarısı
  const routing = useRun((s) => s.routing);
  const keyProviders = useKeys((s) => s.providers);
  const keysLoaded = useKeys((s) => s.loaded);
  const loadKeys = useKeys((s) => s.load);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  useEffect(() => {
    if (!keysLoaded) void loadKeys();
  }, [keysLoaded, loadKeys]);
  const missing = keysLoaded
    ? [...new Set(Object.values(routing))].filter((p) => keyProviders[p] && !keyProviders[p].ok)
    : [];

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
      {!helper && missing.length > 0 && (
        <div className="mb-1.5 flex items-center gap-1.5 text-warn" style={{ fontSize: "var(--t-caption)" }}>
          {missing.join(", ")} için anahtar/CLI eksik —{" "}
          <button onClick={() => setSettingsOpen(true)} className="underline underline-offset-2 hover:text-text">
            Ayarlar'dan ekle
          </button>
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          ref={taRef}
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
        {/* üç durum aynı slotta yer değiştirir → hepsi 36px kare Button (ikon-only) */}
        {running ? (
          <Button
            variant="danger-outline"
            icon={Square}
            onClick={() => void cancel()}
            title="Durdur"
            aria-label="Koşuyu durdur"
            className="w-9 shrink-0 px-0"
          />
        ) : reviewReady ? (
          <Button
            variant="secondary"
            icon={ClipboardCheck}
            disabled
            title="Önce inceleme kararını verin"
            aria-label="İnceleme kararı bekleniyor"
            className="w-9 shrink-0 px-0"
          />
        ) : (
          <Button
            variant="primary"
            icon={Play}
            onClick={() => void start()}
            title="Çalıştır (Enter)"
            aria-label="Çalıştır"
            className="w-9 shrink-0 px-0"
          />
        )}
      </div>
    </div>
  );
}
