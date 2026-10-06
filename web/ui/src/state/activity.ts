/* activity store — F1 (canlı ajan etkinliği). `run.activity` kanalından gelen
   öğeleri koşu başına, sınırlı (son 500) ve yerinde-güncellenen (id bazlı)
   bir listede tutar. run.ts'in install() deseniyle aynı: App mount'ta bir
   kez abone olunur; run.start ile liste sıfırlanır. */

import { create } from "zustand";
import { bridge, ActivityItem } from "@/bridge";
import { useRun, acceptRunActivity } from "@/state/run";

const MAX_ITEMS = 500;

interface ActivityState {
  runId: string | null;
  items: ActivityItem[];
  byRun: Record<string, ActivityItem[]>;
  select: (runId: string | null) => void;
  remove: (runId: string) => void;
  append: (item: ActivityItem) => void;
  reset: (runId: string | null) => void;
  install: () => void;
}

let installed = false;

export const useActivity = create<ActivityState>((set, get) => ({
  runId: null,
  items: [],
  byRun: {},

  select: (runId) => set((s) => ({ runId, items: runId ? (s.byRun[runId] ?? []) : [] })),
  remove: (runId) => set((s) => {
    const { [runId]: _, ...byRun } = s.byRun;
    return { byRun, ...(s.runId === runId ? { runId: null, items: [] } : {}) };
  }),
  // Kept for older callers; selecting a run no longer destroys its history.
  reset: (runId) => set((s) => ({ runId, items: runId ? (s.byRun[runId] ?? []) : [] })),

  append: (item) => {
      if (!useRun.getState().runs[item.runId]) return;
      set((s) => {
        const runItems = s.byRun[item.runId] ?? [];
        const idx = runItems.findIndex((existing) => existing.id === item.id);
        let updated: ActivityItem[];
        if (idx === -1) {
          updated = [...runItems, item];
          if (updated.length > MAX_ITEMS) updated.splice(0, updated.length - MAX_ITEMS);
        } else {
          updated = runItems.slice();
          updated[idx] = item;
        }
        return { byRun: { ...s.byRun, [item.runId]: updated }, ...(s.runId === item.runId ? { items: updated } : {}) };
      });
  },
  install: () => {
    if (installed) return;
    installed = true;
    bridge.on("run.activity", (item) => {
      if (acceptRunActivity(item)) get().append(item);
    });
  },
}));
