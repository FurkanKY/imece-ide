/* ui store — kabuk görünürlük durumları + aktif kenar görünümü + panel boyutları
   + ayarlar diyaloğu. Boyut/görünürlük değişimleri oturuma kaydedilir (lib/session.ts). */

import { create } from "zustand";

export type SideView = "explorer" | "search" | "scm" | "agent";

/** panel boyut sınırları (px) */
export const PANEL_LIMITS = {
  sidebar: { min: 180, max: 440, def: 240 },
  aiPanel: { min: 280, max: 560, def: 340 },
  bottom: { min: 120, max: 520, def: 240 },
} as const;

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
  settingsOpen: boolean;
  /** bir splitter sürükleniyor — panel animasyonları geçici kapalı */
  resizing: boolean;
  toggleSidebar: () => void;
  toggleAiPanel: () => void;
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
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
  toggleAiPanel: () => set((s) => ({ aiPanelVisible: !s.aiPanelVisible })),
  toggleBottom: () => set((s) => ({ bottomVisible: !s.bottomVisible })),
  setSideView: (v) => set({ sideView: v }),
  showSideView: (v) => set({ sideView: v, sidebarVisible: true }),
  setSettingsOpen: (open) => set({ settingsOpen: open }),
  setSidebarWidth: (w) =>
    set({ sidebarWidth: clamp(w, PANEL_LIMITS.sidebar.min, PANEL_LIMITS.sidebar.max) }),
  setAiPanelWidth: (w) =>
    set({ aiPanelWidth: clamp(w, PANEL_LIMITS.aiPanel.min, PANEL_LIMITS.aiPanel.max) }),
  setBottomHeight: (h) =>
    set({ bottomHeight: clamp(h, PANEL_LIMITS.bottom.min, PANEL_LIMITS.bottom.max) }),
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
        l.bottomHeight ?? s.bottomHeight, PANEL_LIMITS.bottom.min, PANEL_LIMITS.bottom.max),
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
