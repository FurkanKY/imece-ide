import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Plus, RefreshCw } from "lucide-react";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";
import { useUi } from "@/state/ui";
import { useEditor } from "@/state/editor";
import { AiPanel } from "@/components/aipanel/AiPanel";
import { Button } from "@/components/ui";

function statusLabel(status: string, stage: string, readOnly = false, historicalStatus: string | null = null) {
  if (readOnly) return `Önceki oturum · ${historicalStatus || status}`;
  if (status === "running") return stage === "ready" ? "İnceleme bekliyor" : "Çalışıyor";
  if (stage === "applied") return "Uygulandı";
  if (stage === "restored") return "Geri alındı";
  if (stage === "cancelled") return "Durduruldu";
  if (stage === "error") return "Hata";
  if (stage === "ready") return "İnceleme bekliyor";
  if (status === "done" && stage === "draft") return "Vazgeçildi";
  return "Tamamlandı";
}

export function TaskWorkspace() {
  const root = useWorkspace((s) => s.root);
  const runs = useRun((s) => s.runs);
  const selectedRunId = useRun((s) => s.selectedRunId);
  const newDraft = useRun((s) => s.newDraft);
  const setTask = useRun((s) => s.setTask);
  const setProviderId = useRun((s) => s.setProviderId);
  const selectRun = useRun((s) => s.selectRun);
  const refreshRuns = useRun((s) => s.refreshRuns);
  const retryHistory = useRun((s) => s.retryHistory);
  const historyUnavailable = useRun((s) => s.historyUnavailable);
  const selectTask = (id: string) => {
    if (useRun.getState().selectedRunId !== id && useEditor.getState().diff) useEditor.getState().closeDiff();
    selectRun(id);
  };
  const createDraft = () => {
    if (useEditor.getState().diff) useEditor.getState().closeDiff();
    newDraft();
  };
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setRefreshError("");
    void refreshRuns().catch((error: unknown) => {
      if (!cancelled) setRefreshError(error instanceof Error ? error.message : "Görevler yenilenemedi.");
    });
    return () => { cancelled = true; };
  }, [root, refreshRuns]);

  const records = useMemo(() => Object.values(runs).filter((run) => run.root === root)
    .sort((a, b) => b.revision - a.revision), [runs, root]);
  const runningCount = records.filter((run) => !run.readOnly && (run.status === "running" || run.runStage === "ready" || run.pending || run.uncertain)).length;
  const cleanTerminal = records.filter((run) => !run.readOnly && run.status !== "running" && !run.pending && !run.uncertain && run.runStage !== "ready");
  const visible = records.filter((run) => run.readOnly || run.status === "running" || run.pending || run.uncertain || run.runStage === "ready" || cleanTerminal.slice(0, 32).includes(run));
  const refresh = async () => {
    setRefreshing(true);
    setRefreshError("");
    try { await refreshRuns(); } catch (error) { setRefreshError(error instanceof Error ? error.message : "Görevler yenilenemedi."); }
    finally { setRefreshing(false); }
  };

  return (
    <section className="task-workspace flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Görevler">
      <nav className="flex h-10 shrink-0 items-center gap-1 border-b border-border-w px-3" aria-label="Çalışma alanı görünümü">
        <span className="mr-3 text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Görevler</span>
        <button className="pressable rounded-[var(--r-sm)] px-2 py-1 text-muted hover:bg-card hover:text-text" onClick={() => useUi.getState().setWorkspaceView("tools")}>Araçlar</button>
      </nav>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col md:flex-row">
        <aside className="task-list flex min-h-0 min-w-0 flex-col border-b border-border-w md:w-[min(34%,360px)] md:shrink-0 md:border-b-0 md:border-r" aria-label="Proje görevleri">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border-w px-3 py-2">
            <div className="min-w-0">
              <h1 className="truncate text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Görevler</h1>
              <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>{runningCount}/2 etkin veya bekleyen</p>
            </div>
            <div className="flex shrink-0 gap-1">
              <Button variant="secondary" size="sm" icon={RefreshCw} disabled={refreshing} loading={refreshing} onClick={() => void refresh()} aria-label="Görevleri yenile">Yenile</Button>
              <Button variant="primary" size="sm" icon={Plus} onClick={createDraft} aria-label="Yeni görev">Yeni</Button>
            </div>
          </div>
          {refreshError && <p role="alert" className="px-3 py-2 text-err" style={{ fontSize: "var(--t-caption)" }}>Yenileme başarısız: {refreshError}</p>}
          {historyUnavailable && <p role="status" className="px-3 py-2 text-warn" style={{ fontSize: "var(--t-caption)" }}>Önceki oturumların kayıtları şu anda okunamıyor; etkin görevler etkilenmedi.</p>}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {visible.length === 0 ? (
              <div className="px-4 py-5">
                <h2 className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Bu projede henüz görev yok</h2>
                <p className="mt-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Bir işi tarif ederek başlayın; önerileri uygulamadan önce inceleyebilirsiniz.</p>
                <Button className="mt-3" variant="secondary" size="sm" icon={Plus} onClick={createDraft}>Görev yaz</Button>
              </div>
            ) : visible.map((run) => {
              const selected = selectedRunId === run.runId;
              return <button key={run.runId} type="button" aria-pressed={selected} onClick={() => selectTask(run.runId)} className={'w-full border-b border-border-w px-3 py-2.5 text-left hover:bg-card/45 ' + (selected ? 'bg-card/60' : '')}>
                <span className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-text2" style={{ fontSize: "var(--t-label)" }}>{run.task || "Görev"}</span>
                  <span className="shrink-0 text-muted" style={{ fontSize: "var(--t-caption)" }}>{statusLabel(run.status, run.runStage, run.readOnly, run.historicalStatus)}</span>
                </span>
                {(run.uncertain || run.pending) && <span className="mt-1 flex items-center gap-1 text-warn" style={{ fontSize: "var(--t-caption)" }}><AlertTriangle size={12}/>{run.uncertain ? "Durum doğrulanamadı" : "İstek bekliyor; yenileyerek doğrulayın"}</span>}
              </button>;
            })}
          </div>
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {selectedRunId && runs[selectedRunId]?.readOnly && <div role="status" className="shrink-0 border-b border-warn/30 bg-warn/5 px-4 py-2 text-warn" style={{ fontSize: "var(--t-caption)" }}>
            Önceki oturumun kaydı; bu oturumda uygulanamaz/devam ettirilemez.
            {runs[selectedRunId]?.authoritativeTask !== false && <Button className="ml-2" variant="secondary" size="sm" onClick={() => { const old = runs[selectedRunId]; if (!root || old.root !== root || useWorkspace.getState().root !== old.root || useRun.getState().selectedRunId !== old.runId) return; createDraft(); setTask(old.task); setProviderId(old.providerId); }}>Yeni görev taslağına taşı</Button>}
            {runs[selectedRunId]?.retryAvailable && <Button className="ml-2" variant="secondary" size="sm" onClick={(event) => { event.preventDefault(); event.stopPropagation(); void retryHistory(selectedRunId); }} disabled={runs[selectedRunId]?.pending || runs[selectedRunId]?.uncertain}>Yeniden çalıştır</Button>}
          </div>}
          <div className="min-h-0 flex-1"><AiPanel embedded /></div>
        </div>
      </div>
    </section>
  );
}
