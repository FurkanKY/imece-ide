/* SettingsDialog — arayüz tercihleri: accent (canlı), yoğunluk, Enter davranışı,
   animasyonlar. settings_panel.py'nin halefi; değişiklik anında uygulanır + kalıcı. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { X, Check, KeyRound, Plus, Trash2, Terminal } from "lucide-react";
import { useUi } from "@/state/ui";
import { useSettings } from "@/state/settings";
import { useKeys } from "@/state/keys";
import { Prefs, ProviderInfo } from "@/bridge";
import { Logo } from "@/components/brand/Logo";
import { Button, StatusDot } from "@/components/ui";
import { Select } from "@/components/ui/Select";
import { toast } from "@/components/toasts/toasts";

const ACCENTS: { id: Prefs["accent"]; color: string }[] = [
  { id: "blue", color: "#8aa6c6" },
  { id: "indigo", color: "#9898c4" },
  { id: "violet", color: "#b095c2" },
  { id: "green", color: "#6bbf93" },
  { id: "amber", color: "#c7a36b" },
  { id: "rose", color: "#cf8b96" },
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

/** Kataloğun varsayılan olarak listede duran üyeleri (klasik üçlü). */
const DEFAULT_VISIBLE = new Set(["claude", "deepseek", "gemini"]);

/** API sağlayıcısı kartı: durum + model seçimi + anahtar girişi (test ederek kaydet).
    Anahtar geri OKUNMAZ — yalnız maske. */
function ApiProviderCard({ p }: { p: ProviderInfo }) {
  const save = useKeys((s) => s.save);
  const test = useKeys((s) => s.test);
  const setModel = useKeys((s) => s.setModel);
  const removeCustom = useKeys((s) => s.removeCustom);
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);

  const saveKey = async () => {
    const key = val.trim();
    setBusy(true);
    try {
      const t = await test(p.id, key);
      if (!t.ok && t.code === "auth") {
        toast.err(`${p.label}: ${t.detail}`);
        return;
      }
      await save({ [p.id]: key });
      setVal("");
      if (t.ok) toast.ok(`${p.label} anahtarı doğrulandı ve kaydedildi.`);
      else toast.info(`${p.label} anahtarı kaydedildi; uç noktaya şu an erişilemedi.`);
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Anahtar kaydedilemedi.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="py-1.5">
      <div className="flex items-center gap-2">
        <span className="flex min-w-0 flex-1 items-center gap-1.5 text-text2" style={{ fontSize: "var(--t-label)" }}>
          <StatusDot tone={p.ok ? "ok" : "warn"} />
          <span className="truncate">{p.label}</span>
        </span>
        {p.models && p.models.length > 1 && p.model && (
          <div className="w-44 shrink-0">
            <Select
              value={p.model}
              options={p.models}
              onChange={(m) => {
                void setModel(p.id, m).then(
                  () => toast.ok(`${p.label} modeli: ${m}`),
                  () => toast.err("Model kaydedilemedi."),
                );
              }}
              ariaLabel={`${p.label} modeli`}
            />
          </div>
        )}
        {p.custom && (
          <button
            aria-label={`${p.label} sağlayıcısını kaldır`}
            title="Kaldır"
            className="icon-btn size-6 shrink-0 text-muted hover:text-err"
            onClick={() => {
              void removeCustom(p.id).then(
                () => toast.ok(`${p.label} kaldırıldı.`),
                () => toast.err("Sağlayıcı kaldırılamadı."),
              );
            }}
          >
            <Trash2 size={12} />
          </button>
        )}
      </div>
      {p.keyless ? (
        <div className="mt-1 pl-4 text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {p.ok ? "Yerel sunucu çalışıyor — anahtar gerekmez." : "Yerel sunucuya erişilemiyor (varsayılan: 127.0.0.1:11434)."}
        </div>
      ) : (
        <div className="mt-1 flex items-center gap-2 pl-4">
          <input
            type="password"
            value={val}
            onChange={(e) => setVal(e.target.value)}
            placeholder={p.ok ? `kayıtlı (${p.masked}). Değiştirmek için yaz` : p.keyHint || "API anahtarı"}
            aria-label={`${p.label} API anahtarı`}
            autoComplete="off"
            spellCheck={false}
            className="selectable min-w-0 flex-1 rounded-[var(--r-sm)] border border-border-w2 bg-field px-2.5 py-1.5 text-text outline-none placeholder:text-faint focus:border-accent"
            style={{ fontSize: "var(--t-caption)", fontFamily: "var(--font-mono)" }}
          />
          <Button size="sm" variant="secondary" loading={busy} disabled={!val.trim()} onClick={() => void saveKey()}>
            Test et ve kaydet
          </Button>
        </div>
      )}
    </div>
  );
}

/** Ajan CLI'sı satırı: PATH tespiti — anahtar yerine kurulum durumu. */
function CliProviderRow({ p }: { p: ProviderInfo }) {
  return (
    <div className="flex items-center gap-2 py-1.5">
      <span className="flex w-32 shrink-0 items-center gap-1.5 text-text2" style={{ fontSize: "var(--t-label)" }}>
        <StatusDot tone={p.ok ? "ok" : "warn"} />
        <Terminal size={11} className="shrink-0 text-muted" />
        <span className="truncate">{p.label}</span>
      </span>
      <span className="min-w-0 flex-1 truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
        {p.ok ? `hazır (${p.detail})` : `${p.detail} — ${p.docsUrl.replace("https://", "")}`}
      </span>
    </div>
  );
}

/** Özel OpenAI-uyumlu uç formu. */
function CustomProviderForm({ onDone }: { onDone: () => void }) {
  const addCustom = useKeys((s) => s.addCustom);
  const [form, setForm] = useState({ id: "", label: "", baseUrl: "", model: "" });
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const ready = form.id.trim() && form.label.trim() && form.baseUrl.trim().startsWith("http") && form.model.trim();
  const fields: { k: keyof typeof form; ph: string }[] = [
    { k: "id", ph: "kısa id (ör. sirket-llm)" },
    { k: "label", ph: "görünen ad" },
    { k: "baseUrl", ph: "base URL (ör. https://llm.example.com/v1)" },
    { k: "model", ph: "model adı" },
  ];
  return (
    <div className="mt-1 flex flex-col gap-1.5 rounded-[var(--r-md)] border border-border-w bg-field/40 p-2">
      {fields.map(({ k, ph }) => (
        <input
          key={k}
          value={form[k]}
          onChange={set(k)}
          placeholder={ph}
          aria-label={ph}
          autoComplete="off"
          spellCheck={false}
          className="selectable rounded-[var(--r-sm)] border border-border-w2 bg-field px-2.5 py-1.5 text-text outline-none placeholder:text-faint focus:border-accent"
          style={{ fontSize: "var(--t-caption)", fontFamily: "var(--font-mono)" }}
        />
      ))}
      <div className="flex justify-end gap-1.5">
        <Button size="sm" variant="ghost" onClick={onDone}>Vazgeç</Button>
        <Button
          size="sm"
          variant="secondary"
          loading={busy}
          disabled={!ready}
          onClick={async () => {
            setBusy(true);
            try {
              await addCustom({ id: form.id.trim(), label: form.label.trim(), baseUrl: form.baseUrl.trim(), model: form.model.trim() });
              toast.ok(`${form.label.trim()} eklendi. Şimdi anahtarını girin.`);
              onDone();
            } catch (e) {
              toast.err(e instanceof Error ? e.message : "Sağlayıcı eklenemedi.");
            } finally {
              setBusy(false);
            }
          }}
        >
          Ekle
        </Button>
      </div>
    </div>
  );
}

function ProvidersSection() {
  const providers = useKeys((s) => s.providers);
  const load = useKeys((s) => s.load);
  const loaded = useKeys((s) => s.loaded);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  // katalogdan bu oturumda eklenenler anahtar girilene kadar listede kalsın
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!loaded) void load();
  }, [loaded, load]);

  const all = Object.values(providers);
  const visible = all.filter((p) => p.ok || p.masked || p.custom || DEFAULT_VISIBLE.has(p.id) || pinned.has(p.id));
  const addable = all.filter((p) => !visible.includes(p));

  return (
    <div className="py-2.5">
      <div className="mb-1 flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-muted"
             style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
          <KeyRound size={11} /> Model sağlayıcıları
        </div>
        <button
          onClick={() => { setPickerOpen((v) => !v); setCustomOpen(false); }}
          className="pressable flex items-center gap-1 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1 text-text2 hover:border-border-w2"
          style={{ fontSize: "var(--t-caption)" }}
        >
          <Plus size={11} /> Sağlayıcı ekle
        </button>
      </div>

      {pickerOpen && (
        <div className="mb-1.5 flex flex-wrap gap-1.5 rounded-[var(--r-md)] border border-border-w bg-field/40 p-2">
          {addable.map((p) => (
            <button
              key={p.id}
              onClick={() => {
                setPinned((s) => new Set(s).add(p.id));
                setPickerOpen(false);
              }}
              className="pressable flex items-center gap-1.5 rounded-[var(--r-pill)] border border-border-w bg-field px-2.5 py-1 text-text2 hover:border-accent hover:text-text"
              style={{ fontSize: "var(--t-caption)" }}
            >
              {p.kind === "cli" && <Terminal size={10} className="text-muted" />}
              {p.label}
            </button>
          ))}
          <button
            onClick={() => { setCustomOpen(true); setPickerOpen(false); }}
            className="pressable flex items-center gap-1.5 rounded-[var(--r-pill)] border border-dashed border-border-w2 bg-transparent px-2.5 py-1 text-muted hover:border-accent hover:text-text"
            style={{ fontSize: "var(--t-caption)" }}
          >
            <Plus size={10} /> Özel (OpenAI-uyumlu)…
          </button>
        </div>
      )}
      {customOpen && <CustomProviderForm onDone={() => setCustomOpen(false)} />}

      {visible.filter((p) => p.kind === "openai").map((p) => (
        <ApiProviderCard key={p.id} p={p} />
      ))}
      {visible.filter((p) => p.kind === "cli").map((p) => (
        <CliProviderRow key={p.id} p={p} />
      ))}

      <p className="mt-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>
        Anahtarlar makinenizde kalır: kaynak modda yerel .env, paketli Windows sürümünde
        Windows hesabınıza bağlı şifreli depo. Hiçbir yere gönderilmez ve arayüze geri okunmaz.
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
  const closeRef = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => closeRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, setOpen]);

  if (!prefs) return null;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: animate ? 0.15 : 0 }}
          className="fixed inset-0 z-[var(--z-dialog)] flex items-center justify-center overflow-y-auto bg-black/50 p-4"
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
            aria-labelledby="settings-dialog-title"
          >
            <div className="flex items-center justify-between border-b border-border-w px-4 py-3">
              <div className="flex items-center gap-2">
                <Logo size={16} />
                <h2 id="settings-dialog-title" className="text-text" style={{ fontSize: "var(--t-title)", fontWeight: "var(--w-title)" }}>
                  Ayarlar
                </h2>
              </div>
              <button
                ref={closeRef}
                onClick={() => setOpen(false)}
                aria-label="Kapat"
                className="icon-btn size-7"
              >
                <X size={15} />
              </button>
            </div>

            <div className="min-h-0 overflow-y-auto divide-y divide-[var(--line)] px-4 py-1">
              <Row label="Vurgu rengi" hint="Anında uygulanır">
                <div className="flex gap-1.5">
                  {ACCENTS.map((a) => (
                    <button
                      key={a.id}
                      title={a.id}
                      aria-label={`Vurgu rengi: ${a.id}`}
                      onClick={() => void update({ accent: a.id })}
                      className="pressable flex size-6 items-center justify-center rounded-[var(--r-xs)] border"
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

              <ProvidersSection />
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
