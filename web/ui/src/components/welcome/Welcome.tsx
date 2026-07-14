/* Welcome — projesiz başlangıç yüzeyi. Ürünün vaadini ve AI karar akışını
   ilk bakışta anlatır; dosya açma eylemi ana odak olarak kalır. */

import { FolderOpen, ArrowRight, Code2, GitPullRequest, Map, ShieldCheck, Command } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useSettings } from "@/state/settings";
import { Logo } from "@/components/brand/Logo";
import { Kbd } from "@/components/ui";
import { openCommandsPalette } from "@/lib/commands";

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

  return (
    <main
      className="welcome-shell flex min-h-0 flex-1 items-center justify-center overflow-y-auto bg-bg px-6 py-8"
    >
      <div className="welcome-layout grid w-full max-w-6xl items-center gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.68fr)]">
        <section className="min-w-0">
          <div className="mb-6 flex items-center gap-2 text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
            <Logo size={18} />
            <span>Yerel çalışma alanı</span>
          </div>
          <h1 className="max-w-[17ch] text-text" style={{ fontSize: "var(--t-hero)", fontWeight: "var(--w-display)", letterSpacing: "var(--ls-hero)", lineHeight: 1.08 }}>
            Değişikliği görün. Kararı siz verin.
          </h1>
          <p className="mt-4 max-w-[54ch] text-muted" style={{ fontSize: "var(--t-body)", lineHeight: 1.65 }}>
            Planı, etkilenen dosyaları ve diff’i aynı çalışma akışında inceleyin. Multi-Agent IDE yerel projenizin denetimini sizde tutar.
          </p>

          <div className="mt-7 flex flex-wrap items-center gap-x-4 gap-y-3">
            <button
              className="pressable group flex items-center gap-2 rounded-[var(--r-sm)] bg-accent px-4 py-2.5 text-on-accent hover:bg-accent2"
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
              onClick={() => void pickAndOpen()}
            >
              <FolderOpen size={15} />
              Klasör Aç
              <ArrowRight size={13} className="opacity-60 transition-transform group-hover:translate-x-0.5" />
            </button>
            <button
              className="pressable flex items-center gap-2 text-muted hover:text-text"
              style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
              onClick={openCommandsPalette}
            >
              <Command size={14} />
              Komut Merkezi
              <Kbd>Ctrl K</Kbd>
            </button>
          </div>

          {recent.length > 0 && (
            <div className="mt-10 max-w-2xl border-t border-border-w pt-3">
              <div className="mb-1 text-left text-faint" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
                Son projeler
              </div>
              <div className="flex flex-col divide-y divide-[var(--line)]">
                {recent.slice(0, 4).map((p) => (
                  <button
                    key={p.path}
                    onClick={() => void openProject(p.path)}
                    className="pressable group flex items-center gap-3 px-1 py-2.5 text-left hover:bg-card/45"
                  >
                    <span className="flex size-6 shrink-0 items-center justify-center border-l-2 border-accent text-accent" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
                      {p.name.slice(0, 1).toLocaleUpperCase("tr")}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{p.name}</span>
                      <span className="block truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>{p.path}</span>
                    </span>
                    <ArrowRight size={13} className="shrink-0 text-faint opacity-0 transition-[opacity,transform] duration-[var(--dur-fast)] ease-[var(--ease-out)] group-hover:translate-x-0.5 group-hover:opacity-100" />
                  </button>
                ))}
              </div>
            </div>
          )}
        </section>

        <section className="hidden border-l border-border-w pl-7 lg:block">
          <div className="flex items-center gap-2 text-text2">
            <GitPullRequest size={15} className="text-accent" />
            <p style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Çalışma anlaşması</p>
          </div>
          <p className="mt-2 max-w-[30ch] text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.55 }}>
            Öneri uygulanmadan önce kapsam ve diff görünür kalır.
          </p>
          <ol className="mt-6 divide-y divide-[var(--line)] border-y border-border-w">
            {FLOW.map(({ Icon, title, text }, i) => (
              <li key={title} className="grid grid-cols-[24px_18px_1fr] gap-3 py-4">
                <span className="text-accent" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>0{i + 1}</span>
                <Icon size={15} className="mt-0.5 shrink-0 text-muted" />
                <div>
                  <p className="text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{title}</p>
                  <p className="mt-0.5 text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.5 }}>{text}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>
      </div>
    </main>
  );
}
