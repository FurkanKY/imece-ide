/* SettingsDialog — arayüz tercihleri: accent (canlı), yoğunluk, Enter davranışı,
   animasyonlar. settings_panel.py'nin halefi; değişiklik anında uygulanır + kalıcı. */

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { X, Check, KeyRound } from "lucide-react";
import { useUi } from "@/state/ui";
import { useSettings } from "@/state/settings";
import { useKeys } from "@/state/keys";
import { Prefs } from "@/bridge";
import { Logo } from "@/components/brand/Logo";
import { Button, StatusDot } from "@/components/ui";
import { toast } from "@/components/toasts/toasts";

const ACCENTS: { id: Prefs["accent"]; color: string }[] = [
  { id: "blue", color: "#6aa1ff" },
  { id: "indigo", color: "#8b8cf0" },
  { id: "violet", color: "#b07cf0" },
  { id: "green", color: "#4bd48a" },
  { id: "amber", color: "#e9b45a" },
  { id: "rose", color: "#ff7a8a" },
];

function Row({ label, hint, children }: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <div className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>
          {label}
        </div>
        {hint && (
          <div className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
            {hint}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

function Toggle({ on, onChange, ariaLabel }: {
  on: boolean;
  onChange: (v: boolean) => void;
  ariaLabel: string;
}) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={ariaLabel}
      onClick={() => onChange(!on)}
      className={
        "pressable relative h-5 w-9 shrink-0 rounded-[var(--r-pill)] border transition-colors " +
        (on ? "border-accent bg-accentdim" : "border-border-w2 bg-field")
      }
    >
      <span
        className={
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-[left,background-color] duration-[var(--dur-fast)] ease-[var(--ease-out)] " +
          (on ? "left-[18px] bg-accent" : "left-[3px] bg-muted")
        }
      />
    </button>
  );
}

function Segmented<T extends string>({ value, options, onChange }: {
  value: T;
  options: { id: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex shrink-0 rounded-[var(--r-sm)] border border-border-w bg-field p-0.5">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={
            "pressable rounded-[6px] px-2.5 py-1 transition-colors " +
            (value === o.id ? "bg-card2 text-text" : "text-muted hover:text-text2")
          }
          style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-caption)" }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** beta onboarding: anahtar durumu + kayıt. Anahtar geri OKUNMAZ — yalnız maske. */
function KeyRow({ name, label, placeholder }: { name: "deepseek" | "gemini"; label: string; placeholder: string }) {
  const status = useKeys((s) => s.providers[name]);
  const save = useKeys((s) => s.save);
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex items-center gap-2 py-1.5">
      <span className="flex w-24 shrink-0 items-center gap-1.5 text-text2" style={{ fontSize: "var(--t-label)" }}>
        <StatusDot tone={status?.ok ? "ok" : "warn"} /> {label}
      </span>
      <input
        type="password"
        value={val}
        onChange={(e) => setVal(e.target.value)}
        placeholder={status?.ok ? `kayıtlı (${status.masked}) — değiştirmek için yaz` : placeholder}
        aria-label={`${label} API anahtarı`}
        autoComplete="off"
        spellCheck={false}
        className="selectable min-w-0 flex-1 rounded-[var(--r-sm)] border border-border-w2 bg-field px-2.5 py-1.5 text-text outline-none placeholder:text-faint focus:border-accent"
        style={{ fontSize: "var(--t-caption)", fontFamily: "var(--font-mono)" }}
      />
      <Button
        size="sm"
        variant="secondary"
        loading={busy}
        disabled={!val.trim()}
        onClick={async () => {
          setBusy(true);
          try {
            await save({ [name]: val.trim() });
            setVal("");
            toast.ok(`${label} anahtarı kaydedildi.`);
          } catch (e) {
            toast.err(e instanceof Error ? e.message : "Anahtar kaydedilemedi.");
          } finally {
            setBusy(false);
          }
        }}
      >
        Kaydet
      </Button>
    </div>
  );
}

function ApiKeysSection() {
  const claude = useKeys((s) => s.providers.claude);
  const load = useKeys((s) => s.load);
  const loaded = useKeys((s) => s.loaded);
  useEffect(() => {
    if (!loaded) void load();
  }, [loaded, load]);
  return (
    <div className="py-2.5">
      <div className="mb-1 flex items-center gap-1.5 text-muted"
           style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
        <KeyRound size={11} /> API ANAHTARLARI
      </div>
      <div className="flex items-center gap-2 py-1.5">
        <span className="flex w-24 shrink-0 items-center gap-1.5 text-text2" style={{ fontSize: "var(--t-label)" }}>
          <StatusDot tone={claude?.ok ? "ok" : "err"} /> Claude
        </span>
        <span className="min-w-0 flex-1 truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {claude?.ok ? `Claude Code CLI hazır (${claude.detail})` : "Claude Code CLI PATH'te bulunamadı — kurulum: claude.com/claude-code"}
        </span>
      </div>
      <KeyRow name="deepseek" label="DeepSeek" placeholder="sk-…  (platform.deepseek.com)" />
      <KeyRow name="gemini" label="Gemini" placeholder="AIza…  (aistudio.google.com/apikey)" />
      <p className="mt-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>
        Anahtarlar yerelde .env dosyasına yazılır; hiçbir yere gönderilmez ve arayüze geri okunmaz.
      </p>
    </div>
  );
}

export function SettingsDialog() {
  const open = useUi((s) => s.settingsOpen);
  const setOpen = useUi((s) => s.setSettingsOpen);
  const prefs = useSettings((s) => s.prefs);
  const update = useSettings((s) => s.update);
  const reduceMotion = useReducedMotion();
  const animate = (prefs?.animations ?? true) && !reduceMotion;

  if (!prefs) return null;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: animate ? 0.15 : 0 }}
          className="fixed inset-0 z-[150] flex items-center justify-center overflow-y-auto bg-black/50 p-4"
          onPointerDown={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
          onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
        >
          <motion.div
            initial={{ opacity: 0, transform: "translateY(10px) scale(0.98)" }}
            animate={{ opacity: 1, transform: "translateY(0) scale(1)" }}
            exit={{ opacity: 0, transform: "translateY(6px) scale(0.98)" }}
            transition={{ duration: animate ? 0.18 : 0, ease: [0.33, 1, 0.68, 1] }}
            className="material-panel flex max-h-full w-[min(460px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[var(--r-lg)] border border-border-w"
            style={{ boxShadow: "var(--bevel-strong), var(--shadow-3)" }}
            role="dialog"
            aria-modal="true"
          >
            <div className="flex items-center justify-between border-b border-border-w px-4 py-3">
              <div className="flex items-center gap-2">
                <Logo size={16} />
                <h2 className="text-text" style={{ fontSize: "var(--t-title)", fontWeight: "var(--w-title)" }}>
                  Ayarlar
                </h2>
              </div>
              <button
                onClick={() => setOpen(false)}
                aria-label="Kapat"
                className="icon-btn size-7"
              >
                <X size={15} />
              </button>
            </div>

            <div className="min-h-0 overflow-y-auto divide-y divide-[var(--line)] px-4 py-1">
              <Row label="Vurgu rengi" hint="Tüm arayüze anında uygulanır">
                <div className="flex gap-1.5">
                  {ACCENTS.map((a) => (
                    <button
                      key={a.id}
                      title={a.id}
                      aria-label={`Accent: ${a.id}`}
                      onClick={() => void update({ accent: a.id })}
                      className="pressable flex size-6 items-center justify-center rounded-full border"
                      style={{
                        background: a.color,
                        borderColor: prefs.accent === a.id ? "white" : "transparent",
                      }}
                    >
                      {prefs.accent === a.id && <Check size={12} strokeWidth={3} color="#0a0b0d" />}
                    </button>
                  ))}
                </div>
              </Row>

              <Row label="Yoğunluk" hint="Aralıkları sıkılaştırır">
                <Segmented
                  value={prefs.density}
                  options={[
                    { id: "comfortable", label: "Rahat" },
                    { id: "compact", label: "Sıkı" },
                  ]}
                  onChange={(v) => void update({ density: v })}
                />
              </Row>

              <Row label="Enter gönderir" hint="Kapalıyken: Ctrl+Enter gönderir, Enter yeni satır">
                <Toggle
                  on={prefs.enterToSend}
                  onChange={(v) => void update({ enterToSend: v })}
                  ariaLabel="Enter gönderir"
                />
              </Row>

              <Row label="Animasyonlar" hint="Mikro-geçişler ve hareket">
                <Toggle
                  on={prefs.animations}
                  onChange={(v) => void update({ animations: v })}
                  ariaLabel="Animasyonlar"
                />
              </Row>

              <ApiKeysSection />
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
