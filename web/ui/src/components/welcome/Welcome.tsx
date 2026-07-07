/* Welcome — proje açık değilken karşılama + son projeler. */

import { FolderOpen, Clock } from "lucide-react";
import { useWorkspace } from "@/state/workspace";
import { useSettings } from "@/state/settings";
import { S } from "@/lib/strings.tr";

const NO_RECENT: { path: string; name: string; lastOpened: string }[] = [];

export function Welcome() {
  const pickAndOpen = useWorkspace((s) => s.pickAndOpen);
  const openProject = useWorkspace((s) => s.openProject);
  // selector STABİL referans dönmeli (yeni [] → sonsuz döngü)
  const recent = useSettings((s) => s.prefs?.recentProjects ?? NO_RECENT);

  return (
    <main className="flex flex-1 items-center justify-center bg-bg">
      <div className="flex w-full max-w-md flex-col items-center gap-3 px-6 text-center">
        <div
          className="flex size-16 items-center justify-center rounded-[var(--r-xl)] border border-border-w bg-card"
          style={{ boxShadow: "var(--shadow-2)" }}
        >
          <span className="text-accent" style={{ fontSize: 28 }}>⌘</span>
        </div>
        <h1
          className="text-text"
          style={{
            fontSize: "var(--t-display)",
            fontWeight: "var(--w-display)",
            letterSpacing: "var(--ls-display)",
          }}
        >
          {S.welcome.title}
        </h1>
        <p className="text-muted" style={{ fontSize: "var(--t-body)" }}>
          {S.welcome.subtitle}
        </p>
        <button
          className="mt-2 flex items-center gap-2 rounded-[var(--r-sm)] border border-border-w2 bg-card px-4 py-2 text-text2 transition-colors duration-[var(--dur-fast)] hover:bg-card2 hover:text-text"
          style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
          onClick={() => void pickAndOpen()}
        >
          <FolderOpen size={15} />
          {S.welcome.openFolder}
        </button>

        {recent.length > 0 && (
          <div className="mt-4 w-full">
            <div
              className="mb-1 px-1 text-left text-faint"
              style={{
                fontSize: "var(--t-overline)",
                fontWeight: "var(--w-overline)",
                letterSpacing: "var(--ls-overline)",
              }}
            >
              SON PROJELER
            </div>
            <div className="flex flex-col gap-0.5">
              {recent.slice(0, 5).map((p) => (
                <button
                  key={p.path}
                  onClick={() => void openProject(p.path)}
                  className="flex items-center gap-2 rounded-[var(--r-xs)] px-2 py-1.5 text-left text-text2 transition-colors hover:bg-card"
                >
                  <Clock size={13} className="shrink-0 text-faint" />
                  <span className="shrink-0" style={{ fontSize: "var(--t-label)" }}>
                    {p.name}
                  </span>
                  <span className="truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
                    {p.path}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}

        <p className="mt-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {S.welcome.hintShortcuts}
        </p>
      </div>
    </main>
  );
}
