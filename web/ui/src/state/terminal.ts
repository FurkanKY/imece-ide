/* terminal store — açık terminal sekmeleri. xterm örnekleri bileşende yaşar;
   burada yalnız kimlikler + aktif sekme + yaşam döngüsü. */

import { create } from "zustand";
import { bridge } from "@/bridge";
import { toast } from "@/components/toasts/toasts";

export interface TermTab {
  id: string;
  title: string;
}

interface TerminalState {
  terms: TermTab[];
  activeId: string | null;
  create: () => Promise<void>;
  kill: (id: string) => Promise<void>;
  activate: (id: string) => void;
  /** terminal.exit olayı — sekmeyi düşür */
  onExit: (id: string) => void;
}

let creating = false; // StrictMode/hızlı çift çağrı koruması

export const useTerminals = create<TerminalState>((set, get) => ({
  terms: [],
  activeId: null,

  create: async () => {
    if (creating) return;
    creating = true;
    try {
      const { termId } = await bridge.call("terminal.create", { cols: 120, rows: 30 });
      set((s) => ({
        terms: [...s.terms, { id: termId, title: `powershell ${s.terms.length + 1}` }],
        activeId: termId,
      }));
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Terminal açılamadı.");
    } finally {
      creating = false;
    }
  },

  kill: async (id) => {
    try {
      await bridge.call("terminal.kill", { termId: id });
    } catch {
      // zaten ölmüş olabilir
    }
    get().onExit(id);
  },

  activate: (id) => set({ activeId: id }),

  onExit: (id) =>
    set((s) => {
      const terms = s.terms.filter((t) => t.id !== id);
      return {
        terms,
        activeId: s.activeId === id ? terms[terms.length - 1]?.id ?? null : s.activeId,
      };
    }),
}));
