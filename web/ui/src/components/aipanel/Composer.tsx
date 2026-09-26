/* Composer — görev kutusu + rol→model seçimleri + Çalıştır/Durdur.
   Enter davranışı prefs.enterToSend'e bağlı (Shift+Enter yeni satır). */

import { useEffect, useRef } from "react";
import { BrainCircuit, Code2, SearchCheck, Play, Square, ClipboardCheck, type LucideIcon } from "lucide-react";
import { useRun } from "@/state/run";
import { useUi } from "@/state/ui";
import { useSettings } from "@/state/settings";
import { useKeys } from "@/state/keys";
import { ProviderInfo, Role } from "@/bridge";
import { Select, SelectGroup } from "@/components/ui/Select";
import { Button } from "@/components/ui";

const ROLE_ICONS: Record<Role, LucideIcon> = {
  planner: BrainCircuit,
  coder: Code2,
  reviewer: SearchCheck,
};

// Backend label'ları (providers.py CATALOG) hesap/API farkını Composer'da
// belirsiz bırakır ("Gemini" hem CLI hem API girdisinde geçer) — burada
// yalnız Composer'ın dropdown'ı için hesap/API'yi netleştiren görünen adlar.
const ACCOUNT_LABELS: Record<string, string> = {
  claude: "Claude (Claude Code hesabı)",
  "codex-cli": "ChatGPT (Codex hesabı)",
  "gemini-cli": "Gemini (Google hesabı)",
  "qwen-code": "Qwen (Qwen Code hesabı)",
};
const API_LABELS: Record<string, string> = {
  anthropic: "Claude API",
  openai: "OpenAI API",
  gemini: "Gemini API",
  deepseek: "DeepSeek API",
};

function displayLabel(p: ProviderInfo): string {
  if (p.kind === "cli") return ACCOUNT_LABELS[p.id] ?? `${p.label} (hesap)`;
  return API_LABELS[p.id] ?? `${p.label} API`;
}

function availabilityHint(p: ProviderInfo): string | undefined {
  if (p.kind === "cli") {
    if (!p.cliAvailable) return "— kurulu değil";
    if (!p.npxAvailable) return "— Node.js (npx) eksik";
    return undefined;
  }
  return p.ok ? undefined : "— anahtar yok";
}

/** Composer'ın rol dropdown'u için "Hesap ile" / "API anahtarı ile" grupları. */
function providerGroups(providers: ProviderInfo[]): SelectGroup[] {
  const account = providers.filter((p) => p.kind === "cli");
  const api = providers.filter((p) => p.kind !== "cli");
  const toOption = (p: ProviderInfo) => {
    const hint = availabilityHint(p);
    return { value: p.id, label: displayLabel(p), dim: !!hint, hint };
  };
  const groups: SelectGroup[] = [];
  if (account.length) groups.push({ label: "Hesap ile", options: account.map(toOption) });
  if (api.length) groups.push({ label: "API anahtarı ile", options: api.map(toOption) });
  return groups;
}

function RoleSelect({ role }: { role: Role }) {
  const providers = useRun((s) => s.providers);
  const value = useRun((s) => s.routing[role]);
  const setRouting = useRun((s) => s.setRouting);
  const Icon = ROLE_ICONS[role];
  const groups = providerGroups(providers);

  return (
    <Select
      value={value}
      groups={groups}
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
  // routing'deki herhangi bir rol yeni (pipeline) motorca desteklenmiyorsa
  // koşu klasik motora düşer — decision 4: bunu kullanıcıya ipucu olarak göster.
  const runProviders = useRun((s) => s.providers);
  const unsupported = runProviders.length
    ? [...new Set(Object.values(routing))].filter((p) => {
        const info = runProviders.find((x) => x.id === p);
        return info && info.engineSupported === false;
      })
    : [];

  const helper = running
    ? "Koşu sürüyor. Gerekirse durdurun."
    : reviewReady
      ? "İnceleme hazır. Dosyaları uygulayın veya vazgeçin."
      : runStage === "error"
        ? "Koşu tamamlanmadı. Görevi düzenleyip tekrar çalıştırın."
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
          {missing.join(", ")} için anahtar veya CLI eksik. {" "}
          <button onClick={() => setSettingsOpen(true)} className="underline underline-offset-2 hover:text-text">
            Ayarlar'dan ekle
          </button>
        </div>
      )}
      {!helper && missing.length === 0 && unsupported.length > 0 && (
        <div className="mb-1.5 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)" }}>
          {unsupported.join(", ")} yeni motoru desteklemiyor — bu koşu klasik motorla yürütülecek.
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          ref={taRef}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={onKey}
          placeholder={locked ? "" : "Görev yazın. Örnek: utils.py tarih biçimini ISO 8601 yap"}
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
