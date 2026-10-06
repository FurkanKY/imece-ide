/* run store — multi-agent koşusunun canlı durumu.
   Motor olayları (stage/info/output/metric/diff/verdict/proposal) değişmeden
   run.event kanalından gelir; burada aşama kartları + pipeline + değişiklikler
   için tüketilir. desktop.py:on_event akışının store karşılığı. */

import { create } from "zustand";
import { bridge, BridgeError, Checkpoint, Proposal, ProviderInfo, Role, Routing, RunEvent, AgentEvidence, ActivityItem } from "@/bridge";
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
  /** A4-A2: null -- rol için hiç kullanım (usage) verisi YOK (ör. ACP/hesap
      rotası) -- arayüz bunu "—" ile gösterir, "0" ile KARIŞTIRMAZ.
      undefined -- rol hiç çalışmadı/metrik gelmedi. */
  tokens?: number | null;
  cost_usd?: number | null;
  /** A4-A2: rol "running" olduğu andaki Date.now() -- canlı geçen süre
      sayacı (Pipeline.tsx) bunu tikleyerek gösterir. */
  startedAt?: number;
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
export type RunStage =
  | "draft"
  | "planning"
  | "working"
  | "reviewing"
  | "ready"
  | "applied"
  | "restored"
  | "error"
  /** Koşu tamamlandı ama Worker hiçbir değişiklik üretmedi (pipeline
      "no_changes" veya legacy motorda boş öneri) -- "draft" (hiç başlamamış)
      ile karıştırılmaması için ayrı bir terminal durum. */
  | "noChanges"
  /** A4-A4: kullanıcı koşuyu durdurdu (run.cancel / F7 gerçek iptal) --
      "draft" (hiç başlamamış) ve "error" (başarısızlık) ile
      KARIŞTIRILMAMASI için ayrı bir terminal durum. */
  | "cancelled";
/** F2 (takip isteği): bir koşunun GERÇEKTE hangi motorla yürütüldüğü —
    run.finished'ın `engine` alanından gelir; henüz bilinmiyorsa null.
    Composer, takip isteği modunu yalnızca "pipeline" iken açar. */
export type RunEngine = "agent" | "pipeline" | "legacy" | null;

/** Bounded public run record for run-history UI consumers. */
export interface RunSnapshot {
  runId: string;
  root: string | null;
  revision: number;
  status: RunStatus;
  runStage: RunStage;
  task: string;
  providerId: string;
  engine: RunEngine;
  followUpDraft: string;
  plan: PlanInfo | null;
  stages: Record<Role, StageInfo>;
  flow: FlowItem[];
  diffs: DiffRow[];
  proposals: Proposal[];
  agentEvidence: AgentEvidence | null;
  checkpointId: string | null;
  checkpointBusy: boolean;
  totals: RunState["totals"];
  verdict: string | null;
  verdictNote: string;
  error: string | null;
  errorTitle: string | null;
  errorDescription: string | null;
  mentions: string[];
  lastRestoredCheckpointId: string | null;
  uncertain: boolean;
  pending: boolean;
}

/** F6 (@-mentions) — Composer'da @ ile pinlenen dosya/klasör; sadece bu
    oturum boyunca task taslağıyla birlikte tutulur (kalıcı depoya YAZILMAZ). */
export const MAX_MENTIONS = 10;

interface RunState {
  runs: Record<string, RunSnapshot>;
  selectedRunId: string | null;
  draftToken: number;
  draftUncertain: boolean;
  status: RunStatus;
  /** Kullanıcının gördüğü lifecycle; altyapıdaki RunStatus'tan daha ayrıntılıdır. */
  runStage: RunStage;
  runId: string | null;
  task: string;
  /** F6 (@-mentions): proje-göreli, forward-slash yollar (dosya veya klasör). */
  mentions: string[];
  /** F2 (takip isteği): bu koşunun motoru (bkz. RunEngine). */
  engine: RunEngine;
  /** F2: "ready" (runStage) sırasında Composer'ın takip-isteği kutusunun taslak metni. */
  followUpDraft: string;
  plan: PlanInfo | null;
  routing: Routing;
  providerId: string;
  agentEvidence: AgentEvidence | null;
  runRootStale: boolean;
  providers: ProviderInfo[];
  stages: Record<Role, StageInfo>;
  flow: FlowItem[];
  diffs: DiffRow[];
  proposals: Proposal[];
  verdict: string | null;
  verdictNote: string;
  totals: { latency_s: number | null; tokens: number | null; cost_usd: number | null } | null;
  /** Ham hata metni (kanonik `error_code`'dan bağımsız) -- terminal hata
      kartındaki "Ayrıntılar" açılır bölümünde gösterilir (bkz. AiPanel). */
  error: string | null;
  /** A5 (hata UX): backend'in TEK Türkçe eşleme noktasından (bkz.
      webhost/api/run.py _ERROR_MESSAGES/_error_details) gelen kısa başlık +
      açıklama -- yoksa (ör. eski bir run.finished) AiPanel kendi genel
      DECISION_META metnine düşer. */
  errorTitle: string | null;
  errorDescription: string | null;
  checkpointId: string | null;
  lastRestoredCheckpointId: string | null;
  checkpointBusy: boolean;

  loadProviders: () => Promise<void>;
  setRouting: (role: Role, provider: string) => void;
  setProviderId: (providerId: string) => void;
  setTask: (task: string) => void;
  /** F6 (@-mentions): halihazırda pinliyse yoksayılır; MAX_MENTIONS'da doludur. */
  addMention: (path: string) => boolean;
  removeMention: (path: string) => void;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
  /** F2 (takip isteği): setFollowUpDraft ile yazılan metni run.followUp'a gönderir. */
  setFollowUpDraft: (text: string) => void;
  followUp: () => Promise<void>;
  toggleDiff: (path: string) => void;
  apply: () => Promise<void>;
  restoreCheckpoint: (checkpointId?: string) => Promise<void>;
  reject: () => Promise<void>;
  /** olay kanalı aboneliği — App mount'ta bir kez */
  install: () => void;
  newDraft: () => void;
  selectRun: (runId: string) => void;
  refreshRuns: () => Promise<void>;
}

const IDLE_STAGES = (): Record<Role, StageInfo> => ({
  planner: { role: "planner", state: "idle", provider: "", output: "" },
  coder: { role: "coder", state: "idle", provider: "", output: "" },
  reviewer: { role: "reviewer", state: "idle", provider: "", output: "" },
});

let flowId = 1;
let installed = false;
let startFlights = 0;
let draftGeneration = 0;
const pendingDraftStarts = new Set<number>();
const followUpFlights = new Set<string>();
const mutationFlights = new Set<string>();
let workspaceGeneration = 0;
let workspaceRoot: string | null = null;
let workspaceTracker: Promise<void> | null = null;
const MAX_TERMINAL_RUNS = 32;
type AdmissionMessage = { type: "event"; ev: RunEvent } | { type: "finished"; payload: import("@/bridge").Events["run.finished"] } | { type: "activity"; item: ActivityItem };
const pendingAdmissionEvents = new Map<string, AdmissionMessage[]>();
const uncertainAdmissionRoots = new Set<string>();

function trimRunRecords() {
  const state = useRun.getState();
  const records = Object.values(state.runs);
  const removable = records.filter((run) => !run.pending && !run.uncertain && run.status !== "running" && run.runStage !== "ready" && !run.proposals.length && !run.checkpointBusy && !followUpFlights.has(run.runId));
  let runs = state.runs;
  while (removable.length > MAX_TERMINAL_RUNS) {
    const index = removable.findIndex((run) => run.runId !== state.selectedRunId);
    if (index < 0) break;
    const [evicted] = removable.splice(index, 1);
    const { [evicted.runId]: _, ...rest } = runs;
    runs = rest;
    void import("@/state/activity").then(({ useActivity }) => useActivity.getState().remove(evicted.runId));
  }
  if (runs !== state.runs) useRun.setState({ runs });
}

function projectSnapshot(run: RunSnapshot) {
  return { selectedRunId: run.runId, runId: run.runId, status: run.status, runStage: run.runStage,
    task: run.task, mentions: run.mentions, providerId: run.providerId, engine: run.engine,
    followUpDraft: run.followUpDraft, plan: run.plan, stages: run.stages, flow: run.flow,
    diffs: run.diffs, proposals: run.proposals, agentEvidence: run.agentEvidence,
    checkpointId: run.checkpointId, checkpointBusy: run.checkpointBusy, totals: run.totals,
    verdict: run.verdict, verdictNote: run.verdictNote, error: run.error,
    errorTitle: run.errorTitle, errorDescription: run.errorDescription,
    lastRestoredCheckpointId: run.lastRestoredCheckpointId, runRootStale: run.root !== workspaceRoot };
}

function patchRun(runId: string, updater: (record: RunSnapshot) => RunSnapshot) {
  const state = useRun.getState();
  const record = state.runs[runId];
  if (!record) return;
  const next = updater(record);
  useRun.setState({ runs: { ...state.runs, [runId]: next }, ...(state.selectedRunId === runId ? projectSnapshot(next) : {}) });
}

async function trackWorkspace() {
  if (!workspaceTracker) {
    workspaceTracker = import("@/state/workspace").then(({ useWorkspace }) => {
      workspaceRoot = useWorkspace.getState().root;
      useWorkspace.subscribe((state) => {
        if (state.root === workspaceRoot) return;
        workspaceRoot = state.root;
        workspaceGeneration += 1;
        const current = useRun.getState();
        current.newDraft();
      });
    });
  }
  await workspaceTracker;
  const { useWorkspace } = await import("@/state/workspace");
  if (useWorkspace.getState().root !== workspaceRoot) {
    workspaceRoot = useWorkspace.getState().root;
    workspaceGeneration += 1;
  }
}

export const useRun = create<RunState>((set, get) => ({
  runs: {},
  selectedRunId: null,
  draftToken: 0,
  draftUncertain: false,
  status: "idle",
  runStage: "draft",
  runId: null,
  task: "",
  mentions: [],
  engine: null,
  followUpDraft: "",
  plan: null,
  routing: { planner: "claude", coder: "deepseek", reviewer: "gemini" },
  providerId: "deepseek",
  agentEvidence: null,
  runRootStale: false,
  providers: [],
  stages: IDLE_STAGES(),
  flow: [],
  diffs: [],
  proposals: [],
  verdict: null,
  verdictNote: "",
  totals: null,
  error: null,
  errorTitle: null,
  errorDescription: null,
  checkpointId: null,
  lastRestoredCheckpointId: null,
  checkpointBusy: false,

  newDraft: () => {
    draftGeneration += 1;
    set({ status: "idle", runStage: "draft", runId: null, task: "", mentions: [], engine: null,
      followUpDraft: "", plan: null, stages: IDLE_STAGES(), flow: [], diffs: [], proposals: [],
      agentEvidence: null, runRootStale: false, verdict: null, verdictNote: "", totals: null,
      error: null, errorTitle: null, errorDescription: null, checkpointId: null,
      lastRestoredCheckpointId: null, checkpointBusy: false, selectedRunId: null, draftToken: draftGeneration,
      draftUncertain: workspaceRoot !== null && uncertainAdmissionRoots.has(workspaceRoot) });
    void import("@/state/activity").then(({ useActivity }) => {
      if (useRun.getState().selectedRunId === null) useActivity.getState().select(null);
    });
  },
  selectRun: (runId) => {
    const record = get().runs[runId];
    if (!record) return;
    set(projectSnapshot(record));
    void import("@/state/activity").then(({ useActivity }) => {
      if (useRun.getState().selectedRunId === runId) useActivity.getState().select(runId);
    });
  },
  refreshRuns: async () => {
    await trackWorkspace();
    const root = workspaceRoot;
    const generation = workspaceGeneration;
    const { runs } = await bridge.call("run.list", {});
    await Promise.all(runs.map(async (item) => {
      if (workspaceRoot !== root || workspaceGeneration !== generation) return;
      if (!get().runs[item.runId]) {
        const status: RunStatus = item.status === "running" || item.status === "queued" || item.status === "waiting_user" ? "running" : item.status === "cancelled" ? "cancelled" : item.status === "succeeded" ? "done" : "failed";
        const placeholder: RunSnapshot = { runId: item.runId, root, revision: 0, status,
          runStage: item.status === "waiting_user" ? "ready" : "working", task: item.task, providerId: item.providerId,
          engine: "agent", followUpDraft: "", plan: null, stages: IDLE_STAGES(), flow: [], diffs: [], proposals: [],
          agentEvidence: null, checkpointId: null, checkpointBusy: false, totals: null, verdict: null, verdictNote: "",
          error: null, errorTitle: null, errorDescription: null, mentions: [], lastRestoredCheckpointId: null, uncertain: true, pending: false };
        set((s) => ({ runs: { ...s.runs, [item.runId]: placeholder } }));
      }
      const before = get().runs[item.runId];
      const revision = before.revision;
      if (before.checkpointBusy || mutationFlights.has(item.runId) || followUpFlights.has(item.runId)) return;
      try {
        const detail = await bridge.call("run.get", { runId: item.runId });
        if (workspaceRoot !== root || workspaceGeneration !== generation || mutationFlights.has(item.runId) || followUpFlights.has(item.runId) || (get().runs[item.runId]?.revision ?? -1) !== revision) return;
        const evidence = detail.evidence ? parseAgentEvidence({ type: "evidence", ...detail.evidence } as RunEvent) : null;
        const restored = detail.phase === "restored" || (before.lastRestoredCheckpointId !== null && before.lastRestoredCheckpointId === detail.checkpointId);
        const snapshot: RunSnapshot = { runId: item.runId, root: workspaceRoot, revision: 1,
          status: detail.status === "running" || detail.status === "queued" ? "running" : detail.status === "cancelled" ? "cancelled" : detail.status === "succeeded" || detail.status === "waiting_user" ? "done" : "failed",
          runStage: restored ? "restored" : detail.phase === "applied" ? "applied" : detail.phase === "rejected" ? "draft" : detail.status === "waiting_user" ? "ready" : detail.status === "running" || detail.status === "queued" ? "working" : detail.status === "cancelled" ? "cancelled" : detail.status === "succeeded" ? "noChanges" : "error",
          task: before.task || detail.task, providerId: detail.providerId, engine: "agent", followUpDraft: before.followUpDraft, plan: before.plan,
          stages: before.stages, flow: before.flow, diffs: detail.proposals.map((p) => ({ path: p.path, isNew: p.is_new, diff: p.diff, checked: before.diffs.find((d) => d.path === p.path)?.checked ?? true })),
          proposals: detail.proposals, agentEvidence: restored || detail.phase === "rejected" ? null : evidence, checkpointId: restored ? null : detail.checkpointId, checkpointBusy: false,
          totals: detail.totals, verdict: before.verdict, verdictNote: before.verdictNote, error: detail.errorCode,
          errorTitle: before.errorTitle, errorDescription: before.errorDescription,
          mentions: before.mentions, lastRestoredCheckpointId: before.lastRestoredCheckpointId, uncertain: detail.status === "unavailable", pending: false };
        useRun.setState((s) => ({ runs: { ...s.runs, [item.runId]: { ...snapshot, revision: revision + 1 } }, ...(s.selectedRunId === item.runId ? projectSnapshot({ ...snapshot, revision: revision + 1 }) : {}) }));
      } catch { /* list remains usable if an individual detail lookup fails */ }
    }));
    trimRunRecords();
  },

  loadProviders: async () => {
    try {
      const [{ providers, recommendedRouting }, prefs] = await Promise.all([
        bridge.call("run.providers", {}),
        bridge.call("settings.get", {}),
      ]);
      // Kullanıcının önceden kaydettiği routing varsa o kalıcıdır (bkz.
      // ui_prefs "routing"); yoksa kullanılabilirliğe göre önerilen atanır.
      // Kaydedilmiş sağlayıcı artık kullanılamıyor olsa da DEĞİŞTİRİLMEZ —
      // Composer'daki mevcut eksik-anahtar/CLI uyarısı zaten bunu gösterir.
      const routing = prefs.routing ?? recommendedRouting;
      set({ providers, routing, ...(get().selectedRunId === null ? { providerId: recommendedRouting.coder } : {}) });
    } catch {
      // varsayılanlar kalır
    }
  },

  setRouting: (role, provider) => {
    const routing = { ...get().routing, [role]: provider };
    set({ routing });
    void import("@/state/settings").then(({ useSettings }) => useSettings.getState().update({ routing }));
  },

  setProviderId: (providerId) => {
    const id = get().selectedRunId;
    if (id) patchRun(id, (r) => ({ ...r, revision: r.revision + 1, providerId }));
    else set({ providerId });
  },

  setTask: (task) => {
    const id = get().selectedRunId;
    if (id) patchRun(id, (r) => ({ ...r, revision: r.revision + 1, task }));
    else set({ task });
  },

  addMention: (path) => {
    const { mentions } = get();
    if (mentions.includes(path)) return true;
    if (mentions.length >= MAX_MENTIONS) return false;
    const next = [...mentions, path];
    const id = get().selectedRunId;
    if (id) patchRun(id, (r) => ({ ...r, revision: r.revision + 1, mentions: next }));
    else set({ mentions: next });
    return true;
  },

  removeMention: (path) => {
    const mentions = get().mentions.filter((m) => m !== path);
    const id = get().selectedRunId;
    if (id) patchRun(id, (r) => ({ ...r, revision: r.revision + 1, mentions }));
    else set({ mentions });
  },

  start: async () => {
    if (get().status === "running" || get().draftUncertain) return;
    if (get().selectedRunId !== null) { toast.info("Yeni çalışma için Yeni görev düğmesini kullanın."); return; }
    const draftToken = draftGeneration;
    if (pendingDraftStarts.has(draftToken)) return;
    pendingDraftStarts.add(draftToken);
    startFlights += 1;
    const draftTokenAtStart = get().draftToken;
    const draft = get();
    const capturedRoot = workspaceRoot;
    const capturedGeneration = workspaceGeneration;
    let requestIssued = false;
    try {
    await trackWorkspace();
    const root = capturedRoot ?? workspaceRoot;
    const generation = capturedRoot === null ? workspaceGeneration : capturedGeneration;
    if (root && uncertainAdmissionRoots.has(root)) { set({ draftUncertain: true }); return; }
    const { task, providerId, providers, mentions, runStage } = draft;
    if ((await import("@/state/delivery")).useDelivery.getState().busy) { toast.info("Ortak teslim işlemi sürerken yeni koşu başlatılamaz."); return; }
    if (get().status === "running") return;
    if (runStage === "ready") {
      toast.info("Önce hazır değişiklikleri inceleyin veya vazgeçin.");
      return;
    }
    if (!task.trim()) {
      toast.info("Görev boş.");
      return;
    }
    const { useCollaboration } = await import("@/state/collaboration");
    const collaboration = useCollaboration.getState();
    if (collaboration.enabled) {
      toast.info("Ortak bağlam bu yeni tek ajan akışına henüz bağlı değil; çalıştırmak için ortak bağlamı kapatın.");
      return;
    }
    const selected = providers.find((provider) => provider.id === providerId);
    if (!selected || selected.engineSupported === false) {
      toast.info(selected?.engineReason || "Seçili sağlayıcı bu tek ajan akışında desteklenmiyor.");
      return;
    }
    if (workspaceGeneration !== generation || workspaceRoot !== root || get().draftToken !== draft.draftToken) return;
    set({
      status: "running",
      runStage: "working",
      stages: IDLE_STAGES(),
      flow: [{ id: flowId++, kind: "task", text: task.trim() }],
      plan: null,
      diffs: [],
      proposals: [],
      verdict: null,
      verdictNote: "",
      totals: null,
      agentEvidence: null,
      error: null,
      errorTitle: null,
      errorDescription: null,
      checkpointId: null,
      engine: "agent",
      runRootStale: false,
    });
    try {
      requestIssued = true;
      if (workspaceGeneration !== generation || workspaceRoot !== root) return;
      const { runId } = await bridge.call("run.start", { task: task.trim(), providerId, mentions });
      const snapshot: RunSnapshot = { runId, root, revision: 1, status: "running", runStage: "working", task: task.trim(), providerId, engine: "agent", followUpDraft: "", plan: null, stages: IDLE_STAGES(), flow: [{ id: flowId++, kind: "task", text: task.trim() }], diffs: [], proposals: [], agentEvidence: null, checkpointId: null, checkpointBusy: false, totals: null, verdict: null, verdictNote: "", error: null, errorTitle: null, errorDescription: null, mentions: [...mentions], lastRestoredCheckpointId: null, uncertain: false, pending: false };
      useRun.setState((s) => ({ runs: { ...s.runs, [runId]: snapshot }, ...(s.draftToken === draftTokenAtStart && s.selectedRunId === null && workspaceGeneration === generation && workspaceRoot === root ? projectSnapshot(snapshot) : {}) }));
      trimRunRecords();
      const buffered = pendingAdmissionEvents.get(runId) ?? [];
      pendingAdmissionEvents.delete(runId);
      for (const message of buffered) {
        if (message.type === "event") routeRunEvent(runId, message.ev);
        else if (message.type === "finished") routeRunFinished(message.payload);
        else void import("@/state/activity").then(({ useActivity }) => useActivity.getState().append(message.item));
      }
      useCollaboration.getState().attachRun(null);
      // F1 (canlı ajan etkinliği): Etkinlik sekmesi bir önceki koşudan kalan
      // öğeleri göstermesin diye döngüsel importu geciktir (diğer state
      // dosyalarındaki desenle aynı).
      void import("@/state/activity").then(({ useActivity }) => {
        if (useRun.getState().selectedRunId === runId) useActivity.getState().reset(runId);
      });
    } catch (e) {
      {
        if (!(e instanceof BridgeError) && root) uncertainAdmissionRoots.add(root);
        const staleRoot = workspaceGeneration !== generation || workspaceRoot !== root;
        if (get().draftToken === draftTokenAtStart && get().selectedRunId === null && (!staleRoot || e instanceof BridgeError)) {
          set({ status: "failed", runStage: "error", runId: null, draftUncertain: !(e instanceof BridgeError), error: staleRoot ? "Proje değişirken koşu isteği reddedildi; eski projede etkin koşu yok." : e instanceof Error ? e.message : "Koşu başlatılamadı." });
          toast.err(e instanceof Error ? e.message : "Koşu başlatılamadı.");
        }
      }
    }
    } finally {
      if (!requestIssued && get().runRootStale && get().status === "running") {
        if (get().draftToken === draftTokenAtStart && get().selectedRunId === null) set({ status: "failed", runStage: "error", runId: null, error: "Proje değişmeden önce koşu isteği gönderilmedi." });
      }
      startFlights = Math.max(0, startFlights - 1);
      pendingDraftStarts.delete(draftToken);
      if (startFlights === 0) pendingAdmissionEvents.clear();
    }
  },

  cancel: async () => {
    const { runId, status } = get();
    if (status !== "running") return;
    if (!runId) return;
    await bridge.call("run.cancel", { runId });
  },

  setFollowUpDraft: (text) => {
    const id = get().selectedRunId;
    if (id) patchRun(id, (r) => ({ ...r, revision: r.revision + 1, followUpDraft: text }));
    else set({ followUpDraft: text });
  },

  followUp: async () => {
    const invocation = get();
    const expectedRunId = invocation.runId;
    const record = expectedRunId ? invocation.runs[expectedRunId] : undefined;
    const root = record?.root ?? null;
    const generation = workspaceGeneration;
    if (!expectedRunId || followUpFlights.has(expectedRunId) || mutationFlights.has(expectedRunId)) return;
    followUpFlights.add(expectedRunId);
    let requestIssued = false;
    try {
    await trackWorkspace();
    if (!record || !root || workspaceRoot !== root || workspaceGeneration !== generation || record.uncertain) return;
    if ((await import("@/state/delivery")).useDelivery.getState().busy) { toast.info("Ortak teslim işlemi sürerken takip koşusu başlatılamaz."); return; }
    if ((await import("@/state/collaboration")).useCollaboration.getState().enabled) {
      toast.info("Bu yeni akışta ortak bağlam henüz bağlı değil. Takip koşusu başlatılmadı.");
      return;
    }
    const { followUpDraft, mentions, status, runStage, engine } = record;
    if (status === "running") return;
    if (runStage !== "ready") return;
    if (engine !== "pipeline" && engine !== "agent") {
      toast.info("Bu koşuda takip isteği desteklenmiyor; yeni bir görev başlatın.");
      return;
    }
    const feedback = followUpDraft.trim();
    if (!feedback) {
      toast.info("Takip isteği boş.");
      return;
    }
    if (workspaceGeneration !== generation || workspaceRoot !== root) return;
    try {
       requestIssued = true;
       if (!expectedRunId || workspaceGeneration !== generation || workspaceRoot !== root) return;
       patchRun(expectedRunId, (r) => ({ ...r, revision: r.revision + 1, pending: true }));
       await bridge.call("run.followUp", { runId: expectedRunId, feedback, mentions });
      // Sunucu, continuation'ı başlatmadan ÖNCE bir "followUpStarted" run.event
      // yayınlar (bkz. webhost/api/run.py) -- diff/proposal sıfırlama ve
      // kullanıcı mesajının akışa (flow) eklenmesi ORADA yapılır (consume()),
      // burada değil; böylece tek bir doğruluk kaynağı olur.
    } catch (e) {
      {
        const knownPreExecution = e instanceof BridgeError && ["busy", "no_active_run", "wrong_root", "stale_run", "run_project_mismatch", "run_not_ready"].includes(e.code);
         if (knownPreExecution) patchRun(expectedRunId, (r) => ({ ...r, revision: r.revision + 1, pending: false }));
         else patchRun(expectedRunId, (r) => ({ ...r, revision: r.revision + 1, pending: false, status: "failed", runStage: "error", uncertain: true, proposals: [], diffs: [], agentEvidence: null, error: "Takip isteği kabulü doğrulanamadı. Sonuç yetkisi teyitsiz; tekrar veya yeni işlem yapmadan önce durumu kontrol edin." }));
        toast.err(e instanceof Error ? e.message : "Takip isteği başarısız.");
      }
    }
    } finally {
      if (!requestIssued && get().runRootStale && get().status === "running") {
        patchRun(expectedRunId, (r) => ({ ...r, revision: r.revision + 1, status: "failed", runStage: "error", uncertain: true, proposals: [], diffs: [], error: "Proje değişmeden önce takip isteği gönderilmedi." }));
      }
      followUpFlights.delete(expectedRunId);
    }
  },

  toggleDiff: (path) => {
    const id = get().selectedRunId;
    if (!id) return;
    patchRun(id, (r) => ({ ...r, revision: r.revision + 1,
      diffs: r.diffs.map((d) => d.path === path ? { ...d, checked: !d.checked } : d) }));
  },

  apply: async () => {
    const origin = get();
    const runId = origin.runId;
    const record = runId ? origin.runs[runId] : undefined;
    const root = record?.root ?? null;
    const generation = workspaceGeneration;
     if (!runId || !record || !root || root !== workspaceRoot || record.pending || record.uncertain || mutationFlights.has(runId) || followUpFlights.has(runId)) return;
    mutationFlights.add(runId);
    patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, checkpointBusy: true }));
    const paths = record.diffs.filter((d) => d.checked).map((d) => d.path);
    if (paths.length === 0) {
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
      toast.info("Uygulanacak dosya seçilmedi.");
      return;
    }
    try {
      if ((await import("@/state/delivery")).useDelivery.getState().busy) { toast.info("Ortak teslim işlemi sürerken değişiklikler uygulanamaz."); return; }
      const { useEditor } = await import("@/state/editor");
      if (workspaceGeneration !== generation || workspaceRoot !== root) return;
      const dirty = useEditor.getState().tabs.filter((t) => t.dirty && paths.includes(t.rel));
      if (dirty.length) { toast.err(`Önce kaydedilmemiş sekmeleri kaydedin: ${dirty.map((t) => t.name).join(", ")}`); return; }
      const { applied, errors, conflicts, checkpointId } = await bridge.call("run.applyProposals", { runId, paths });
      for (const c of conflicts ?? []) toast.err(c.reason);
      for (const e of errors) toast.err(`${e.path}: ${e.message}`);
      if (!applied.length) return;
      if (!checkpointId) throw new Error("Uygulama tamamlandı ancak checkpoint kimliği alınamadı.");
      if (workspaceGeneration === generation && workspaceRoot === root && get().selectedRunId === runId) await refreshProjectFiles(applied, runId, root, generation);
      patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, diffs: [], proposals: [], runStage: "applied", mentions: [], checkpointId, lastRestoredCheckpointId: null }));
      toast.ok(`${applied.length} dosya uygulandı · checkpoint hazır.`, {
        label: "Geri Al",
        run: () => {
          if (get().selectedRunId !== runId || workspaceRoot !== root) { toast.info("Koşuyu ve projeyi yeniden seçin; geri alma başlatılmadı."); return; }
          void get().restoreCheckpoint(checkpointId);
        },
      });
    } catch (e) {
      const knownRefusal = e instanceof BridgeError && ["busy", "wrong_root", "stale_run", "no_proposals", "run_not_ready"].includes(e.code);
      if (!knownRefusal) patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, status: "failed", runStage: "error", uncertain: true, proposals: [], diffs: [], agentEvidence: null, error: "Uygulama kabulü doğrulanamadı; eski öneri kanıtı kullanılamaz." }));
      toast.err(e instanceof Error ? e.message : "Uygulanamadı.");
    } finally {
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
    }
  },

  restoreCheckpoint: async (requestedId) => {
    const origin = get();
    const runId = origin.runId;
    const record = runId ? origin.runs[runId] : undefined;
    const checkpointId = requestedId ?? record?.checkpointId;
    const root = record?.root ?? null;
    const generation = workspaceGeneration;
     if (!runId || !record || record.pending || record.uncertain || !checkpointId || checkpointId !== record.checkpointId || !root || root !== workspaceRoot || mutationFlights.has(runId) || followUpFlights.has(runId)) {
      toast.info("Geri alınacak checkpoint yok.");
      return;
    }
    mutationFlights.add(runId);
    patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, checkpointBusy: true }));
    try {
    let checkpoint: Checkpoint | undefined;
    try {
      const { checkpoints } = await bridge.call("checkpoint.list", {});
      checkpoint = checkpoints.find((item) => item.id === checkpointId);
    } catch {
      toast.err("Checkpoint bilgisi doğrulanamadı.");
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
      return;
    }
    if (!checkpoint || checkpoint.runId !== runId) {
      toast.err("Checkpoint bulunamadı veya artık geçerli değil.");
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
      return;
    }
    const { useEditor } = await import("@/state/editor");
    const dirty = useEditor.getState().tabs.filter(
      (tab) => tab.dirty && checkpoint.files.includes(tab.rel),
    );
    if (dirty.length) {
      toast.err(`Geri almadan önce kaydedilmemiş sekmeleri kaydedin: ${dirty.map((t) => t.name).join(", ")}`);
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
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
    if (!accepted) { mutationFlights.delete(runId); patchRun(runId, (r) => ({ ...r, checkpointBusy: false })); return; }
    try {
      if (workspaceGeneration !== generation || workspaceRoot !== root) return;
      const { restored } = await bridge.call("checkpoint.restore", { checkpointId });
      if (get().selectedRunId === runId && workspaceRoot === root && workspaceGeneration === generation) await refreshProjectFiles(restored, runId, root, generation);
      patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, runStage: "restored", checkpointId: null,
        lastRestoredCheckpointId: checkpointId, diffs: [], proposals: [], agentEvidence: null }));
      toast.ok(`${restored.length} dosya checkpoint'e geri alındı.`);
    } catch (e) {
      const knownRefusal = e instanceof BridgeError && ["wrong_root", "stale_checkpoint", "checkpoint_not_found"].includes(e.code);
      if (!knownRefusal) patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, uncertain: true, proposals: [], diffs: [], agentEvidence: null }));
      toast.err(e instanceof Error ? e.message : "Checkpoint geri alınamadı.");
    } finally {
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
    }
    } finally {
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
    }
  },

  reject: async () => {
    const origin = get();
    const runId = origin.runId;
    const record = runId ? origin.runs[runId] : undefined;
    const root = record?.root ?? null;
    const generation = workspaceGeneration;
     if (!runId || !record || !root || root !== workspaceRoot || record.pending || record.uncertain || mutationFlights.has(runId) || followUpFlights.has(runId)) return;
    mutationFlights.add(runId);
    patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, checkpointBusy: true }));
    try {
      if ((await import("@/state/delivery")).useDelivery.getState().busy) { toast.info("Ortak teslim işlemi sürerken öneriler reddedilemez."); return; }
      if (workspaceGeneration !== generation || workspaceRoot !== root) return;
      await bridge.call("run.rejectProposals", { runId });
      patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, diffs: [], proposals: [], runStage: "draft", mentions: [], agentEvidence: null, totals: null, verdict: null, verdictNote: "", followUpDraft: "", checkpointBusy: false }));
      const { useEditor } = await import("@/state/editor");
      if (get().selectedRunId === runId && workspaceRoot === root && workspaceGeneration === generation) useEditor.getState().closeDiff();
      toast.info("Değişiklikler reddedildi.");
    } catch (e) {
      const knownRefusal = e instanceof BridgeError && ["busy", "wrong_root", "stale_run", "no_proposals", "run_not_ready"].includes(e.code);
      if (!knownRefusal) patchRun(runId, (r) => ({ ...r, revision: r.revision + 1, status: "failed", runStage: "error", uncertain: true, proposals: [], diffs: [], agentEvidence: null, error: "Reddetme kabulü doğrulanamadı; eski öneri kanıtı kullanılamaz." }));
      toast.err(e instanceof Error ? e.message : "Değişiklikler reddedilemedi.");
    } finally {
      mutationFlights.delete(runId);
      patchRun(runId, (r) => ({ ...r, checkpointBusy: false }));
    }
  },

  install: () => {
    if (installed) return;
    installed = true;

    void trackWorkspace();

    bridge.on("run.event", ({ runId, ev }) => {
      if (!get().runs[runId]) { bufferAdmission(runId, { type: "event", ev }); return; }
      routeRunEvent(runId, ev);
    });
    bridge.on("run.finished", (payload) => {
      if (!get().runs[payload.runId]) { bufferAdmission(payload.runId, { type: "finished", payload }); return; }
      routeRunFinished(payload);
    });
  },
}));

function bufferAdmission(runId: string, message: AdmissionMessage) {
  if (!startFlights) return;
  const count = [...pendingAdmissionEvents.values()].reduce((n, batch) => n + batch.length, 0);
  if (count >= 500 || (!pendingAdmissionEvents.has(runId) && pendingAdmissionEvents.size >= 32)) return;
  pendingAdmissionEvents.set(runId, [...(pendingAdmissionEvents.get(runId) ?? []), message]);
}

export function acceptRunActivity(item: ActivityItem): boolean {
  if (useRun.getState().runs[item.runId]) return true;
  bufferAdmission(item.runId, { type: "activity", item });
  return false;
}

function routeRunFinished(payload: import("@/bridge").Events["run.finished"]) {
  const { runId, status, error, errorTitle, errorDescription, engine } = payload;
  patchRun(runId, (record) => ({ ...record, revision: record.revision + 1,
    status: status === "cancelled" ? "cancelled" : status === "failed" ? "failed" : "done",
    runStage: status === "cancelled" ? "cancelled" : status === "failed" ? "error" : record.runStage === "ready" ? "ready" : "noChanges",
    engine: engine ?? record.engine,
    stages: status === "failed" ? failRunning(record.stages) : record.stages,
    flow: status === "failed" ? [...record.flow, { id: flowId++, kind: "error" as const, text: `Hata: ${error ?? "bilinmiyor"}` }].slice(-500) : record.flow,
    error: status === "failed" ? error ?? null : record.error,
    errorTitle: status === "failed" ? errorTitle ?? null : record.errorTitle,
    errorDescription: status === "failed" ? errorDescription ?? null : record.errorDescription }));
  trimRunRecords();
}

function routeRunEvent(runId: string, ev: RunEvent) {
  const snapshot = useRun.getState().runs[runId];
  if (!snapshot) return;
  let projected = { ...useRun.getState(), ...projectSnapshot(snapshot) } as ReturnType<typeof useRun.getState>;
  const update = (fn: (s: ReturnType<typeof useRun.getState>) => Partial<ReturnType<typeof useRun.getState>>) => {
    projected = { ...projected, ...fn(projected) };
  };
  consume(ev, update, () => projected);
  const next: RunSnapshot = { ...snapshot, revision: snapshot.revision + 1, status: projected.status,
    ...(ev.type === "followUpStarted" ? { pending: false, uncertain: false } : {}),
    runStage: projected.runStage, task: projected.task, providerId: projected.providerId, engine: projected.engine,
    followUpDraft: projected.followUpDraft, plan: projected.plan, stages: projected.stages,
    flow: projected.flow.slice(-500), diffs: projected.diffs, proposals: projected.proposals,
    agentEvidence: projected.agentEvidence, checkpointId: projected.checkpointId, checkpointBusy: projected.checkpointBusy,
    totals: projected.totals, verdict: projected.verdict, verdictNote: projected.verdictNote,
    error: projected.error, errorTitle: projected.errorTitle, errorDescription: projected.errorDescription,
    mentions: projected.mentions, lastRestoredCheckpointId: projected.lastRestoredCheckpointId };
  useRun.setState((s) => ({ runs: { ...s.runs, [runId]: next }, ...(s.selectedRunId === runId ? projectSnapshot(next) : {}) }));
  trimRunRecords();
}

function failRunning(stages: Record<Role, StageInfo>): Record<Role, StageInfo> {
  return Object.fromEntries(Object.entries(stages).map(([role, stage]) => [role, stage.state === "running" ? { ...stage, state: "error" } : stage])) as Record<Role, StageInfo>;
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
            // A4-A2: `startedAt`, Pipeline.tsx'in canlı geçen-süre sayacının
            // taban noktasıdır -- token/maliyet gibi kalıntı değerler de
            // burada sıfırlanır (yeni rol, önceki rolün metriklerini
            // MİRAS ALMAZ).
            [role]: {
              ...s.stages[role], state: "running", provider,
              startedAt: Date.now(), tokens: undefined, cost_usd: undefined, latency_s: undefined,
            },
          }
        : s.stages,
      flow: [...s.flow, { id: flowId++, kind: "stage", text: provider, stage }],
    }));
  } else if (type === "info") {
    set((s) => ({ flow: [...s.flow, { id: flowId++, kind: "info", text: ev.text as string }] }));
  } else if (type === "summary") {
    // Worker'ın son mesajı (bkz. webhost/api/run.py _last_worker_final_message)
    // -- "Değişiklik önerisi çıkmadı." bilgi satırının altında görünür.
    // kind="info" (yeşil "başarı" görünümü değil) kasıtlı: bu satır bir
    // proposal-hazır özeti değil, ajanın ne yaptığını/denediğini anlatan
    // düz bir açıklama.
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
      // A4-A2 (rol başına canlı sayaç): `partial` -- backend'in rol hâlâ
      // ÇALIŞIRKEN gönderdiği ara tik (bkz. webhost/api/run.py
      // _emit_role_metric) -- tokens/cost_usd/latency_s güncellenir ama
      // `state` DOKUNULMAZ (rol "running" kalır); yalnızca son (partial
      // OLMAYAN) metric rolü "done"ya taşır. tokens/cost_usd `null` ise
      // (ör. ACP/hesap rotasında hiç usage yoksa) Pipeline/Chat "—" gösterir.
      const partial = !!ev.partial;
      set((s) => ({
        stages: {
          ...s.stages,
          [role]: {
            ...s.stages[role],
            state: partial ? s.stages[role].state : "done",
            model: (ev.model as string) ?? s.stages[role].model,
            latency_s: ev.latency_s as number,
            tokens: ev.tokens as number | null,
            cost_usd: ev.cost_usd as number | null,
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
  } else if (type === "followUpStarted") {
    // F2 (takip isteği): webhost/api/run.py'nin run.followUp handler'ı bu
    // olayı continuation başlamadan ÖNCE yayınlar -- diff/proposal durumu
    // burada sıfırlanır (flow/chat GEÇMİŞİ korunur, yalnızca EKLENİR).
    set((s) => ({
      status: "running",
      runStage: "working",
      diffs: [],
      proposals: [],
      verdict: null,
      verdictNote: "",
      agentEvidence: null,
      checkpointId: null,
      followUpDraft: "",
      flow: [...s.flow, { id: flowId++, kind: "task", text: (ev.feedback as string) ?? "" }],
    }));
  } else if (type === "verdict") {
    set(() => ({ verdict: ev.verdict as string, verdictNote: (ev.note as string) ?? "" }));
  } else if (type === "proposal") {
    const proposals = (ev.proposals as Proposal[]) ?? [];
    const rawTotals = ev.totals as { latency_s?: number | null; tokens?: number | null; cost_usd?: number | null } | undefined;
    const totals = rawTotals ? { latency_s: rawTotals.latency_s ?? null, tokens: rawTotals.tokens ?? null, cost_usd: rawTotals.cost_usd ?? null } : null;
    set((s) => ({
      proposals,
      runStage: "ready",
      totals: totals as RunState["totals"],
      verdict: (ev.verdict as string) ?? s.verdict,
      flow: [
        ...s.flow,
        proposals.length
          ? { id: flowId++, kind: "summary", text: `${proposals.length} dosya önerisi hazır.` }
          : { id: flowId++, kind: "info", text: "Değişiklik önerisi çıkmadı." },
      ],
    }));
  } else if (type === "evidence") {
    set(() => ({ agentEvidence: parseAgentEvidence(ev) }));
  }
  void get; // (şimdilik kullanılmıyor)
}

function parseAgentEvidence(ev: RunEvent): AgentEvidence {
  const raw = ev as unknown as Record<string, unknown>;
  const validOutcomes = new Set(["pass", "fail", "not_run", "error", "invalidated", "timeout"]);
  const validCheckStatuses = new Set(["pass", "fail", "not_run", "error", "invalidated", "timeout", "skipped"]);
  let unknown = false;
  let truncated = raw.truncated === true;
  const text = (value: unknown, cap: number, nullable = false): string | null => {
    if (nullable && value === null) return null;
    if (typeof value !== "string") { unknown = true; return null; }
    if (value.length > cap) { unknown = true; truncated = true; return value.slice(0, cap); }
    return value;
  };
  const reason = text(raw.reason, 128) ?? "unknown";
  const execution_id = text(raw.execution_id, 128) ?? "unknown";
  const agent_message = text(raw.agent_message, 20_000) ?? "";
  if (!reason || reason === "unknown" || !execution_id || execution_id === "unknown") unknown = true;
  const receipt = raw.attempt_receipt && typeof raw.attempt_receipt === "object" ? raw.attempt_receipt as Record<string, unknown> : null;
  if (!receipt) unknown = true;
  const count = (value: unknown) => {
    if (value === null) return null;
    if (typeof value !== "number" || !Number.isInteger(value) || value < 0) { unknown = true; return null; }
    return value;
  };
  const changedRaw = raw.changed_paths;
  if (!Array.isArray(changedRaw)) unknown = true;
  if (Array.isArray(changedRaw) && changedRaw.length > 256) { unknown = true; truncated = true; }
  const changed_paths = (Array.isArray(changedRaw) ? changedRaw.slice(0, 256) : []).map((path) => text(path, 1024) ?? "");
  const diff_sha256 = text(raw.diff_sha256, 128, true);
  const verificationRaw = raw.verification && typeof raw.verification === "object" ? raw.verification as Record<string, unknown> : null;
  if (!verificationRaw) unknown = true;
  const outcomeRaw = verificationRaw?.outcome;
  const outcome = typeof outcomeRaw === "string" && validOutcomes.has(outcomeRaw) ? outcomeRaw : "unknown";
  if (outcome === "unknown") unknown = true;
  const bool = (value: unknown) => {
    if (typeof value !== "boolean") { unknown = true; return null; }
    return value;
  };
  const verification_id = text(verificationRaw?.verification_id, 128, true);
  const plan_id = text(verificationRaw?.plan_id, 128, true);
  const checksRaw = verificationRaw?.checks;
  if (!Array.isArray(checksRaw)) unknown = true;
  if (Array.isArray(checksRaw) && checksRaw.length > 100) { unknown = true; truncated = true; }
  const checks = (Array.isArray(checksRaw) ? checksRaw.slice(0, 100) : []).map((item) => {
    if (!item || typeof item !== "object") { unknown = true; return { check_id: "unknown", status: "unknown" }; }
    const check = item as Record<string, unknown>;
    const check_id = text(check.check_id, 128) ?? "unknown";
    const status = typeof check.status === "string" && validCheckStatuses.has(check.status) ? check.status : "unknown";
    if (!check_id || check_id === "unknown" || status === "unknown") unknown = true;
    return { check_id, status };
  });
  if (raw.truncated !== undefined && typeof raw.truncated !== "boolean") { unknown = true; truncated = true; }
  return {
    unknown, truncated, reason, execution_id, agent_message,
    attempt_receipt: { model_turns: count(receipt?.model_turns), tool_calls: count(receipt?.tool_calls) },
    changed_paths, diff_sha256,
    verification: {
      outcome,
      fingerprint_complete: bool(verificationRaw?.fingerprint_complete),
      changed_content: bool(verificationRaw?.changed_content),
      verification_id, plan_id, checks,
    },
  };
}

function markPrevDone(stages: Record<Role, StageInfo>): Record<Role, StageInfo> {
  const out = { ...stages };
  for (const r of Object.keys(out) as Role[]) {
    if (out[r].state === "running") out[r] = { ...out[r], state: "done" };
  }
  return out;
}

async function refreshProjectFiles(paths: string[], runId: string, root: string, generation: number) {
  const [{ useEditor }, { useWorkspace }, { useScm }] = await Promise.all([
    import("@/state/editor"),
    import("@/state/workspace"),
    import("@/state/scm"),
  ]);
  const editor = useEditor.getState();
  for (const rel of paths) {
    if (workspaceRoot !== root || workspaceGeneration !== generation || useRun.getState().selectedRunId !== runId) return;
    if (!editor.tabs.some((tab) => tab.rel === rel)) continue;
    try {
      const { content } = await bridge.call("fs.readFile", { rel });
      if (workspaceRoot !== root || workspaceGeneration !== generation || useRun.getState().selectedRunId !== runId) return;
      useEditor.setState((state) => ({
        tabs: state.tabs.map((tab) =>
          tab.rel === rel ? { ...tab, content, draft: content, dirty: false } : tab,
        ),
      }));
    } catch {
      useEditor.getState().closeDeleted(rel);
    }
  }
  if (workspaceRoot !== root || workspaceGeneration !== generation || useRun.getState().selectedRunId !== runId) return;
  const workspace = useWorkspace.getState();
  await Promise.all(Object.keys(workspace.children).map((rel) => workspace.loadDir(rel)));
  await useScm.getState().refresh();
  if (workspaceRoot === root && workspaceGeneration === generation && useRun.getState().selectedRunId === runId) useEditor.getState().closeDiff();
}
