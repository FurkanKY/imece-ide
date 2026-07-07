/* ui store — kabuk görünürlük durumları + aktif kenar görünümü + ayarlar diyaloğu. */

import { create } from "zustand";

export type SideView = "explorer" | "search" | "scm" | "agent";

interface UiState {
  sidebarVisible: boolean;
  aiPanelVisible: boolean;
  bottomVisible: boolean;
  sideView: SideView;
  settingsOpen: boolean;
  toggleSidebar: () => void;
  toggleAiPanel: () => void;
  toggleBottom: () => void;
  setSideView: (v: SideView) => void;
  /** görünümü aç + kenar çubuğu kapalıysa göster */
  showSideView: (v: SideView) => void;
  setSettingsOpen: (open: boolean) => void;
}

export const useUi = create<UiState>((set) => ({
  sidebarVisible: true,
  aiPanelVisible: true,
  bottomVisible: false,
  sideView: "explorer",
  settingsOpen: false,
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
  toggleAiPanel: () => set((s) => ({ aiPanelVisible: !s.aiPanelVisible })),
  toggleBottom: () => set((s) => ({ bottomVisible: !s.bottomVisible })),
  setSideView: (v) => set({ sideView: v }),
  showSideView: (v) => set({ sideView: v, sidebarVisible: true }),
  setSettingsOpen: (open) => set({ settingsOpen: open }),
}));
