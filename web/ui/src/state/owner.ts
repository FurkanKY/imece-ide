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
  startLAN: (params: Api["collab.owner.startLAN"]["params"]) => Promise<boolean>;
  issueInvite: (memberId: string) => Promise<Api["collab.owner.issueInvite"]["result"] | null>;
  revokeMember: (memberId: string) => Promise<boolean>;
  stop: () => Promise<boolean>;
  forgetPreview: () => void;
}

let draftGeneration = 0;
let statusVersion = 0;
let statusRequest: Promise<void> | null = null;

export const useOwner = create<OwnerState>((set, get) => ({
  status: null, preview: null, draftRoot: null, busy: false, error: null,
  resetRoot: (draftRoot) => {
    if (get().draftRoot === draftRoot) return;
    draftGeneration += 1;
    statusVersion += 1;
    set({ draftRoot, preview: null, error: null });
  },
  refresh: () => {
    if (statusRequest) return statusRequest;
    if (get().busy) return Promise.resolve();
    const version = statusVersion;
    statusRequest = bridge.call("collab.owner.status", {}).then((status) => {
      if (version === statusVersion) set({ status, error: get().error });
    }).catch(() => {
      if (version === statusVersion) set({ error: "Sahip oturumu durumu alınamadı." });
    }).finally(() => { statusRequest = null; });
    return statusRequest;
  },
  previewCreate: async (root, params) => {
    if (get().busy || !root || get().draftRoot !== root) return false;
    const token = ++draftGeneration;
    statusVersion += 1;
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
    statusVersion += 1;
    set({ busy: true, error: null, preview: null });
    try { set({ status: await bridge.call("collab.owner.create", { previewId }), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Oturum oluşturulamadı." }); return false; }
  },
  select: async (params) => {
    if (get().busy) return false;
    statusVersion += 1;
    set({ busy: true, error: null, preview: null });
    try { set({ status: await bridge.call("collab.owner.select", params), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Oturum seçilemedi." }); return false; }
  },
  start: async () => {
    if (get().busy) return false;
    statusVersion += 1;
    set({ busy: true, error: null });
    try { set({ status: await bridge.call("collab.owner.start", {}), busy: false }); return true; }
    catch (e) { set({ busy: false, error: e instanceof Error ? e.message : "Sunucu başlatılamadı." }); return false; }
  },
  startLAN: async (params) => {
    if (get().busy || !get().draftRoot || useWorkspace.getState().root !== get().draftRoot) return false;
    const root = get().draftRoot, token = draftGeneration;
    statusVersion += 1;
    set({ busy: true, error: null });
    try {
      const status = await bridge.call("collab.owner.startLAN", params);
      if (token !== draftGeneration || useWorkspace.getState().root !== root) {
        await bridge.call("collab.owner.stop", { expectedEpoch: status.epoch, expectedProjectRoot: status.projectRoot ?? undefined }).catch(() => undefined);
        return false;
      }
      set({ status, busy: false }); return true;
    } catch (e) { if (token === draftGeneration) set({ error: e instanceof Error ? e.message : "TLS sunucusu başlatılamadı." }); return false; }
    finally { if (get().busy) set({ busy: false }); }
  },
  issueInvite: async (memberId) => {
    const root = get().draftRoot, token = draftGeneration;
    const captured = get().status;
    if (!root || get().busy || useWorkspace.getState().root !== root || captured?.projectRoot !== root || captured.transportMode !== "lan" || captured.state !== "running") return null;
    statusVersion += 1;
    set({ busy: true, error: null });
    try {
      const invite = await bridge.call("collab.owner.issueInvite", { memberId, expectedEpoch: captured.epoch });
      const status = useOwner.getState().status;
      if (token !== draftGeneration || useWorkspace.getState().root !== root || status?.epoch !== invite.epoch || status?.projectRoot !== root || status?.state !== "running") {
        await bridge.call("collab.owner.cancelInvite", { code: invite.code, projectRoot: root, expectedEpoch: invite.epoch }).catch(() => undefined);
        return null;
      }
      return invite;
    } catch (e) { if (token === draftGeneration) set({ error: e instanceof Error ? e.message : "Davet oluşturulamadı." }); return null; }
    finally { set({ busy: false }); }
  },
  revokeMember: async (memberId) => {
    const root = get().draftRoot, token = draftGeneration, captured = get().status;
    if (!root || get().busy || useWorkspace.getState().root !== root || captured?.projectRoot !== root || captured.transportMode !== "lan") return false;
    statusVersion += 1;
    set({ busy: true, error: null });
    try {
      const status = await bridge.call("collab.owner.revokeMember", { memberId, expectedEpoch: captured.epoch });
      if (token !== draftGeneration || useWorkspace.getState().root !== root) return false;
      set({ status }); return true;
    } catch (e) { if (token === draftGeneration) set({ error: e instanceof Error ? e.message : "Üye iptal edilemedi." }); return false; }
    finally { set({ busy: false }); }
  },
  stop: async () => {
    if (get().busy) return false;
    set({ busy: true, error: null });
    statusVersion += 1;
    const captured = get().status;
    try { set({ status: await bridge.call("collab.owner.stop", { expectedEpoch: captured?.epoch, expectedProjectRoot: captured?.projectRoot ?? undefined }), busy: false }); return true; }
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
