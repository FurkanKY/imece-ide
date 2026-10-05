import { create } from "zustand";
import { Api, bridge, OwnerPreview, OwnerStatus } from "@/bridge";
import { useWorkspace } from "@/state/workspace";

interface OwnerState {
  status: OwnerStatus | null;
  preview: OwnerPreview | null;
  draftRoot: string | null;
  busy: boolean;
  error: string | null;
  resetRoot: (root: string | null) => void;
  refresh: () => Promise<void>;
  previewCreate: (root: string, params: Api["collab.owner.previewCreate"]["params"]) => Promise<boolean>;
  create: (previewId: string) => Promise<boolean>;
  select: (params: Api["collab.owner.select"]["params"]) => Promise<boolean>;
  start: () => Promise<boolean>;
  stop: () => Promise<boolean>;
  forgetPreview: () => void;
}

let draftGeneration = 0;
let statusRequest: Promise<void> | null = null;

export const useOwner = create<OwnerState>((set, get) => ({
  status: null, preview: null, draftRoot: null, busy: false, error: null,
  resetRoot: (draftRoot) => {
    if (get().draftRoot === draftRoot) return;
    draftGeneration += 1;
    set({ draftRoot, preview: null, error: null });
  },
  refresh: () => {
    if (statusRequest) return statusRequest;
    statusRequest = bridge.call("collab.owner.status", {}).then((status) => {
      set({ status, error: get().error });
    }).catch(() => {
      set({ error: "Sahip oturumu durumu alınamadı." });
    }).finally(() => { statusRequest = null; });
    return statusRequest;
  },
  previewCreate: async (root, params) => {
    if (get().busy || !root || get().draftRoot !== root) return false;
    const token = ++draftGeneration;
    set({ busy: true, error: null, preview: null });
    try {
      const preview = await bridge.call("collab.owner.previewCreate", params);
      if (token !== draftGeneration || get().draftRoot !== root || preview.projectRoot !== root) return false;
      set({ preview, busy: false });
      return true;
    } catch (e) {
      if (token === draftGeneration) set({ error: e instanceof Error ? e.message : "Önizleme alınamadı." });
      return false;
    } finally {
      if (get().busy) set({ busy: false });
    }
  },
  create: async (previewId) => {
    if (get().busy) return false;
    set({ busy: true, error: null, preview: null });
    try { set({ status: await bridge.call("collab.owner.create", { previewId }), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Oturum oluşturulamadı." }); return false; }
  },
  select: async (params) => {
    if (get().busy) return false;
    set({ busy: true, error: null, preview: null });
    try { set({ status: await bridge.call("collab.owner.select", params), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Oturum seçilemedi." }); return false; }
  },
  start: async () => {
    if (get().busy) return false;
    set({ busy: true, error: null });
    try { set({ status: await bridge.call("collab.owner.start", {}), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Sunucu başlatılamadı." }); return false; }
  },
  stop: async () => {
    if (get().busy) return false;
    set({ busy: true, error: null });
    try { set({ status: await bridge.call("collab.owner.stop", {}), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Sunucu durdurulamadı." }); return false; }
  },
  forgetPreview: () => {
    draftGeneration += 1;
    set({ preview: null, error: null });
  },
}));

useWorkspace.subscribe((next, previous) => {
  if (next.root !== previous.root) useOwner.getState().resetRoot(next.root);
});
useOwner.getState().resetRoot(useWorkspace.getState().root);
