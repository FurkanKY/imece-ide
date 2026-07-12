/* keys store — API anahtarı durumu (beta onboarding). Ayarlar'dan kaydedilir;
   Composer eksik-anahtar uyarısını buradan okur. Anahtarın kendisi UI'da tutulmaz. */

import { create } from "zustand";
import { bridge } from "@/bridge";

export interface ProviderKeyStatus {
  ok: boolean;
  masked?: string;
  detail?: string;
}

interface KeysState {
  providers: Record<string, ProviderKeyStatus>;
  loaded: boolean;
  load: () => Promise<void>;
  save: (v: { deepseek?: string; gemini?: string }) => Promise<void>;
}

export const useKeys = create<KeysState>((set, get) => ({
  providers: {},
  loaded: false,

  load: async () => {
    try {
      const { providers } = await bridge.call("keys.status", {});
      set({ providers, loaded: true });
    } catch {
      set({ loaded: true }); // durum alınamadı — uyarı gösterme, koşu hatası yakalar
    }
  },

  save: async (v) => {
    await bridge.call("keys.set", v);
    await get().load();
  },
}));
