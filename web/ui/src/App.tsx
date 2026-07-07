/* App — pencere kabuğu + çalışma alanı. Proje açıksa workspace, değilse welcome. */

import { lazy, Suspense, useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { bridge } from "@/bridge";
import { useSettings } from "@/state/settings";
import { useWorkspace } from "@/state/workspace";
import { installKeymap } from "@/lib/keymap";
import { Titlebar } from "@/components/titlebar/Titlebar";
import { ResizeEdges } from "@/components/window/ResizeEdges";
import { ActivityBar } from "@/components/activitybar/ActivityBar";
import { Explorer } from "@/components/explorer/Explorer";
import { SearchView } from "@/components/search/SearchView";
import { Editor } from "@/components/editor/Editor";
import { StatusBar } from "@/components/statusbar/StatusBar";
import { Welcome } from "@/components/welcome/Welcome";
import { Palette } from "@/components/palette/Palette";
import { ToastHost } from "@/components/toasts/ToastHost";
import { DialogHost } from "@/components/dialogs/DialogHost";
import { SettingsDialog } from "@/components/settings/SettingsDialog";
import { AiPanel } from "@/components/aipanel/AiPanel";
import { BottomPanel } from "@/components/bottompanel/BottomPanel";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { useRun } from "@/state/run";

const MonacoSmoke = lazy(() => import("@/dev/MonacoSmoke"));
const smokeMode = new URLSearchParams(location.search).get("smoke");
const scenario = new URLSearchParams(location.search).get("scenario");

function Workspace() {
  const view = useUi((s) => s.sideView);
  const setView = useUi((s) => s.setSideView);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const hasTabs = useEditor((s) => s.tabs.length > 0);
  const hasDiff = useEditor((s) => s.diff !== null);
  const sidebarVisible = useUi((s) => s.sidebarVisible);
  const aiPanelVisible = useUi((s) => s.aiPanelVisible);
  const bottomVisible = useUi((s) => s.bottomVisible);

  const ease = [0.33, 1, 0.68, 1] as const;

  return (
    <div className="flex min-h-0 flex-1">
      <ActivityBar active={view} onSelect={setView} onSettings={() => setSettingsOpen(true)} />
      <AnimatePresence initial={false}>
        {sidebarVisible && (
          <motion.aside
            key="sidebar"
            initial={{ width: 0 }}
            animate={{ width: 240 }}
            exit={{ width: 0 }}
            transition={{ duration: 0.18, ease }}
            className="shrink-0 overflow-hidden border-r border-border-w bg-side"
          >
            <div className="h-full w-[240px]">
              {view === "explorer" ? (
                <Explorer />
              ) : view === "search" ? (
                <SearchView />
              ) : (
                <div className="p-4 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                  Bu görünüm sonraki fazda gelecek.
                </div>
              )}
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
      {/* orta kolon: editör ↕ terminal */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {hasTabs || hasDiff ? (
          <Editor />
        ) : (
          <div className="flex min-h-0 flex-1 items-center justify-center bg-panel">
            <p className="text-faint" style={{ fontSize: "var(--t-body)" }}>
              Soldan bir dosya seç — ya da sağdan ekibe bir görev ver.
            </p>
          </div>
        )}
        <AnimatePresence initial={false}>
          {bottomVisible && (
            <motion.div
              key="bottom"
              initial={{ height: 0 }}
              animate={{ height: 240 }}
              exit={{ height: 0 }}
              transition={{ duration: 0.18, ease }}
              className="shrink-0 overflow-hidden"
            >
              <div className="h-[240px]">
                <BottomPanel />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      <AnimatePresence initial={false}>
        {aiPanelVisible && (
          <motion.div
            key="aipanel"
            initial={{ width: 0 }}
            animate={{ width: 340 }}
            exit={{ width: 0 }}
            transition={{ duration: 0.18, ease }}
            className="shrink-0 overflow-hidden"
          >
            <AiPanel />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function App() {
  const [maximized, setMaximized] = useState(false);
  const loadSettings = useSettings((s) => s.load);
  const root = useWorkspace((s) => s.root);
  const openProject = useWorkspace((s) => s.openProject);

  useEffect(() => {
    installKeymap();
    const off = bridge.on("window.state", (s) => setMaximized(s.maximized));
    void (async () => {
      const { installSessionPersistence } = await import("@/lib/session");
      installSessionPersistence();
      useRun.getState().install();
      void useRun.getState().loadProviders();
      await loadSettings();
      // mock senaryoları: otomatik proje aç (+ koşu) — webshot/geliştirme
      const runScenarios = ["running", "result", "error"];
      if (!bridge.isNative && (scenario === "project" || scenario === "editor" || runScenarios.includes(scenario ?? ""))) {
        await openProject("C:/Projeler/demo-api");
        if (scenario === "editor") await useEditor.getState().open("src/App.tsx");
        if (runScenarios.includes(scenario ?? "")) {
          const { TASK } = await import("@/bridge/mock/fixtures/run");
          useRun.getState().setTask(TASK);
          void useRun.getState().start();
        }
      }
      document.documentElement.dataset.ready = "1";
      void bridge.call("window.ready", {}); // kapatma koruması aktive
    })();
    return off;
  }, [loadSettings, openProject]);

  if (smokeMode === "monaco") {
    return (
      <div className={"window" + (maximized ? " maximized" : "")}>
        <ResizeEdges maximized={maximized} />
        <Titlebar maximized={maximized} />
        <Suspense fallback={null}>
          <div className="min-h-0 flex-1"><MonacoSmoke /></div>
        </Suspense>
      </div>
    );
  }

  return (
    <div className={"window" + (maximized ? " maximized" : "")}>
      <ResizeEdges maximized={maximized} />
      <Titlebar maximized={maximized} />
      {root ? <Workspace /> : <Welcome />}
      <StatusBar />
      <Palette />
      <DialogHost />
      <SettingsDialog />
      <ToastHost />
    </div>
  );
}
