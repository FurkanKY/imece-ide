/* activity store — F1 (canlı ajan etkinliği). `run.activity` kanalından gelen
   öğeleri koşu başına, sınırlı (son 500) ve yerinde-güncellenen (id bazlı)
   bir listede tutar. run.ts'in install() deseniyle aynı: App mount'ta bir
   kez abone olunur; run.start ile liste sıfırlanır. */

import { create } from "zustand";
import { bridge, ActivityItem } from "@/bridge";

const MAX_ITEMS = 500;

interface ActivityState {
  runId: string | null;
  items: ActivityItem[];
  reset: (runId: string | null) => void;
  install: () => void;
}

let installed = false;

export const useActivity = create<ActivityState>((set) => ({
  runId: null,
  items: [],

  reset: (runId) => set({ runId, items: [] }),

  install: () => {
    if (installed) return;
    installed = true;
    bridge.on("run.activity", (item) => {
      set((s) => {
        // Farklı bir koşunun geç gelen bir öğesi (ör. eski akış hâlâ
        // tahliye ediliyorken yeni koşu başlamış) sessizce yok sayılır.
        if (s.runId !== null && item.runId !== s.runId) return s;
        const idx = s.items.findIndex((existing) => existing.id === item.id);
        if (idx === -1) {
          const items = [...s.items, item];
          if (items.length > MAX_ITEMS) items.splice(0, items.length - MAX_ITEMS);
          return { items };
        }
        const items = s.items.slice();
        items[idx] = item;
        return { items };
      });
    });
  },
}));
