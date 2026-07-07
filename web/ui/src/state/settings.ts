/* settings store — prefs köprüden yüklenir, değişince köprüye yazılır ve
   <html> data-attribute'ları (accent/density/animations) canlı güncellenir. */

import { create } from "zustand";
import { bridge, Prefs } from "@/bridge";

interface SettingsState {
  prefs: Prefs | null;
  load: () => Promise<void>;
  update: (patch: Partial<Prefs>) => Promise<void>;
}

function applyToDocument(prefs: Prefs) {
  const el = document.documentElement;
  el.dataset.accent = prefs.accent;
  el.dataset.density = prefs.density;
  el.dataset.animations = prefs.animations ? "on" : "off";
}

export const useSettings = create<SettingsState>((set, get) => ({
  prefs: null,
  load: async () => {
    const prefs = await bridge.call("settings.get", {});
    applyToDocument(prefs);
    set({ prefs });
  },
  update: async (patch) => {
    const prefs = { ...get().prefs!, ...patch };
    applyToDocument(prefs);
    set({ prefs });
    await bridge.call("settings.set", prefs);
  },
}));
