import { create } from "zustand";
import { bridge, CollaborationPreview, CollaborationStatus } from "@/bridge";

const recoveryStates = new Set(["resnapshot_required", "access_denied", "protocol_error", "server_error", "cleanup_failed"]);
export function collaborationNeedsRecovery(status: CollaborationStatus | null): boolean {
  return !!status && [status.state, status.code, status.recoveryState, status.recoveryCode].some((value) => !!value && recoveryStates.has(value));
}
export function collaborationNeedsCursorReset(status: CollaborationStatus | null): boolean {
  return !!status && [status.state, status.code, status.recoveryState, status.recoveryCode].includes("resnapshot_required");
}

interface CollaborationState {
  enabled: boolean;
  preview: CollaborationPreview | null;
  approvalHandle: string | null;
  approvalProjectRoot: string | null;
  attachedRunId: string | null;
  currentStatus: CollaborationStatus | null;
  busy: boolean;
  error: string | null;
  recoveryConsent: boolean;
  setEnabled: (enabled: boolean) => void;
  setRecoveryConsent: (value: boolean) => void;
  previewContext: (input: { endpoint: string; credential: string; memberId: string; taskId: string }) => Promise<void>;
  approve: (resetCursor: boolean) => Promise<void>;
  refreshStatus: (runId?: string) => Promise<void>;
  discard: () => Promise<void>;
  releaseApproval: () => Promise<void>;
  invalidateDraft: () => Promise<void>;
  adoptLocalPreview: (preview: CollaborationPreview, root: string) => boolean;
  attachRun: (runId: string | null) => void;
  resetRoot: (root: string | null) => void;
}

let generation = 0;
let currentRoot: string | null = null;

async function discardHandles(previewId?: string | null, approvalHandle?: string | null) {
  if (previewId) await bridge.call("collab.discard", { previewId }).catch(() => undefined);
  if (approvalHandle) await bridge.call("collab.discard", { approvalHandle }).catch(() => undefined);
}

export const useCollaboration = create<CollaborationState>((set, get) => ({
  enabled: false,
  preview: null,
  approvalHandle: null,
  approvalProjectRoot: null,
  attachedRunId: null,
  currentStatus: null,
  busy: false,
  error: null,
  recoveryConsent: false,
  setEnabled: (enabled) => {
    const { preview, approvalHandle, attachedRunId } = get();
    if (!enabled && attachedRunId) {
      generation += 1;
      set({ enabled: false, preview: null, approvalHandle: null, approvalProjectRoot: null, busy: false, recoveryConsent: false });
      void discardHandles(preview?.previewId, approvalHandle);
      return;
    }
    generation += 1;
    set({ enabled, preview: null, approvalHandle: null, approvalProjectRoot: null, busy: false, recoveryConsent: false });
    void discardHandles(preview?.previewId, approvalHandle);
  },
  setRecoveryConsent: (recoveryConsent) => set({ recoveryConsent }),
  previewContext: async (input) => {
    const ownGeneration = ++generation;
    const root = currentRoot;
    if (!root) { set({ error: "Önce bir proje açın." }); return; }
    const old = get();
    set({ busy: true, error: null, preview: null, approvalHandle: null, approvalProjectRoot: null, recoveryConsent: false });
    await discardHandles(old.preview?.previewId, old.approvalHandle);
    try {
      const preview = await bridge.call("collab.preview", input);
      if (ownGeneration !== generation || root !== currentRoot) {
        await discardHandles(preview.previewId);
        return;
      }
      set({ preview, busy: false, error: null });
    } catch {
      if (ownGeneration === generation) set({ busy: false, error: "Ortak bağlam önizlenemedi. Uç noktayı ve erişimi denetleyin." });
    }
  },
  approve: async (resetCursor) => {
    const preview = get().preview;
    const ownGeneration = generation;
    const root = currentRoot;
    if (!preview || !root) return;
    set({ busy: true, error: null });
    try {
      const result = await bridge.call("collab.approve", { previewId: preview.previewId, resetCursor });
      if (ownGeneration !== generation || root !== currentRoot) {
        await discardHandles(undefined, result.approvalHandle);
        return;
      }
      set({ preview: result.preview, approvalHandle: result.approvalHandle, approvalProjectRoot: root, busy: false });
    } catch {
      if (ownGeneration === generation) set({ busy: false, error: "Onay tamamlanamadı. Önizlemeyi yeniden alın." });
    }
  },
  refreshStatus: async (runId) => {
    const ownGeneration = generation;
    const root = currentRoot;
    try {
      const { collaboration } = await bridge.call("collab.status", { runId });
      if (currentRoot && currentRoot === root && generation === ownGeneration) set({ currentStatus: collaboration });
    } catch {
      if (currentRoot === root && generation === ownGeneration) set({ error: "Ortak çalışma durumu alınamadı." });
    }
  },
  discard: async () => {
    generation += 1;
    const { preview, approvalHandle } = get();
    set({ preview: null, approvalHandle: null, approvalProjectRoot: null, busy: false, recoveryConsent: false, error: null });
    await discardHandles(preview?.previewId, approvalHandle);
  },
  releaseApproval: async () => {
    const approvalHandle = get().approvalHandle;
    set({ approvalHandle: null, approvalProjectRoot: null, recoveryConsent: false });
    await discardHandles(null, approvalHandle);
  },
  invalidateDraft: async () => {
    await get().discard();
  },
  adoptLocalPreview: (preview, root) => {
    if (!root || root !== currentRoot || preview.projectRoot !== root) return false;
    generation += 1;
    set({ enabled: true, preview, approvalHandle: null, approvalProjectRoot: null, attachedRunId: null, busy: false, error: null, recoveryConsent: false });
    return true;
  },
  attachRun: (attachedRunId) => set({ attachedRunId }),
  resetRoot: (root) => {
    if (root === currentRoot) return;
    generation += 1;
    currentRoot = root;
    const { preview, approvalHandle } = get();
    set({ enabled: false, preview: null, approvalHandle: null, approvalProjectRoot: null, attachedRunId: null, currentStatus: null, busy: false, error: null, recoveryConsent: false });
    void discardHandles(preview?.previewId, approvalHandle);
  },
}));
