/* Composer — görev kutusu + rol→model seçimleri + Çalıştır/Durdur.
   Enter davranışı prefs.enterToSend'e bağlı (Shift+Enter yeni satır).
   F6 (@-mentions): "@" yazınca proje dosya/klasörlerini fuzzy filtreleyen
   satır-içi bir açılır liste açılır; seçim metne değil ayrı bir `mentions`
   dizisine eklenir ve textarea'nın üstünde çip olarak gösterilir. */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  BrainCircuit, Code2, SearchCheck, Play, Square, ClipboardCheck, X, Folder,
  CornerDownLeft, type LucideIcon,
} from "lucide-react";
import { useRun, MAX_MENTIONS } from "@/state/run";
import { useUi } from "@/state/ui";
import { useSettings } from "@/state/settings";
import { useKeys } from "@/state/keys";
import { bridge, ProviderInfo, Role } from "@/bridge";
import { Select, SelectGroup } from "@/components/ui/Select";
import { Button } from "@/components/ui";
import { fuzzyFilter, FuzzyHit } from "@/lib/fuzzy";
import { fileIcon } from "@/lib/fileIcons";
import { toast } from "@/components/toasts/toasts";

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

// ---------------- F6 (@-mentions) ----------------

interface MentionCandidate {
  path: string;
  isDir: boolean;
}

/** project.listFiles()'ın döndürdüğü düz dosya listesinden benzersiz üst
    klasörleri de çıkarır — "@src/" gibi bir klasör bahsi de seçilebilsin. */
function buildMentionCandidates(files: string[]): MentionCandidate[] {
  const dirs = new Set<string>();
  for (const f of files) {
    const parts = f.split("/");
    for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
  }
  const fileCandidates = files.map((path) => ({ path, isDir: false }));
  const dirCandidates = [...dirs].sort().map((path) => ({ path, isDir: true }));
  return [...dirCandidates, ...fileCandidates];
}

/** composer mount'ta bir kez proje dosya listesini yükler (quick-open ile
    aynı `project.listFiles` kaynağı — bkz. lib/commands.ts openFilesPalette). */
function useMentionCandidates(): MentionCandidate[] {
  const [files, setFiles] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    void bridge.call("project.listFiles", {}).then(({ files: f }) => {
      if (!cancelled) setFiles(f);
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  return useMemo(() => buildMentionCandidates(files), [files]);
}

interface MentionMatch {
  key: string;
  candidate: MentionCandidate;
  hit: FuzzyHit;
}

function MentionHighlight({ text, hit }: { text: string; hit: FuzzyHit }) {
  const marks = new Set(hit.indices);
  return (
    <>
      {[...text].map((ch, i) => (
        <span key={i} className={marks.has(i) ? "text-accent" : undefined} style={marks.has(i) ? { fontWeight: 700 } : undefined}>
          {ch}
        </span>
      ))}
    </>
  );
}

/** Composer içi satır-içi @-bahis açılır listesi (Palette.tsx'in dosya modu
    ile aynı fuzzy eşleme/kısayol deseni, ama ayrı bir overlay yerine
    textarea'nın altında konumlanan küçük bir kart). */
function MentionPopup({
  matches, sel, onPick, onHover,
}: {
  matches: MentionMatch[];
  sel: number;
  onPick: (m: MentionMatch) => void;
  onHover: (i: number) => void;
}) {
  return (
    <div
      role="listbox"
      aria-label="Bahsedilecek dosya/klasör"
      className="absolute bottom-full left-0 z-[var(--z-menu)] mb-1.5 max-h-[220px] w-[min(360px,100%)] overflow-y-auto rounded-[var(--r-md)] border border-border-w bg-panel py-1"
      style={{ boxShadow: "var(--shadow-2)" }}
    >
      {matches.length === 0 ? (
        <div className="px-3 py-2 text-muted" style={{ fontSize: "var(--t-label)" }}>
          Eşleşme yok
        </div>
      ) : (
        matches.map((m, i) => {
          const on = i === sel;
          const { Icon, color } = m.candidate.isDir
            ? { Icon: Folder, color: undefined }
            : fileIcon(m.candidate.path.split("/").pop()!);
          return (
            <div
              key={m.key}
              role="option"
              aria-selected={on}
              onPointerEnter={() => onHover(i)}
              onMouseDown={(e) => {
                e.preventDefault(); // textarea blur/seçim kaybını engelle
                onPick(m);
              }}
              className={
                "flex cursor-pointer items-center gap-2 px-3 py-1.5 " +
                (on ? "bg-accentdim/60 text-text" : "text-text2 hover:bg-card/45")
              }
              style={{ fontSize: "var(--t-label)" }}
            >
              <Icon size={13} strokeWidth={1.9} className="shrink-0 opacity-90" style={color ? { color } : undefined} />
              <span className="min-w-0 flex-1 truncate">
                <MentionHighlight text={m.candidate.path + (m.candidate.isDir ? "/" : "")} hit={m.hit} />
              </span>
              {on && <CornerDownLeft size={12} className="shrink-0 text-faint" />}
            </div>
          );
        })
      )}
    </div>
  );
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
  const mentions = useRun((s) => s.mentions);
  const addMention = useRun((s) => s.addMention);
  const removeMention = useRun((s) => s.removeMention);
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

  // ---------------- F6 (@-mentions): "@" tetikleyici ----------------
  const mentionCandidates = useMentionCandidates();
  const [mentionQuery, setMentionQuery] = useState<string | null>(null); // null → kapalı
  const [mentionAt, setMentionAt] = useState(0); // task içindeki "@" indeksi
  const [mentionSel, setMentionSel] = useState(0);

  const mentionMatches = useMemo<MentionMatch[]>(() => {
    if (mentionQuery === null) return [];
    return fuzzyFilter(mentionQuery, mentionCandidates, (c) => c.path, 8).map(({ item, hit }) => ({
      key: item.path, candidate: item, hit,
    }));
  }, [mentionQuery, mentionCandidates]);

  useEffect(() => setMentionSel(0), [mentionMatches.length, mentionQuery]);

  // imleçten geriye bakıp açık bir "@sorgu" (boşluksuz) var mı diye bulur.
  function detectMentionTrigger(value: string, caret: number): { at: number; query: string } | null {
    const uptoCaret = value.slice(0, caret);
    const at = uptoCaret.lastIndexOf("@");
    if (at === -1) return null;
    const query = uptoCaret.slice(at + 1);
    if (/\s/.test(query)) return null; // boşluk görüldüyse tetikleyici kapanmış
    return { at, query };
  }

  function closeMentionPopup() {
    setMentionQuery(null);
  }

  function pickMention(m: MentionMatch) {
    const ta = taRef.current;
    const caret = ta ? ta.selectionStart : task.length;
    const before = task.slice(0, mentionAt);
    const after = task.slice(caret);
    const nextTask = before + after;
    setTask(nextTask);
    closeMentionPopup();
    if (!addMention(m.candidate.path)) {
      toast.info(`En fazla ${MAX_MENTIONS} bahis eklenebilir.`);
      return;
    }
    requestAnimationFrame(() => {
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(before.length, before.length);
    });
  }

  const onChangeTask = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setTask(value);
    const trigger = detectMentionTrigger(value, e.target.selectionStart);
    if (trigger) {
      setMentionAt(trigger.at);
      setMentionQuery(trigger.query);
    } else {
      closeMentionPopup();
    }
  };

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

  const mentionOpen = mentionQuery !== null;

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionOpen) {
      if (e.key === "Escape") {
        e.preventDefault();
        closeMentionPopup();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionSel((s) => Math.min(s + 1, Math.max(0, mentionMatches.length - 1)));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionSel((s) => Math.max(s - 1, 0));
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && mentionMatches[mentionSel]) {
        e.preventDefault();
        pickMention(mentionMatches[mentionSel]);
        return;
      }
    }
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
      {mentions.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1.5">
          {mentions.map((path) => {
            const known = mentionCandidates.find((c) => c.path === path);
            const isDir = known ? known.isDir : !path.split("/").pop()?.includes(".");
            const label = path.split("/").pop() || path;
            return (
              <span
                key={path}
                title={path}
                className="inline-flex max-w-[220px] items-center gap-1 rounded-[var(--r-pill)] bg-accentdim px-2 py-0.5 text-accent"
                style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
              >
                {isDir ? <Folder size={11} strokeWidth={2} className="shrink-0" /> : null}
                <span className="truncate">@{label}</span>
                {!locked && (
                  <button
                    type="button"
                    onClick={() => removeMention(path)}
                    aria-label={`Bahsi kaldır: ${path}`}
                    className="shrink-0 rounded-full hover:text-text"
                  >
                    <X size={11} strokeWidth={2.2} />
                  </button>
                )}
              </span>
            );
          })}
        </div>
      )}
      <div className="flex items-end gap-2">
        <div className="relative min-w-0 flex-1">
          {mentionOpen && !locked && (
            <MentionPopup
              matches={mentionMatches}
              sel={mentionSel}
              onHover={setMentionSel}
              onPick={pickMention}
            />
          )}
          <textarea
            ref={taRef}
            value={task}
            onChange={onChangeTask}
            onKeyDown={onKey}
            onBlur={closeMentionPopup}
            placeholder={locked ? "" : "Görev yazın. @dosya ile referans ekleyin."}
            rows={2}
            spellCheck={false}
            readOnly={locked}
            aria-label={reviewReady ? "İnceleme tamamlanmayı bekliyor" : "Ekip görevi"}
            className="selectable min-h-[54px] w-full resize-none rounded-[var(--r-md)] border border-border-w2 bg-field px-3 py-2 text-text outline-none transition-colors placeholder:text-faint focus:border-accent"
            style={{ fontSize: "var(--t-body)" }}
          />
        </div>
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
