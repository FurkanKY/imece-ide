/* scm store — git kaynak denetimi durumu (P4). status yenileme, stage/unstage,
   discard (onaylı), commit; satıra tık → merkez Monaco diff. */

import { create } from "zustand";
import { bridge, BridgeError, ScmChange, ScmStatus } from "@/bridge";
import { useEditor } from "@/state/editor";
import { toast } from "@/components/toasts/toasts";
import { confirmDialog } from "@/components/dialogs/dialogs";

interface ScmState extends ScmStatus {
  loaded: boolean;
  busy: boolean;
  message: string;
  setMessage: (m: string) => void;
  refresh: () => Promise<void>;
  stage: (paths: string[]) => Promise<void>;
  unstage: (paths: string[]) => Promise<void>;
  discard: (change: ScmChange) => Promise<void>;
  commit: () => Promise<void>;
  openDiff: (change: ScmChange, staged: boolean) => Promise<void>;
}

const err = (e: unknown, fallback: string) =>
  toast.err(e instanceof BridgeError ? e.message : fallback);

export const useScm = create<ScmState>((set, get) => ({
  isRepo: false,
  branch: "",
  ahead: 0,
  behind: 0,
  staged: [],
  unstaged: [],
  loaded: false,
  busy: false,
  message: "",

  setMessage: (m) => set({ message: m }),

  refresh: async () => {
    try {
      const st = await bridge.call("scm.status", {});
      set({ ...st, loaded: true });
    } catch {
      set({ isRepo: false, staged: [], unstaged: [], loaded: true });
    }
  },

  stage: async (paths) => {
    try {
      await bridge.call("scm.stage", { paths });
      await get().refresh();
    } catch (e) {
      err(e, "Hazırlanamadı (stage).");
    }
  },

  unstage: async (paths) => {
    try {
      await bridge.call("scm.unstage", { paths });
      await get().refresh();
    } catch (e) {
      err(e, "Hazırlıktan çıkarılamadı (unstage).");
    }
  },

  discard: async (change) => {
    const untracked = change.status === "U";
    const ok = await confirmDialog({
      title: "Değişikliği At",
      message: untracked
        ? `"${change.path}" izlenmiyor — dosya SİLİNECEK. Emin misin?`
        : `"${change.path}" üzerindeki değişiklikler geri alınacak. Emin misin?`,
      okLabel: untracked ? "Sil" : "Değişikliği At",
      danger: true,
    });
    if (!ok) return;
    try {
      await bridge.call("scm.discard", { path: change.path, untracked });
      // açık sekme varsa diskten tazele/kapat
      const ed = useEditor.getState();
      if (untracked) ed.closeDeleted(change.path);
      else if (ed.tabs.some((t) => t.rel === change.path)) {
        ed.close(change.path);
        void ed.open(change.path);
      }
      await get().refresh();
      toast.ok(`Atıldı: ${change.path}`);
    } catch (e) {
      err(e, "Değişiklik atılamadı.");
    }
  },

  commit: async () => {
    const { message, staged } = get();
    if (!message.trim() || staged.length === 0) return;
    set({ busy: true });
    try {
      const { summary } = await bridge.call("scm.commit", { message: message.trim() });
      set({ message: "" });
      await get().refresh();
      toast.ok(summary);
    } catch (e) {
      err(e, "Commit başarısız.");
    } finally {
      set({ busy: false });
    }
  },

  openDiff: async (change, staged) => {
    try {
      const { original, modified } = await bridge.call("scm.diff", {
        path: change.path,
        staged,
      });
      useEditor.getState().showDiff(change.path, original, modified);
    } catch (e) {
      err(e, "Diff alınamadı.");
    }
  },
}));
