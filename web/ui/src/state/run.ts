/* run store — multi-agent koşusunun canlı durumu.
   Motor olayları (stage/info/output/metric/diff/verdict/proposal) değişmeden
   run.event kanalından gelir; burada aşama kartları + pipeline + değişiklikler
   için tüketilir. desktop.py:on_event akışının store karşılığı. */

import { create } from "zustand";
import { bridge, Checkpoint, Proposal, Role, Routing, RunEvent } from "@/bridge";
import { toast } from "@/components/toasts/toasts";

export const STAGE_ROLE: Record<string, Role> = {
  plan: "planner",
  code: "coder",
  review: "reviewer",
};

export type StageState = "idle" | "running" | "done" | "error";

export interface StageInfo {
  role: Role;
  state: StageState;
  provider: string;
  output: string;
  model?: string;
  latency_s?: number;
  tokens?: number;
  cost_usd?: number;
}

export interface FlowItem {
  id: number;
  kind: "task" | "stage" | "info" | "error" | "summary";
  text: string;
  stage?: string; // kind=stage → plan|code|review
}

export interface DiffRow {
  path: string;
  isNew: boolean;
  diff: string;
  checked: boolean;
}

export interface PlanInfo {
  summary: string;
  files: string[];
  assumptions: string[];
  risks: string[];
}

export type RunStatus = "idle" | "running" | "done" | "failed" | "cancelled";
export type RunStage = "draft" | "planning" | "working" | "reviewing" | "ready" | "applied" | "restored" | "error";

interface RunState {
  status: RunStatus;
  /** Kullanıcının gördüğü lifecycle; altyapıdaki RunStatus'tan daha ayrıntılıdır. */
  runStage: RunStage;
  runId: string | null;
  task: string;
  plan: PlanInfo | null;
  routing: Routing;
  providers: string[];
  stages: Record<Role, StageInfo>;
  flow: FlowItem[];
  diffs: DiffRow[];
  proposals: Proposal[];
  verdict: string | null;
  verdictNote: string;
  totals: { latency_s: number; tokens: number; cost_usd: number } | null;
  error: string | null;
  checkpointId: string | null;
  lastRestoredCheckpointId: string | null;
  checkpointBusy: boolean;

  loadProviders: () => Promise<void>;
  setRouting: (role: Role, provider: string) => void;
  setTask: (task: string) => void;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
  toggleDiff: (path: string) => void;
  apply: () => Promise<void>;
  restoreCheckpoint: (checkpointId?: string) => Promise<void>;
  reject: () => Promise<void>;
  /** olay kanalı aboneliği — App mount'ta bir kez */
  install: () => void;
}

const IDLE_STAGES = (): Record<Role, StageInfo> => ({
  planner: { role: "planner", state: "idle", provider: "", output: "" },
  coder: { role: "coder", state: "idle", provider: "", output: "" },
  reviewer: { role: "reviewer", state: "idle", provider: "", output: "" },
});

let flowId = 1;
let installed = false;

export const useRun = create<RunState>((set, get) => ({
  status: "idle",
  runStage: "draft",
  runId: null,
  task: "",
  plan: null,
  routing: { planner: "claude", coder: "deepseek", reviewer: "gemini" },
  providers: ["claude", "deepseek", "gemini"],
  stages: IDLE_STAGES(),
  flow: [],
  diffs: [],
  proposals: [],
  verdict: null,
  verdictNote: "",
  totals: null,
  error: null,
  checkpointId: null,
  lastRestoredCheckpointId: null,
  checkpointBusy: false,

  loadProviders: async () => {
    try {
      const { providers, defaultRouting } = await bridge.call("run.providers", {});
      set({ providers, routing: defaultRouting });
    } catch {
      // varsayılanlar kalır
    }
  },

  setRouting: (role, provider) =>
    set((s) => ({ routing: { ...s.routing, [role]: provider } })),

  setTask: (task) => set({ task }),

  start: async () => {
    const { task, routing, status, runStage } = get();
    if (status === "running") return;
    if (runStage === "ready") {
      toast.info("Önce hazır değişiklikleri inceleyin veya vazgeçin.");
      return;
    }
    if (!task.trim()) {
      toast.info("Görev boş.");
      return;
    }
    set({
      status: "running",
      runStage: "planning",
      stages: IDLE_STAGES(),
      flow: [{ id: flowId++, kind: "task", text: task.trim() }],
      plan: null,
      diffs: [],
      proposals: [],
      verdict: null,
      verdictNote: "",
      totals: null,
      error: null,
      checkpointId: null,
    });
    try {
      const { runId } = await bridge.call("run.start", { task: task.trim(), routing });
      set({ runId });
    } catch (e) {
      set({ status: "failed", runStage: "error", error: e instanceof Error ? e.message : "Koşu başlatılamadı." });
      toast.err(e instanceof Error ? e.message : "Koşu başlatılamadı.");
    }
  },

  cancel: async () => {
    const { runId, status } = get();
    if (status !== "running") return;
    await bridge.call("run.cancel", { runId: runId ?? undefined });
  },

  toggleDiff: (path) =>
    set((s) => ({
      diffs: s.diffs.map((d) => (d.path === path ? { ...d, checked: !d.checked } : d)),
    })),

  apply: async () => {
    const paths = get().diffs.filter((d) => d.checked).map((d) => d.path);
    if (paths.length === 0) {
      toast.info("Uygulanacak dosya seçilmedi.");
      return;
    }
    if (get().checkpointBusy) return;
    const { useEditor } = await import("@/state/editor");
    const dirty = useEditor.getState().tabs.filter((t) => t.dirty && paths.includes(t.rel));
    if (dirty.length) {
      toast.err(`Önce kaydedilmemiş sekmeleri kaydedin: ${dirty.map((t) => t.name).join(", ")}`);
      return;
    }
    set({ checkpointBusy: true });
    try {
      const { applied, errors, checkpointId } = await bridge.call("run.applyProposals", { paths });
      for (const e of errors) toast.err(`${e.path}: ${e.message}`);
      if (!applied.length) return;
      if (!checkpointId) throw new Error("Uygulama tamamlandı ancak checkpoint kimliği alınamadı.");
      await refreshProjectFiles(applied);
      set({
        diffs: [],
        proposals: [],
        runStage: "applied",
        task: "",
        checkpointId,
        lastRestoredCheckpointId: null,
      });
      toast.ok(`${applied.length} dosya uygulandı · checkpoint hazır.`, {
        label: "Geri Al",
        run: () => void get().restoreCheckpoint(checkpointId),
      });
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Uygulanamadı.");
    } finally {
      set({ checkpointBusy: false });
    }
  },

  restoreCheckpoint: async (requestedId) => {
    const checkpointId = requestedId ?? get().checkpointId;
    if (!checkpointId) {
      toast.info("Geri alınacak checkpoint yok.");
      return;
    }
    if (get().checkpointBusy) return;
    let checkpoint: Checkpoint | undefined;
    try {
      const { checkpoints } = await bridge.call("checkpoint.list", {});
      checkpoint = checkpoints.find((item) => item.id === checkpointId);
    } catch {
      toast.err("Checkpoint bilgisi doğrulanamadı.");
      return;
    }
    if (!checkpoint) {
      toast.err("Checkpoint bulunamadı veya artık geçerli değil.");
      return;
    }
    const { useEditor } = await import("@/state/editor");
    const dirty = useEditor.getState().tabs.filter(
      (tab) => tab.dirty && checkpoint.files.includes(tab.rel),
    );
    if (dirty.length) {
      toast.err(`Geri almadan önce kaydedilmemiş sekmeleri kaydedin: ${dirty.map((t) => t.name).join(", ")}`);
      return;
    }
    const { confirmDialog } = await import("@/components/dialogs/dialogs");
    const preview = checkpoint.files.slice(0, 3).join(", ");
    const more = checkpoint.files.length > 3 ? ` ve ${checkpoint.files.length - 3} dosya daha` : "";
    const accepted = await confirmDialog({
      title: "Checkpoint'e geri dön",
      message: `${checkpoint.files.length} dosya checkpoint anına dönecek: ${preview}${more}.`,
      okLabel: "Geri Al",
      danger: true,
    });
    if (!accepted) return;
    set({ checkpointBusy: true });
    try {
      const { restored } = await bridge.call("checkpoint.restore", { checkpointId });
      await refreshProjectFiles(restored);
      set({
        runStage: "restored",
        checkpointId: null,
        lastRestoredCheckpointId: checkpointId,
        diffs: [],
        proposals: [],
      });
      toast.ok(`${restored.length} dosya checkpoint'e geri alındı.`);
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Checkpoint geri alınamadı.");
    } finally {
      set({ checkpointBusy: false });
    }
  },

  reject: async () => {
    await bridge.call("run.rejectProposals", {});
    set({ diffs: [], proposals: [], runStage: "draft", task: "" });
    (await import("@/state/editor")).useEditor.getState().closeDiff();
    toast.info("Değişiklikler reddedildi.");
  },

  install: () => {
    if (installed) return;
    installed = true;

    bridge.on("run.event", ({ ev }) => consume(ev, set, get));
    bridge.on("run.finished", ({ status, error }) => {
      if (status === "failed") {
        set((s) => ({
          status: "failed",
          runStage: "error",
          error: error ?? null,
          stages: failRunning(s.stages),
          flow: [...s.flow, { id: flowId++, kind: "error", text: `Hata: ${error ?? "bilinmiyor"}` }],
        }));
      } else if (status === "cancelled") {
        set((s) => ({
          status: "cancelled",
          runStage: "draft",
          stages: failRunning(s.stages),
          flow: [...s.flow, { id: flowId++, kind: "info", text: "Koşu durduruldu." }],
        }));
      } else {
        set((s) => ({ status: "done", runStage: s.runStage === "ready" ? "ready" : "draft" }));
      }
    });
  },
}));

function failRunning(stages: Record<Role, StageInfo>): Record<Role, StageInfo> {
  const out = { ...stages };
  for (const r of Object.keys(out) as Role[]) {
    if (out[r].state === "running") out[r] = { ...out[r], state: "error" };
  }
  return out;
}

function consume(
  ev: RunEvent,
  set: (fn: (s: ReturnType<typeof useRun.getState>) => Partial<ReturnType<typeof useRun.getState>>) => void,
  get: () => ReturnType<typeof useRun.getState>,
) {
  const type = ev.type as string;
  if (type === "stage") {
    const stage = ev.stage as string;
    const provider = (ev.provider as string) ?? "";
    const role = STAGE_ROLE[stage];
    set((s) => ({
      runStage: stage === "plan" ? "planning" : stage === "code" ? "working" : stage === "review" ? "reviewing" : s.runStage,
      stages: role
        ? {
            ...markPrevDone(s.stages),
            [role]: { ...s.stages[role], state: "running", provider },
          }
        : s.stages,
      flow: [...s.flow, { id: flowId++, kind: "stage", text: provider, stage }],
    }));
  } else if (type === "info") {
    set((s) => ({ flow: [...s.flow, { id: flowId++, kind: "info", text: ev.text as string }] }));
  } else if (type === "output") {
    const stage = (ev.stage as string) ?? "";
    const role = STAGE_ROLE[stage];
    if (role) {
      set((s) => ({
        stages: {
          ...s.stages,
          [role]: { ...s.stages[role], output: s.stages[role].output + (ev.text as string) },
        },
      }));
    }
  } else if (type === "metric") {
    const role = STAGE_ROLE[(ev.stage as string) ?? ""];
    if (role) {
      set((s) => ({
        stages: {
          ...s.stages,
          [role]: {
            ...s.stages[role],
            state: "done",
            model: ev.model as string,
            latency_s: ev.latency_s as number,
            tokens: ev.tokens as number,
            cost_usd: ev.cost_usd as number,
          },
        },
      }));
    }
  } else if (type === "plan") {
    set(() => ({
      plan: {
        summary: (ev.summary as string) ?? "",
        files: (ev.files as string[] | undefined) ?? [],
        assumptions: (ev.assumptions as string[] | undefined) ?? [],
        risks: (ev.risks as string[] | undefined) ?? [],
      },
    }));
  } else if (type === "diff") {
    set((s) => ({
      diffs: [
        ...s.diffs,
        {
          path: ev.path as string,
          isNew: !!ev.is_new,
          diff: ev.diff as string,
          checked: true,
        },
      ],
    }));
  } else if (type === "verdict") {
    set(() => ({ verdict: ev.verdict as string, verdictNote: (ev.note as string) ?? "" }));
  } else if (type === "proposal") {
    const proposals = (ev.proposals as Proposal[]) ?? [];
    const totals = ev.totals as { latency_s: number; tokens: number; cost_usd: number };
    set((s) => ({
      proposals,
      runStage: "ready",
      totals,
      verdict: (ev.verdict as string) ?? s.verdict,
      flow: [
        ...s.flow,
        proposals.length
          ? { id: flowId++, kind: "summary", text: `${proposals.length} dosya önerisi hazır.` }
          : { id: flowId++, kind: "info", text: "Değişiklik önerisi çıkmadı." },
      ],
    }));
    // ilk diff'i merkezde aç (Cursor deseni) — döngüsel importu geciktir
    if (proposals.length) {
      void import("@/state/editor").then(({ useEditor }) =>
        useEditor.getState().openDiff(proposals[0].path),
      );
    }
  }
  void get; // (şimdilik kullanılmıyor)
}

function markPrevDone(stages: Record<Role, StageInfo>): Record<Role, StageInfo> {
  const out = { ...stages };
  for (const r of Object.keys(out) as Role[]) {
    if (out[r].state === "running") out[r] = { ...out[r], state: "done" };
  }
  return out;
}

async function refreshProjectFiles(paths: string[]) {
  const [{ useEditor }, { useWorkspace }, { useScm }] = await Promise.all([
    import("@/state/editor"),
    import("@/state/workspace"),
    import("@/state/scm"),
  ]);
  const editor = useEditor.getState();
  for (const rel of paths) {
    if (!editor.tabs.some((tab) => tab.rel === rel)) continue;
    try {
      const { content } = await bridge.call("fs.readFile", { rel });
      useEditor.setState((state) => ({
        tabs: state.tabs.map((tab) =>
          tab.rel === rel ? { ...tab, content, draft: content, dirty: false } : tab,
        ),
      }));
    } catch {
      useEditor.getState().closeDeleted(rel);
    }
  }
  const workspace = useWorkspace.getState();
  await Promise.all(Object.keys(workspace.children).map((rel) => workspace.loadDir(rel)));
  await useScm.getState().refresh();
  useEditor.getState().closeDiff();
}
