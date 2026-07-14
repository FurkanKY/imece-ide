/* Welcome — projesiz başlangıç yüzeyi. Ürünün vaadini ve AI karar akışını
   ilk bakışta anlatır; dosya açma eylemi ana odak olarak kalır. */

import { motion, useReducedMotion } from "motion/react";
import { FolderOpen, ArrowRight, Bot, Code2, GitPullRequest, Map, ShieldCheck } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useSettings } from "@/state/settings";
import { Logo } from "@/components/brand/Logo";
import { Kbd } from "@/components/ui";

const NO_RECENT: { path: string; name: string; lastOpened: string }[] = [];

const FLOW = [
  { Icon: Map, title: "Planla", text: "Görevi, bağlamı ve etkilenecek dosyaları netleştir." },
  { Icon: Code2, title: "Üret", text: "Ekip öneriyi kontrollü bir değişiklik setine dönüştürür." },
  { Icon: ShieldCheck, title: "İncele", text: "Diff'i görün, uygula veya geri dön; kontrol sizde kalır." },
];

export function Welcome() {
  const pickAndOpen = useWorkspace((s) => s.pickAndOpen);
  const openProject = useWorkspace((s) => s.openProject);
  const recent = useSettings((s) => s.prefs?.recentProjects ?? NO_RECENT);
  const animationsEnabled = useSettings((s) => s.prefs?.animations ?? true);
  const reduceMotion = useReducedMotion();
  const animate = animationsEnabled && !reduceMotion;

  return (
    <main
      className="welcome-shell relative flex min-h-0 flex-1 items-center justify-center overflow-y-auto bg-bg px-6 py-8"
      style={{ backgroundImage: "radial-gradient(ellipse 55% 65% at 72% 44%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%)" }}
    >
      <div className="pointer-events-none absolute inset-0 opacity-50" style={{ backgroundImage: "linear-gradient(120deg, color-mix(in srgb, var(--accent) 7%, transparent), transparent 35%)" }} />
      <motion.div
        initial={{ opacity: 0, transform: "translateY(10px)" }}
        animate={{ opacity: 1, transform: "translateY(0)" }}
        transition={{ duration: animate ? 0.26 : 0, ease: [0.23, 1, 0.32, 1] }}
        className="welcome-layout relative grid w-full max-w-5xl items-center gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(340px,0.82fr)]"
      >
        <section className="min-w-0">
          <div className="mb-5 flex items-center gap-2 text-accent" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
            <span className="flex size-6 items-center justify-center rounded-[var(--r-xs)] border border-accent/25 bg-accentdim"><Bot size={13} /></span>
            AI DESTEKLİ ÇALIŞMA ALANI
          </div>
          <div className="flex items-start gap-4">
            <div
              className="flex size-[64px] shrink-0 items-center justify-center rounded-[var(--r-xl)] border border-border-w"
              style={{ background: "linear-gradient(to bottom, var(--card2), var(--card))", boxShadow: "var(--bevel-strong), var(--shadow-2), var(--glow-accent-soft)" }}
            >
              <Logo size={32} />
            </div>
            <div className="min-w-0">
              <h1 className="text-text" style={{ fontSize: "var(--t-display)", fontWeight: "var(--w-display)" }}>
                Kod değişikliklerini ekipçe yönetin.
              </h1>
              <p className="mt-2 max-w-[52ch] text-muted" style={{ fontSize: "var(--t-body)", lineHeight: 1.6 }}>
                Planı görün, öneriyi diff olarak inceleyin ve son kararı siz verin. Multi-Agent IDE, yerel projenizi kontrol altında tutar.
              </p>
            </div>
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <button
              className="pressable group flex items-center gap-2 rounded-[var(--r-sm)] bg-accent px-4 py-2.5 text-on-accent hover:bg-accent2"
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)", boxShadow: "var(--shadow-1), var(--glow-accent-soft)" }}
              onClick={() => void pickAndOpen()}
            >
              <FolderOpen size={15} />
              Klasör Aç
              <ArrowRight size={13} className="opacity-60 transition-transform group-hover:translate-x-0.5" />
            </button>
            <div className="flex items-center gap-1.5 text-faint" style={{ fontSize: "var(--t-caption)" }}>
              <Kbd>Ctrl K</Kbd><span>komutlar</span><span>·</span><Kbd>Ctrl P</Kbd><span>dosyalar</span>
            </div>
          </div>

          {recent.length > 0 && (
            <div className="mt-8 max-w-xl">
              <div className="mb-1.5 px-1 text-left text-faint" style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}>
                SON PROJELER
              </div>
              <div className="flex flex-col gap-1">
                {recent.slice(0, 4).map((p, i) => (
                  <motion.button
                    key={p.path}
                    initial={{ opacity: 0, transform: "translateY(6px)" }}
                    animate={{ opacity: 1, transform: "translateY(0)" }}
                    transition={{ delay: animate ? 0.08 + i * 0.05 : 0, duration: animate ? 0.25 : 0 }}
                    onClick={() => void openProject(p.path)}
                    className="material-card pressable group flex items-center gap-2.5 rounded-[var(--r-md)] border border-border-w px-3 py-2 text-left hover:border-border-w2 hover:bg-card2"
                    style={{ boxShadow: "var(--bevel)" }}
                  >
                    <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--r-xs)] bg-accentdim text-accent" style={{ fontSize: "12px", fontWeight: 700 }}>
                      {p.name.slice(0, 1).toLocaleUpperCase("tr")}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{p.name}</span>
                      <span className="block truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>{p.path}</span>
                    </span>
                    <ArrowRight size={13} className="shrink-0 text-faint opacity-0 transition-[opacity,transform] duration-[var(--dur-fast)] ease-[var(--ease-out)] group-hover:translate-x-0.5 group-hover:opacity-100" />
                  </motion.button>
                ))}
              </div>
            </div>
          )}
        </section>

        <section className="material-panel hidden overflow-hidden rounded-[var(--r-lg)] border border-border-w lg:block" style={{ boxShadow: "var(--bevel-strong), var(--shadow-3)" }}>
          <div className="flex items-center justify-between border-b border-border-w px-5 py-4">
            <div className="flex items-center gap-2">
              <span className="flex size-7 items-center justify-center rounded-[var(--r-xs)] bg-accentdim text-accent"><GitPullRequest size={14} /></span>
              <div>
                <p className="text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Karar akışı</p>
                <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>Her öneri siz onaylayana kadar diff'te kalır.</p>
              </div>
            </div>
            <span className="flex items-center gap-1.5 text-ok" style={{ fontSize: "var(--t-caption)" }}><span className="size-1.5 rounded-full bg-ok" /> Hazır</span>
          </div>
          <div className="divide-y divide-[var(--line)] px-5 py-2">
            {FLOW.map(({ Icon, title, text }, i) => (
              <div key={title} className="flex gap-3 py-4">
                <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border-w bg-card text-accent" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>{i + 1}</span>
                <Icon size={15} className="mt-1 shrink-0 text-muted" />
                <div>
                  <p className="text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{title}</p>
                  <p className="mt-0.5 text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.5 }}>{text}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      </motion.div>
    </main>
  );
}
