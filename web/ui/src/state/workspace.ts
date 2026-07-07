/* workspace store — açık proje + gezgin ağaç durumu (lazy genişletme) + dosya işlemleri. */

import { create } from "zustand";
import { bridge, BridgeError, DirEntry } from "@/bridge";
import { useEditor } from "@/state/editor";
import { toast } from "@/components/toasts/toasts";
import { confirmDialog, promptDialog } from "@/components/dialogs/dialogs";

function parentOf(rel: string): string {
  const i = rel.lastIndexOf("/");
  return i >= 0 ? rel.slice(0, i) : "";
}

function validName(v: string): string | null {
  if (!v.trim()) return "Ad boş olamaz.";
  if (/[/\\:*?"<>|]/.test(v)) return 'Geçersiz karakter: / \\ : * ? " < > |';
  return null;
}

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
  // dosya işlemleri (diyalog + köprü + tazeleme uçtan uca)
  newFile: (dirRel: string) => Promise<void>;
  newFolder: (dirRel: string) => Promise<void>;
  renameEntry: (entry: DirEntry) => Promise<void>;
  deleteEntry: (entry: DirEntry) => Promise<void>;
  copyPath: (rel: string) => Promise<void>;
  revealInOS: (rel: string) => Promise<void>;
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
    // önceki oturumun sekmelerini geri yükle
    const { restoreSession } = await import("@/lib/session");
    await restoreSession();
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

  // ---------------- dosya işlemleri ----------------

  newFile: async (dirRel) => {
    const name = await promptDialog({
      title: "Yeni Dosya",
      message: dirRel ? `Konum: ${dirRel}/` : "Konum: proje kökü",
      okLabel: "Oluştur",
      placeholder: "dosya-adi.py",
      validate: validName,
    });
    if (name === null) return;
    const rel = dirRel ? `${dirRel}/${name}` : name;
    try {
      await bridge.call("fs.createFile", { rel });
      await get().loadDir(dirRel);
      await useEditor.getState().open(rel); // yeni dosya editörde açılır (eski davranış)
      toast.ok(`Oluşturuldu: ${rel}`);
    } catch (e) {
      toast.err(e instanceof BridgeError ? e.message : "Dosya oluşturulamadı.");
    }
  },

  newFolder: async (dirRel) => {
    const name = await promptDialog({
      title: "Yeni Klasör",
      message: dirRel ? `Konum: ${dirRel}/` : "Konum: proje kökü",
      okLabel: "Oluştur",
      placeholder: "klasor-adi",
      validate: validName,
    });
    if (name === null) return;
    const rel = dirRel ? `${dirRel}/${name}` : name;
    try {
      await bridge.call("fs.createFolder", { rel });
      await get().loadDir(dirRel);
      toast.ok(`Oluşturuldu: ${rel}/`);
    } catch (e) {
      toast.err(e instanceof BridgeError ? e.message : "Klasör oluşturulamadı.");
    }
  },

  renameEntry: async (entry) => {
    const newName = await promptDialog({
      title: "Yeniden Adlandır",
      okLabel: "Adlandır",
      initial: entry.name,
      validate: validName,
    });
    if (newName === null || newName === entry.name) return;
    try {
      const { rel: newRel } = await bridge.call("fs.rename", {
        rel: entry.rel,
        newName,
      });
      useEditor.getState().renamePath(entry.rel, newRel);
      // genişletilmiş klasör taşındıysa anahtarları da taşı
      set((s) => {
        const expanded = new Set(
          [...s.expanded].map((r) =>
            r === entry.rel ? newRel :
            r.startsWith(entry.rel + "/") ? newRel + r.slice(entry.rel.length) : r,
          ),
        );
        const children: Record<string, DirEntry[]> = {};
        for (const [k, v] of Object.entries(s.children)) {
          const nk =
            k === entry.rel ? newRel :
            k.startsWith(entry.rel + "/") ? newRel + k.slice(entry.rel.length) : k;
          children[nk] = v;
        }
        return { expanded, children };
      });
      await get().loadDir(parentOf(entry.rel));
      toast.ok(`Yeniden adlandırıldı: ${newRel}`);
    } catch (e) {
      toast.err(e instanceof BridgeError ? e.message : "Yeniden adlandırılamadı.");
    }
  },

  deleteEntry: async (entry) => {
    const sure = await confirmDialog({
      title: "Sil",
      message: entry.isDir
        ? `"${entry.name}" klasörü ve TÜM içeriği silinecek. Emin misin?`
        : `"${entry.name}" silinecek. Emin misin?`,
      okLabel: "Sil",
      danger: true,
    });
    if (!sure) return;
    try {
      await bridge.call("fs.delete", { rel: entry.rel });
      useEditor.getState().closeDeleted(entry.rel);
      set((s) => {
        const expanded = new Set(
          [...s.expanded].filter((r) => r !== entry.rel && !r.startsWith(entry.rel + "/")),
        );
        const children = { ...s.children };
        for (const k of Object.keys(children)) {
          if (k === entry.rel || k.startsWith(entry.rel + "/")) delete children[k];
        }
        return { expanded, children };
      });
      await get().loadDir(parentOf(entry.rel));
      toast.ok(`Silindi: ${entry.rel}`);
    } catch (e) {
      toast.err(e instanceof BridgeError ? e.message : "Silinemedi.");
    }
  },

  copyPath: async (rel) => {
    try {
      await navigator.clipboard.writeText(rel);
    } catch {
      await bridge.call("app.clipboardWrite", { text: rel });
    }
    toast.info(`Yol kopyalandı: ${rel}`);
  },

  revealInOS: async (rel) => {
    const { root } = get();
    if (!root) return;
    try {
      await bridge.call("app.revealInOS", { path: root + "/" + rel });
    } catch (e) {
      toast.err(e instanceof BridgeError ? e.message : "Sistemde gösterilemedi.");
    }
  },
}));
