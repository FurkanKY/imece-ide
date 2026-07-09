/* Welcome — proje açık değilken karşılama: marka + accent glow + hızlı eylemler +
   kbd ipuçları + kart görünümlü son projeler. Uygulamanın "vitrini". */

import { motion } from "motion/react";
import { FolderOpen, ArrowRight } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useSettings } from "@/state/settings";
import { Logo } from "@/components/brand/Logo";
import { S } from "@/lib/strings.tr";

const NO_RECENT: { path: string; name: string; lastOpened: string }[] = [];

export function Welcome() {
  const pickAndOpen = useWorkspace((s) => s.pickAndOpen);
  const openProject = useWorkspace((s) => s.openProject);
  const recent = useSettings((s) => s.prefs?.recentProjects ?? NO_RECENT);

  return (
    <main className="relative flex flex-1 items-center justify-center overflow-hidden bg-bg">
      {/* accent glow orb — sahnenin ışığı */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.26, ease: [0.23, 1, 0.32, 1] }}
        className="relative flex w-full max-w-md flex-col items-center gap-3 px-6 text-center"
      >
        <div
          className="flex size-[68px] items-center justify-center rounded-[var(--r-xl)] border border-border-w"
          style={{
            background: "linear-gradient(to bottom, var(--card2), var(--card))",
            boxShadow: "var(--bevel-strong), var(--shadow-2), var(--glow-accent-soft)",
          }}
        >
          <Logo size={34} />
        </div>

        <div>
          <h1
            className="text-text"
            style={{
              fontSize: "22px",
              fontWeight: "var(--w-display)",
              letterSpacing: "0",
            }}
          >
            {S.welcome.title}
          </h1>
          <p className="mt-1 text-muted" style={{ fontSize: "var(--t-body)" }}>
            {S.welcome.subtitle}
          </p>
        </div>

        <button
          className="pressable group mt-2 flex items-center gap-2 rounded-[var(--r-sm)] bg-accent px-4 py-2 text-on-accent hover:bg-accent2"
          style={{
            fontSize: "var(--t-label)",
            fontWeight: "var(--w-label)",
            boxShadow: "var(--shadow-1), var(--glow-accent-soft)",
          }}
          onClick={() => void pickAndOpen()}
        >
          <FolderOpen size={15} />
          {S.welcome.openFolder}
          <ArrowRight size={13} className="opacity-60 transition-transform group-hover:translate-x-0.5" />
        </button>

        {recent.length > 0 && (
          <div className="mt-5 w-full">
            <div
              className="mb-1.5 px-1 text-left text-faint"
              style={{
                fontSize: "var(--t-overline)",
                fontWeight: "var(--w-overline)",
                letterSpacing: "var(--ls-overline)",
              }}
            >
              SON PROJELER
            </div>
            <div className="flex flex-col gap-1">
              {recent.slice(0, 4).map((p, i) => (
                <motion.button
                  key={p.path}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.08 + i * 0.05, duration: 0.25 }}
                  onClick={() => void openProject(p.path)}
                  className="material-card pressable group flex items-center gap-2.5 rounded-[var(--r-md)] border border-border-w px-3 py-2 text-left hover:border-border-w2 hover:bg-card2"
                  style={{ boxShadow: "var(--bevel)" }}
                >
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--r-xs)] bg-accentdim text-accent"
                        style={{ fontSize: "12px", fontWeight: 700 }}>
                    {p.name.slice(0, 1).toLocaleUpperCase("tr")}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>
                      {p.name}
                    </span>
                    <span className="block truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
                      {p.path}
                    </span>
                  </span>
                  <ArrowRight size={13} className="shrink-0 text-faint opacity-0 transition-[opacity,transform] duration-[var(--dur-fast)] ease-[var(--ease-out)] group-hover:translate-x-0.5 group-hover:opacity-100" />
                </motion.button>
              ))}
            </div>
          </div>
        )}

      </motion.div>
    </main>
  );
}
