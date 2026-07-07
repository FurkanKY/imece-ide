/* run store — multi-agent koşusunun canlı durumu.
   Motor olayları (stage/info/output/metric/diff/verdict/proposal) değişmeden
   run.event kanalından gelir; burada aşama kartları + pipeline + değişiklikler
   için tüketilir. desktop.py:on_event akışının store karşılığı. */

import { create } from "zustand";
import { bridge, Proposal, Role, Routing, RunEvent } from "@/bridge";
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

export type RunStatus = "idle" | "running" | "done" | "failed" | "cancelled";

interface RunState {
  status: RunStatus;
  runId: string | null;
  task: string;
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

  loadProviders: () => Promise<void>;
  setRouting: (role: Role, provider: string) => void;
  setTask: (task: string) => void;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
  toggleDiff: (path: string) => void;
  apply: () => Promise<void>;
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
  runId: null,
  task: "",
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
    const { task, routing, status } = get();
    if (status === "running") return;
    if (!task.trim()) {
      toast.info("Görev boş.");
      return;
    }
    set({
      status: "running",
      stages: IDLE_STAGES(),
      flow: [{ id: flowId++, kind: "task", text: task.trim() }],
      diffs: [],
      proposals: [],
      verdict: null,
      verdictNote: "",
      totals: null,
      error: null,
    });
    try {
      const { runId } = await bridge.call("run.start", { task: task.trim(), routing });
      set({ runId });
    } catch (e) {
      set({ status: "failed", error: e instanceof Error ? e.message : "Koşu başlatılamadı." });
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
    try {
      const { applied, errors } = await bridge.call("run.applyProposals", { paths });
      if (applied.length) {
        toast.ok(`${applied.length} dosya uygulandı (.bak yedekli).`);
        // açık sekmeleri ve ağacı tazele
        const { useEditor } = await import("@/state/editor");
        const { useWorkspace } = await import("@/state/workspace");
        const ed = useEditor.getState();
        for (const p of applied) {
          const tab = ed.tabs.find((t) => t.rel === p);
          if (tab) {
            const { content } = await bridge.call("fs.readFile", { rel: p });
            useEditor.setState((s) => ({
              tabs: s.tabs.map((t) =>
                t.rel === p ? { ...t, content, draft: content, dirty: false } : t,
              ),
            }));
          }
        }
        const ws = useWorkspace.getState();
        for (const rel of Object.keys(ws.children)) void ws.loadDir(rel);
      }
      for (const e of errors) toast.err(`${e.path}: ${e.message}`);
      set({ diffs: [], proposals: [] });
      (await import("@/state/editor")).useEditor.getState().closeDiff();
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Uygulanamadı.");
    }
  },

  reject: async () => {
    await bridge.call("run.rejectProposals", {});
    set({ diffs: [], proposals: [] });
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
          error: error ?? null,
          stages: failRunning(s.stages),
          flow: [...s.flow, { id: flowId++, kind: "error", text: `Hata: ${error ?? "bilinmiyor"}` }],
        }));
      } else if (status === "cancelled") {
        set((s) => ({
          status: "cancelled",
          stages: failRunning(s.stages),
          flow: [...s.flow, { id: flowId++, kind: "info", text: "Koşu durduruldu." }],
        }));
      } else {
        set({ status: "done" });
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
