/* ui store — kabuk görünürlük durumları (kenar çubuğu; P2'de AI paneli, P3'te alt panel). */

import { create } from "zustand";

interface UiState {
  sidebarVisible: boolean;
  toggleSidebar: () => void;
}

export const useUi = create<UiState>((set) => ({
  sidebarVisible: true,
  toggleSidebar: () => set((s) => ({ sidebarVisible: !s.sidebarVisible })),
}));
