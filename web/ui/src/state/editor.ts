/* editor store — açık sekmeler, aktif sekme, dirty izleme, kaydetme. */

import { create } from "zustand";
import { bridge } from "@/bridge";

export interface Tab {
  rel: string;
  name: string;
  content: string;      // diskteki (kayıtlı) içerik
  draft: string;        // editördeki güncel içerik
  dirty: boolean;
  tooLarge: boolean;
}

interface EditorState {
  tabs: Tab[];
  activeRel: string | null;
  open: (rel: string) => Promise<void>;
  close: (rel: string) => void;
  activate: (rel: string) => void;
  setDraft: (rel: string, draft: string) => void;
  save: (rel: string) => Promise<void>;
  saveActive: () => Promise<void>;
  /** yeniden adlandırma/taşıma sonrası açık sekmelerin yollarını günceller */
  renamePath: (oldRel: string, newRel: string) => void;
  /** silinen dosya/klasörün sekmelerini kapatır */
  closeDeleted: (rel: string) => void;
  /** merkez diff görünümü (Cursor deseni): öneri diff'i tam boy editörde */
  diff: { path: string; original: string; modified: string } | null;
  openDiff: (path: string) => Promise<void>;
  closeDiff: () => void;
  /** dosyayı aç + satır/sütuna git (arama sonuçları) */
  openAt: (rel: string, line: number, col: number) => Promise<void>;
  pendingReveal: { rel: string; line: number; col: number } | null;
  clearReveal: () => void;
}

export const useEditor = create<EditorState>((set, get) => ({
  tabs: [],
  activeRel: null,

  open: async (rel) => {
    const existing = get().tabs.find((t) => t.rel === rel);
    if (existing) {
      set({ activeRel: rel });
      return;
    }
    const { content, tooLarge } = await bridge.call("fs.readFile", { rel });
    const name = rel.split("/").pop() ?? rel;
    const tab: Tab = { rel, name, content, draft: content, dirty: false, tooLarge: !!tooLarge };
    // yeniden kontrol: eşzamanlı open çağrıları (StrictMode/hızlı tık) çift sekme açmasın
    set((s) =>
      s.tabs.some((t) => t.rel === rel)
        ? { activeRel: rel }
        : { tabs: [...s.tabs, tab], activeRel: rel },
    );
  },

  close: (rel) => {
    set((s) => {
      const tabs = s.tabs.filter((t) => t.rel !== rel);
      let activeRel = s.activeRel;
      if (activeRel === rel) {
        const idx = s.tabs.findIndex((t) => t.rel === rel);
        activeRel = tabs[Math.min(idx, tabs.length - 1)]?.rel ?? null;
      }
      return { tabs, activeRel };
    });
  },

  activate: (rel) => set({ activeRel: rel }),

  setDraft: (rel, draft) =>
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.rel === rel ? { ...t, draft, dirty: draft !== t.content } : t,
      ),
    })),

  save: async (rel) => {
    const tab = get().tabs.find((t) => t.rel === rel);
    if (!tab || !tab.dirty) return;
    await bridge.call("fs.writeFile", { rel, content: tab.draft });
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.rel === rel ? { ...t, content: t.draft, dirty: false } : t,
      ),
    }));
  },

  saveActive: async () => {
    const { activeRel } = get();
    if (activeRel) await get().save(activeRel);
  },

  renamePath: (oldRel, newRel) =>
    set((s) => ({
      // hem dosyanın kendisi hem taşınan klasörün altındakiler
      tabs: s.tabs.map((t) => {
        const moved =
          t.rel === oldRel ? newRel :
          t.rel.startsWith(oldRel + "/") ? newRel + t.rel.slice(oldRel.length) : null;
        return moved
          ? { ...t, rel: moved, name: moved.split("/").pop() ?? moved }
          : t;
      }),
      activeRel:
        s.activeRel === oldRel ? newRel :
        s.activeRel?.startsWith(oldRel + "/")
          ? newRel + s.activeRel.slice(oldRel.length)
          : s.activeRel,
    })),

  closeDeleted: (rel) =>
    set((s) => {
      const gone = (r: string) => r === rel || r.startsWith(rel + "/");
      const tabs = s.tabs.filter((t) => !gone(t.rel));
      const activeRel =
        s.activeRel && gone(s.activeRel)
          ? tabs[tabs.length - 1]?.rel ?? null
          : s.activeRel;
      return { tabs, activeRel };
    }),

  diff: null,

  openDiff: async (path) => {
    const { useRun } = await import("@/state/run");
    const prop = useRun.getState().proposals.find((p) => p.path === path);
    if (!prop) return;
    let original = "";
    if (!prop.is_new) {
      try {
        const r = await bridge.call("fs.readFile", { rel: path });
        original = r.content;
      } catch {
        // yeni dosya gibi davran
      }
    }
    set({ diff: { path, original, modified: prop.new } });
  },

  closeDiff: () => set({ diff: null }),

  pendingReveal: null,

  openAt: async (rel, line, col) => {
    set({ diff: null }); // diff açıksa kapat — dosyaya gidiyoruz
    await get().open(rel);
    set({ pendingReveal: { rel, line, col } });
  },

  clearReveal: () => set({ pendingReveal: null }),
}));
