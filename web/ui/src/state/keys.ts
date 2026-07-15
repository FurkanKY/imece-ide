/* keys store — sağlayıcı kataloğu durumu (anahtar/CLI). Ayarlar'dan yönetilir;
   Composer eksik-anahtar uyarısını buradan okur. Anahtarın kendisi UI'da tutulmaz. */

import { create } from "zustand";
import { bridge, ProviderInfo } from "@/bridge";

interface KeysState {
  providers: Record<string, ProviderInfo>;
  loaded: boolean;
  load: () => Promise<void>;
  save: (v: Record<string, string>) => Promise<void>;
  /** kaydetmeden canlı doğrulama; key verilmezse kayıtlı anahtar denenir */
  test: (provider: string, key?: string) => Promise<{ ok: boolean; code: string; detail: string }>;
  setModel: (provider: string, model: string) => Promise<void>;
  addCustom: (v: { id: string; label: string; baseUrl: string; model: string }) => Promise<void>;
  removeCustom: (provider: string) => Promise<void>;
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

  test: (provider, key) => bridge.call("keys.test", { provider, key }),

  setModel: async (provider, model) => {
    await bridge.call("providers.setModel", { provider, model });
    await get().load();
  },

  addCustom: async (v) => {
    await bridge.call("providers.addCustom", v);
    await get().load();
  },

  removeCustom: async (provider) => {
    await bridge.call("providers.removeCustom", { provider });
    await get().load();
  },
}));
