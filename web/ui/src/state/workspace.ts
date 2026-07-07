/* workspace store — açık proje + gezgin ağaç durumu (lazy genişletme). */

import { create } from "zustand";
import { bridge, DirEntry } from "@/bridge";

interface WorkspaceState {
  root: string | null;
  name: string | null;
  /** rel → çocuk girdileri (yüklenmiş klasörler) */
  children: Record<string, DirEntry[]>;
  expanded: Set<string>;
  loading: Set<string>;
  openProject: (path: string) => Promise<void>;
  pickAndOpen: () => Promise<void>;
  toggleDir: (rel: string) => Promise<void>;
  loadDir: (rel: string) => Promise<void>;
}

export const useWorkspace = create<WorkspaceState>((set, get) => ({
  root: null,
  name: null,
  children: {},
  expanded: new Set(),
  loading: new Set(),

  openProject: async (path) => {
    const { root, name } = await bridge.call("project.open", { path });
    set({ root, name, children: {}, expanded: new Set(), loading: new Set() });
    await get().loadDir("");
  },

  pickAndOpen: async () => {
    const { path } = await bridge.call("app.pickFolder", {});
    if (path) await get().openProject(path);
  },

  loadDir: async (rel) => {
    set((s) => ({ loading: new Set(s.loading).add(rel) }));
    try {
      const { entries } = await bridge.call("fs.listDir", { rel });
      set((s) => {
        const loading = new Set(s.loading);
        loading.delete(rel);
        return { children: { ...s.children, [rel]: entries }, loading };
      });
    } catch {
      set((s) => {
        const loading = new Set(s.loading);
        loading.delete(rel);
        return { loading };
      });
    }
  },

  toggleDir: async (rel) => {
    const { expanded, children } = get();
    const next = new Set(expanded);
    if (next.has(rel)) {
      next.delete(rel);
      set({ expanded: next });
    } else {
      next.add(rel);
      set({ expanded: next });
      if (!children[rel]) await get().loadDir(rel);
    }
  },
}));
