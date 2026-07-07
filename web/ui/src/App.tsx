/* App — pencere kabuğu + çalışma alanı. Proje açıksa workspace, değilse welcome. */

import { lazy, Suspense, useEffect, useState } from "react";
import { bridge } from "@/bridge";
import { useSettings } from "@/state/settings";
import { useWorkspace } from "@/state/workspace";
import { installKeymap } from "@/lib/keymap";
import { Titlebar } from "@/components/titlebar/Titlebar";
import { ResizeEdges } from "@/components/window/ResizeEdges";
import { ActivityBar, View } from "@/components/activitybar/ActivityBar";
import { Explorer } from "@/components/explorer/Explorer";
import { Editor } from "@/components/editor/Editor";
import { StatusBar } from "@/components/statusbar/StatusBar";
import { Welcome } from "@/components/welcome/Welcome";
import { Palette } from "@/components/palette/Palette";
import { ToastHost } from "@/components/toasts/ToastHost";
import { DialogHost } from "@/components/dialogs/DialogHost";
import { AiPanel } from "@/components/aipanel/AiPanel";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { useRun } from "@/state/run";

const MonacoSmoke = lazy(() => import("@/dev/MonacoSmoke"));
const smokeMode = new URLSearchParams(location.search).get("smoke");
const scenario = new URLSearchParams(location.search).get("scenario");

function Workspace() {
  const [view, setView] = useState<View>("explorer");
  const hasTabs = useEditor((s) => s.tabs.length > 0);
  const hasDiff = useEditor((s) => s.diff !== null);
  const sidebarVisible = useUi((s) => s.sidebarVisible);
  const aiPanelVisible = useUi((s) => s.aiPanelVisible);

  return (
    <div className="flex min-h-0 flex-1">
      <ActivityBar active={view} onSelect={setView} onSettings={() => {}} />
      {sidebarVisible && (
      <aside className="w-[240px] shrink-0 border-r border-border-w bg-side">
        {view === "explorer" ? (
          <Explorer />
        ) : (
          <div className="p-4 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Bu görünüm sonraki fazda gelecek.
          </div>
        )}
      </aside>
      )}
      {hasTabs || hasDiff ? (
        <Editor />
      ) : (
        <div className="flex min-w-0 flex-1 items-center justify-center bg-panel">
          <p className="text-faint" style={{ fontSize: "var(--t-body)" }}>
            Soldan bir dosya seç — ya da sağdan ekibe bir görev ver.
          </p>
        </div>
      )}
      {aiPanelVisible && <AiPanel />}
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
      <ToastHost />
    </div>
  );
}
