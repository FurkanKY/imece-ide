/* ui store — kabuk görünürlük durumları (kenar çubuğu; P2'de AI paneli, P3'te alt panel). */

import { create } from "zustand";

interface UiState {
  sidebarVisible: boolean;
  aiPanelVisible: boolean;
  bottomVisible: boolean;
  toggleSidebar: () => void;
  toggleAiPanel: () => void;
  toggleBottom: () => void;
}

export const useUi = create<UiState>((set) => ({
  sidebarVisible: true,
  aiPanelVisible: true,
  bottomVisible: false,
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
  toggleAiPanel: () => set((s) => ({ aiPanelVisible: !s.aiPanelVisible })),
  toggleBottom: () => set((s) => ({ bottomVisible: !s.bottomVisible })),
}));
