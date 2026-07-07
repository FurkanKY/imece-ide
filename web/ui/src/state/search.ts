/* search store — global arama durumu: sorgu + seçenekler + dosyaya gruplu sonuçlar. */

import { create } from "zustand";
import { bridge, BridgeError, SearchMatch } from "@/bridge";
import { toast } from "@/components/toasts/toasts";

interface SearchState {
  query: string;
  regex: boolean;
  caseSensitive: boolean;
  searching: boolean;
  searchId: string | null;
  matches: SearchMatch[];
  total: number;
  limitHit: boolean;
  /** arama girişine odak isteği (Ctrl+Shift+F) — nonce */
  focusNonce: number;
  setQuery: (q: string) => void;
  toggleRegex: () => void;
  toggleCase: () => void;
  requestFocus: () => void;
  start: () => Promise<void>;
  cancel: () => Promise<void>;
  install: () => void;
}

let installed = false;

export const useSearch = create<SearchState>((set, get) => ({
  query: "",
  regex: false,
  caseSensitive: false,
  searching: false,
  searchId: null,
  matches: [],
  total: 0,
  limitHit: false,
  focusNonce: 0,

  setQuery: (q) => set({ query: q }),
  toggleRegex: () => set((s) => ({ regex: !s.regex })),
  toggleCase: () => set((s) => ({ caseSensitive: !s.caseSensitive })),
  requestFocus: () => set((s) => ({ focusNonce: s.focusNonce + 1 })),

  start: async () => {
    const { query, regex, caseSensitive } = get();
    if (query.trim().length < 2) return;
    set({ searching: true, matches: [], total: 0, limitHit: false });
    try {
      const { searchId } = await bridge.call("search.start", {
        query: query.trim(),
        regex,
        caseSensitive,
      });
      set({ searchId });
    } catch (e) {
      set({ searching: false });
      toast.err(e instanceof BridgeError ? e.message : "Arama başlatılamadı.");
    }
  },

  cancel: async () => {
    await bridge.call("search.cancel", {});
    set({ searching: false });
  },

  install: () => {
    if (installed) return;
    installed = true;
    bridge.on("search.results", ({ searchId, matches }) => {
      if (searchId !== get().searchId) return; // bayat arama
      set((s) => ({ matches: [...s.matches, ...matches] }));
    });
    bridge.on("search.done", ({ searchId, total, limitHit }) => {
      if (searchId !== get().searchId) return;
      set({ searching: false, total, limitHit });
    });
  },
}));
