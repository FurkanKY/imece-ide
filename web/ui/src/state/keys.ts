/* keys store — sağlayıcı kataloğu durumu (anahtar/CLI) + Jev karar sağlayıcısı.
   Ayarlar'dan yönetilir; Composer eksik-anahtar uyarısını buradan okur.
   Anahtarın kendisi UI'da tutulmaz. Jev (typesafe) ayrı bir karar sağlayıcısıdır:
   decisionProviders sonucunda gelir, providers kataloğuna/yönlendirmeye karışmaz
   (bkz. decision_credentials.py, keys.status). */

import { create } from "zustand";
import { bridge, DecisionProviderInfo, ProviderInfo } from "@/bridge";

interface KeysState {
  providers: Record<string, ProviderInfo>;
  /** Jev (TypeSafe) — keys.status'un decisionProviders alanı (ayrı arayüz). */
  decisionProviders: Record<string, DecisionProviderInfo>;
  loaded: boolean;
  load: () => Promise<void>;
  save: (v: Record<string, string>) => Promise<void>;
  /** kaydetmeden canlı doğrulama; key verilmezse kayıtlı anahtar denenir.
      Jev (typesafe) için salt bağlantı testi isteği gönderir — proje içeriği yok. */
  test: (provider: string, key?: string) => Promise<{ ok: boolean; code: string; detail: string }>;
  /** Jev anahtarını kaydeder (keys.set typesafe → TYPESAFE_API_KEY); doğrulama
      arka uçta yapılır, kayıt sırasında ağ çağrısı YOKTUR. */
  saveDecision: (key: string) => Promise<void>;
  setModel: (provider: string, model: string) => Promise<void>;
  addCustom: (v: { id: string; label: string; baseUrl: string; model: string }) => Promise<void>;
  removeCustom: (provider: string) => Promise<void>;
}

export const useKeys = create<KeysState>((set, get) => ({
  providers: {},
  decisionProviders: {},
  loaded: false,

  load: async () => {
    try {
      const { providers, decisionProviders } = await bridge.call("keys.status", {});
      set({ providers, decisionProviders, loaded: true });
    } catch {
      set({ loaded: true }); // durum alınamadı — uyarı gösterme, koşu hatası yakalar
    }
  },

  save: async (v) => {
    await bridge.call("keys.set", v);
    await get().load();
  },

  test: (provider, key) => bridge.call("keys.test", { provider, key }),

  saveDecision: async (key) => {
    await bridge.call("keys.set", { typesafe: key });
    await get().load();
  },

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
