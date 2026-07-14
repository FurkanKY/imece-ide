/* scm store — git kaynak denetimi durumu (P4). status yenileme, stage/unstage,
   discard (onaylı), commit; satıra tık → merkez Monaco diff.
   P6: global — proje açılınca/fs.changed'te tazelenir; gezgin dekorasyonları ve
   statusbar dal göstergesi de buradan beslenir. */

import { useMemo } from "react";
import { create } from "zustand";
import { bridge, BridgeError, ScmChange, ScmStatus } from "@/bridge";
import { useEditor } from "@/state/editor";
import { toast } from "@/components/toasts/toasts";
import { confirmDialog } from "@/components/dialogs/dialogs";

/* status harfi → renk + açıklama (ScmView + gezgin dekorasyonları ortak) */
export const SCM_STATUS: Record<string, { color: string; label: string }> = {
  M: { color: "var(--amber)", label: "Değişti" },
  A: { color: "var(--green)", label: "Eklendi" },
  D: { color: "var(--red)", label: "Silindi" },
  R: { color: "var(--accent)", label: "Adlandı" },
  C: { color: "var(--accent)", label: "Kopya" },
  U: { color: "var(--green)", label: "İzlenmiyor" },
};

interface ScmState extends ScmStatus {
  loaded: boolean;
  busy: boolean;
  error: string | null;
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
  error: null,
  message: "",

  setMessage: (m) => set({ message: m }),

  refresh: async () => {
    try {
      const st = await bridge.call("scm.status", {});
      set({ ...st, loaded: true, error: null });
    } catch (e) {
      set({ isRepo: false, staged: [], unstaged: [], loaded: true,
        error: e instanceof BridgeError ? e.message : "Kaynak denetimi okunamadı." });
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
        ? `"${change.path}" izlenmiyor. Dosya silinecek. Emin misin?`
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

let installed = false;

/** Global scm yaşam döngüsü: proje açılınca tazele, fs.changed'te debounce'la tazele.
    App.tsx bir kez çağırır; ScmView/gezgin/statusbar aynı store'dan okur. */
export function installScm() {
  if (installed) return;
  installed = true;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const later = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => void useScm.getState().refresh(), 400);
  };
  bridge.on("fs.changed", later);
  void (async () => {
    const { useWorkspace } = await import("@/state/workspace");
    useWorkspace.subscribe((s, prev) => {
      if (s.root !== prev.root) {
        useScm.setState({
          isRepo: false, branch: "", ahead: 0, behind: 0,
          staged: [], unstaged: [], loaded: false, message: "", error: null,
        });
        if (s.root) void useScm.getState().refresh();
      }
    });
    if (useWorkspace.getState().root) void useScm.getState().refresh();
  })();
}

/** Gezgin dekorasyonları: dosya → durum harfi; değişiklik içeren klasör kümesi.
    Çalışma-ağacı durumu staged'ı ezer (VS Code davranışı). */
export function useScmDecorations() {
  const staged = useScm((s) => s.staged);
  const unstaged = useScm((s) => s.unstaged);
  return useMemo(() => {
    const files = new Map<string, string>();
    for (const c of staged) files.set(c.path, c.status);
    for (const c of unstaged) files.set(c.path, c.status);
    const dirs = new Set<string>();
    for (const p of files.keys()) {
      let d = p;
      for (let i = d.lastIndexOf("/"); i > 0; i = d.lastIndexOf("/")) {
        d = d.slice(0, i);
        dirs.add(d);
      }
    }
    return { files, dirs };
  }, [staged, unstaged]);
}
