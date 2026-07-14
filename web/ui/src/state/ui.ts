/* ui store — kabuk görünürlük durumları + aktif kenar görünümü + panel boyutları
   + ayarlar diyaloğu + zoom/word-wrap (kullanıcı-global, localStorage).
   Boyut/görünürlük değişimleri oturuma kaydedilir (lib/session.ts). */

import { create } from "zustand";
import { bridge } from "@/bridge";

export type SideView = "explorer" | "search" | "scm" | "debug";

/** panel boyut sınırları (px) */
export const PANEL_LIMITS = {
  sidebar: { min: 180, max: 440, def: 240 },
  aiPanel: { min: 280, max: 560, def: 340 },
  bottom: { min: 120, max: 520, def: 240 },
} as const;

/** R5 dar kırılımı: bu genişliğin altında AI paneli overlay drawer, içerik sidebar'ı
    ikon-only'ye çöker (ActivityBar kalır). 1024 üstte inline, 820 altta drawer. */
export const NARROW_BP = 900;

/** Alt panel üst sınırı ekran yüksekliğine göre clamp edilir (R5): görünür alanın
    ~%60'ından fazlasını kaplamaz; kısa ekranda editör/karar boğulmaz. */
export function bottomMax(): number {
  const byViewport = Math.round((typeof window !== "undefined" ? window.innerHeight : 900) * 0.6);
  return Math.min(PANEL_LIMITS.bottom.max, Math.max(PANEL_LIMITS.bottom.min, byViewport));
}

/** oturuma yazılan/oturumdan dönen düzen alanları */
export interface LayoutState {
  sideView: SideView;
  sidebarVisible: boolean;
  aiPanelVisible: boolean;
  bottomVisible: boolean;
  sidebarWidth: number;
  aiPanelWidth: number;
  bottomHeight: number;
}

interface UiState extends LayoutState {
  /** alt panel aktif görünümü: terminal sekmeleri mi F5 ÇIKTI'sı mı (P8.1) */
  bottomView: "terminal" | "output";
  setBottomView: (v: "terminal" | "output") => void;
  /** alt paneli aç (kapalıysa) + görünüme geç */
  showBottom: (v: "terminal" | "output") => void;
  settingsOpen: boolean;
  /** bir splitter sürükleniyor — panel animasyonları geçici kapalı */
  resizing: boolean;
  /** merkez diff görünümü: yan-yana mı inline mı (P6.5) */
  diffSideBySide: boolean;
  toggleDiffSideBySide: () => void;
  /** UI zoom (P6.6, Ctrl+± — native sayfa zoom'u) ve editör satır kaydırma */
  zoom: number;
  setZoom: (z: number) => void;
  wordWrap: boolean;
  toggleWordWrap: () => void;
  toggleSidebar: () => void;
  hideSidebar: () => void;
  toggleAiPanel: () => void;
  showAiPanel: () => void;
  hideAiPanel: () => void;
  /** alt panel yüksekliğini güncel viewport'a göre yeniden clamp et (resize'da) */
  clampToViewport: () => void;
  /** Composer'a odak iste (command center "Görev ver"): AI panelini açar + nonce artırır */
  composerFocusNonce: number;
  focusComposer: () => void;
  toggleBottom: () => void;
  setSideView: (v: SideView) => void;
  /** görünümü aç + kenar çubuğu kapalıysa göster */
  showSideView: (v: SideView) => void;
  setSettingsOpen: (open: boolean) => void;
  setSidebarWidth: (w: number) => void;
  setAiPanelWidth: (w: number) => void;
  setBottomHeight: (h: number) => void;
  setResizing: (on: boolean) => void;
  /** oturumdan düzeni geri yükle (yalnız bilinen/geçerli alanlar) */
  applyLayout: (l: Partial<LayoutState>) => void;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const useUi = create<UiState>((set) => ({
  sidebarVisible: true,
  aiPanelVisible: true,
  bottomVisible: false,
  sideView: "explorer",
  sidebarWidth: PANEL_LIMITS.sidebar.def,
  aiPanelWidth: PANEL_LIMITS.aiPanel.def,
  bottomHeight: PANEL_LIMITS.bottom.def,
  settingsOpen: false,
  resizing: false,
  diffSideBySide: localStorage.getItem("imece.diffSideBySide") === "1",
  toggleDiffSideBySide: () =>
    set((s) => {
      const v = !s.diffSideBySide;
      localStorage.setItem("imece.diffSideBySide", v ? "1" : "0");
      return { diffSideBySide: v };
    }),
  zoom: Number(localStorage.getItem("imece.zoom")) || 1,
  setZoom: (z) => {
    const v = Math.round(clamp(z, 0.7, 1.6) * 10) / 10;
    localStorage.setItem("imece.zoom", String(v));
    void bridge.call("window.setZoom", { factor: v });
    set({ zoom: v });
  },
  wordWrap: localStorage.getItem("imece.wordWrap") === "1",
  toggleWordWrap: () =>
    set((s) => {
      const v = !s.wordWrap;
      localStorage.setItem("imece.wordWrap", v ? "1" : "0");
      return { wordWrap: v };
    }),
  bottomView: "terminal",
  setBottomView: (v) => set({ bottomView: v }),
  showBottom: (v) => set({ bottomView: v, bottomVisible: true }),
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
  hideSidebar: () => set({ sidebarVisible: false }),
  toggleAiPanel: () => set((s) => ({ aiPanelVisible: !s.aiPanelVisible })),
  showAiPanel: () => set({ aiPanelVisible: true }),
  hideAiPanel: () => set({ aiPanelVisible: false }),
  composerFocusNonce: 0,
  focusComposer: () =>
    set((s) => ({ aiPanelVisible: true, composerFocusNonce: s.composerFocusNonce + 1 })),
  toggleBottom: () => set((s) => ({ bottomVisible: !s.bottomVisible })),
  setSideView: (v) => set({ sideView: v }),
  showSideView: (v) => set({ sideView: v, sidebarVisible: true }),
  setSettingsOpen: (open) => set({ settingsOpen: open }),
  setSidebarWidth: (w) =>
    set({ sidebarWidth: clamp(w, PANEL_LIMITS.sidebar.min, PANEL_LIMITS.sidebar.max) }),
  setAiPanelWidth: (w) =>
    set({ aiPanelWidth: clamp(w, PANEL_LIMITS.aiPanel.min, PANEL_LIMITS.aiPanel.max) }),
  setBottomHeight: (h) =>
    set({ bottomHeight: clamp(h, PANEL_LIMITS.bottom.min, bottomMax()) }),
  clampToViewport: () =>
    set((s) => ({ bottomHeight: clamp(s.bottomHeight, PANEL_LIMITS.bottom.min, bottomMax()) })),
  setResizing: (on) => set({ resizing: on }),
  applyLayout: (l) =>
    set((s) => ({
      sideView: l.sideView ?? s.sideView,
      sidebarVisible: l.sidebarVisible ?? s.sidebarVisible,
      aiPanelVisible: l.aiPanelVisible ?? s.aiPanelVisible,
      bottomVisible: l.bottomVisible ?? s.bottomVisible,
      sidebarWidth: clamp(
        l.sidebarWidth ?? s.sidebarWidth, PANEL_LIMITS.sidebar.min, PANEL_LIMITS.sidebar.max),
      aiPanelWidth: clamp(
        l.aiPanelWidth ?? s.aiPanelWidth, PANEL_LIMITS.aiPanel.min, PANEL_LIMITS.aiPanel.max),
      bottomHeight: clamp(
        l.bottomHeight ?? s.bottomHeight, PANEL_LIMITS.bottom.min, bottomMax()),
    })),
}));

/** oturuma yazılacak düzen anlık görüntüsü */
export function layoutSnapshot(): LayoutState {
  const s = useUi.getState();
  return {
    sideView: s.sideView,
    sidebarVisible: s.sidebarVisible,
    aiPanelVisible: s.aiPanelVisible,
    bottomVisible: s.bottomVisible,
    sidebarWidth: s.sidebarWidth,
    aiPanelWidth: s.aiPanelWidth,
    bottomHeight: s.bottomHeight,
  };
}
