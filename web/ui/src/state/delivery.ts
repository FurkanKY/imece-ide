import { create } from "zustand";
import { bridge, BridgeError, DeliveryCandidate, DeliveryPreview, DeliveryProposal } from "@/bridge";

interface DeliveryState {
  root: string | null; runId: string | null; storePath: string; hubPath: string; outputPath: string;
  preview: DeliveryPreview | null; previewSelectionPaths: string[]; proposals: DeliveryProposal[]; selectedProposalIds: string[];
  candidate: DeliveryCandidate | null; conflicts: string[]; error: string | null; busy: boolean;
  verify: boolean; allowOutOfScope: boolean; publishedReceipt: string | null;
  configure: (field: "storePath" | "hubPath" | "outputPath", value: string) => void;
  adoptChannelPaths: (root: string, runId: string | null, storePath: string, hubPath: string) => boolean;
  setVerify: (value: boolean) => void; setAllowOutOfScope: (value: boolean) => void;
  toggleProposal: (id: string) => void; reset: (root: string | null, runId?: string | null) => void;
  previewDelivery: (input: { root: string; runId: string; paths: string[] }) => Promise<void>;
  publish: (root: string, runId: string) => Promise<void>; refresh: (root: string, runId: string) => Promise<void>;
  combine: (root: string, runId: string) => Promise<void>; discardPreview: () => Promise<void>;
}

let generation = 0;
const valid = (root: string, runId: string, token: number) => generation === token && useDelivery.getState().root === root && useDelivery.getState().runId === runId;
const discard = (previewId?: string) => previewId ? bridge.call("collab.delivery.discard", { previewId }).catch(() => undefined) : Promise.resolve();

export const useDelivery = create<DeliveryState>((set, get) => ({
  root: null, runId: null, storePath: "", hubPath: "", outputPath: "", preview: null, previewSelectionPaths: [], proposals: [],
  selectedProposalIds: [], candidate: null, conflicts: [], error: null, busy: false, verify: false,
  allowOutOfScope: false, publishedReceipt: null,
  configure: (field, value) => {
    if (get().busy) return;
    const oldPreview = get().preview;
    generation++;
    set({ [field]: value, preview: null, previewSelectionPaths: [], candidate: null, conflicts: [], error: null, allowOutOfScope: false, ...(field !== "outputPath" ? { proposals: [], selectedProposalIds: [] } : {}) });
    void discard(oldPreview?.previewId);
  },
  adoptChannelPaths: (root, runId, storePath, hubPath) => {
    if (!root || !storePath || !hubPath) return false;
    generation++;
    const preview = get().preview;
    set({ root, runId, storePath, hubPath, preview: null, previewSelectionPaths: [], proposals: [], selectedProposalIds: [], candidate: null, conflicts: [], error: null, allowOutOfScope: false });
    void discard(preview?.previewId);
    return true;
  },
  setVerify: (verify) => set({ verify }),
  setAllowOutOfScope: (allowOutOfScope) => set({ allowOutOfScope }),
  toggleProposal: (id) => set((s) => {
    const selected = new Set(s.selectedProposalIds);
    if (selected.has(id)) selected.delete(id);
    else {
      const proposal = s.proposals.find((item) => item.proposalId === id);
      if (!proposal || !proposal.currentContextHashMatches) return { error: "Yalnız listelenmiş ve güncel bağlamı eşleşen öneriler seçilebilir." };
      if (selected.size >= 16) return { error: "Bir adayda en fazla 16 öneri seçilebilir." };
      if (s.proposals.some((item) => selected.has(item.proposalId) && item.taskId === proposal.taskId)) return { error: "Her görev için yalnız bir öneri seçin. Mevcut seçimi otomatik değiştirmedik." };
      selected.add(id);
    }
    return { selectedProposalIds: [...selected], candidate: null, conflicts: [], error: null };
  }),
  reset: (root, runId = null) => {
    const s = get();
    if (s.root === root && s.runId === runId) return;
    generation++;
    void discard(s.preview?.previewId);
    // A new execution discards authorization/results, not the same project's
    // explicitly supplied channel configuration. Another root keeps neither.
    set({ root, runId, storePath: s.root === root ? s.storePath : "", hubPath: s.root === root ? s.hubPath : "", outputPath: "", preview: null, previewSelectionPaths: [], proposals: [], selectedProposalIds: [], candidate: null, conflicts: [], error: null, busy: s.busy, verify: false, allowOutOfScope: false, publishedReceipt: null });
  },
  previewDelivery: async ({ root, runId, paths }) => {
    const s = get();
    const requestedPaths = [...new Set(paths)].sort();
    if (s.busy || s.root !== root || s.runId !== runId || !s.storePath.trim() || !s.hubPath.trim() || !requestedPaths.length || requestedPaths.length > 64) return;
    const token = ++generation;
    set({ busy: true, preview: null, previewSelectionPaths: requestedPaths, candidate: null, conflicts: [], error: null, allowOutOfScope: false });
    await discard(s.preview?.previewId);
    if (!valid(root, runId, token)) { set({ busy: false }); return; }
    try {
      const preview = await bridge.call("collab.delivery.preview", { runId, storePath: s.storePath.trim(), hubPath: s.hubPath.trim(), paths: requestedPaths });
      if (!valid(root, runId, token)) { await discard(preview.previewId); set({ busy: false }); return; }
      if (preview.paths.some((path) => !requestedPaths.includes(path))) {
        await discard(preview.previewId);
        set({ preview: null, previewSelectionPaths: [], busy: false, error: "Önizleme beklenmeyen bir dosya içerdiği için atıldı." });
        return;
      }
      set({ preview, previewSelectionPaths: requestedPaths, busy: false });
    } catch (e) { if (valid(root, runId, token)) set({ busy: false, previewSelectionPaths: [], error: e instanceof Error ? e.message : "Önizleme alınamadı." }); else set({ busy: false }); }
  },
  publish: async (root, runId) => {
    const { preview, allowOutOfScope, busy } = get();
    if (!preview || busy || (preview.outOfScopePaths.length > 0 && !allowOutOfScope)) return;
    const token = generation;
    set({ busy: true, error: null });
    try {
      const receipt = await bridge.call("collab.delivery.publish", { runId, previewId: preview.previewId, allowOutOfScope });
      if (!valid(root, runId, token)) { set({ busy: false }); return; }
      set({ busy: false, preview: null, previewSelectionPaths: [], publishedReceipt: receipt.proposalId, error: null });
    } catch (e) {
      if (!valid(root, runId, token)) { set({ busy: false }); return; }
      if (e instanceof BridgeError && ["delivery_stale", "delivery_expired", "delivery_unavailable"].includes(e.code)) {
        set({ busy: false, preview: null, previewSelectionPaths: [], allowOutOfScope: false, error: "Bu teslim bileti artık kullanılamıyor. Yeni bir önizleme alın." });
        await discard(preview.previewId);
        return;
      }
      set({ busy: false, error: e instanceof Error ? e.message : "Yayımlama başarısız." });
    }
  },
  refresh: async (root, runId) => {
    const { storePath, hubPath, busy } = get();
    if (busy || !storePath.trim() || !hubPath.trim()) return;
    const token = ++generation; set({ busy: true, error: null });
    try {
      const { proposals } = await bridge.call("collab.delivery.list", { runId, storePath: storePath.trim(), hubPath: hubPath.trim() });
      if (valid(root, runId, token)) set({ proposals, selectedProposalIds: [], candidate: null, conflicts: [], busy: false }); else set({ busy: false });
    } catch (e) { if (valid(root, runId, token)) set({ busy: false, error: e instanceof Error ? e.message : "Öneriler alınamadı." }); else set({ busy: false }); }
  },
  combine: async (root, runId) => {
    const { storePath, hubPath, outputPath, selectedProposalIds, verify, busy, proposals } = get();
    if (busy) return;
    if (!storePath.trim() || !hubPath.trim() || !outputPath.trim() || !selectedProposalIds.length || selectedProposalIds.length > 16 || new Set(selectedProposalIds).size !== selectedProposalIds.length) {
      set({ error: "Bir ile 16 arasında listelenmiş öneri ve yeni aday dizini seçin." }); return;
    }
    const selected = selectedProposalIds.map((id) => proposals.find((proposal) => proposal.proposalId === id));
    if (selected.some((proposal) => !proposal)) { set({ error: "Seçili öneriler bu yenilenen listede yok. Listeyi yenileyin." }); return; }
    const selectedProposals = selected as DeliveryProposal[];
    if (selectedProposals.some((proposal) => !proposal.currentContextHashMatches)) { set({ error: "Eski bağlamlı öneriler birleştirilemez. Listeyi yenileyin." }); return; }
    if (new Set(selectedProposals.map((proposal) => proposal.taskId)).size !== selectedProposals.length) { set({ error: "Her görev için yalnız bir öneri seçin; seçimi değiştirmeden bırakın." }); return; }
    const token = ++generation; set({ busy: true, error: null, candidate: null, conflicts: [] });
    try {
      const result = await bridge.call("collab.delivery.candidate", { runId, storePath: storePath.trim(), hubPath: hubPath.trim(), proposalIds: [...selectedProposalIds], outputPath: outputPath.trim(), verify });
      if (valid(root, runId, token)) set({ ...result, busy: false }); else set({ busy: false });
    } catch (e) { if (valid(root, runId, token)) set({ busy: false, error: e instanceof Error ? e.message : "Aday oluşturulamadı." }); else set({ busy: false }); }
  },
  discardPreview: async () => {
    generation++;
    const preview = get().preview;
    set({ preview: null, previewSelectionPaths: [], candidate: null, allowOutOfScope: false, error: null });
    await discard(preview?.previewId);
  },
}));
