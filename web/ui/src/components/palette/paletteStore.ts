/* palette.ts — palet store'u: mod (files|commands) + komut kaydı.
   Komutlar tek listede; palet fuzzy filtreler. Eski command_palette.py'nin halefi. */

import { create } from "zustand";
import type { LucideIcon } from "lucide-react";

export interface Command {
  id: string;
  label: string;
  hint?: string;      // kısayol vb.
  Icon?: LucideIcon;
  run: () => void | Promise<void>;
}

export type PaletteMode = "files" | "commands";

interface PaletteState {
  open: boolean;
  mode: PaletteMode;
  files: string[];        // files modunda doldurulur
  commands: Command[];
  openFiles: (files: string[]) => void;
  openCommands: (commands: Command[]) => void;
  close: () => void;
}

export const usePalette = create<PaletteState>((set) => ({
  open: false,
  mode: "files",
  files: [],
  commands: [],
  openFiles: (files) => set({ open: true, mode: "files", files }),
  openCommands: (commands) => set({ open: true, mode: "commands", commands }),
  close: () => set({ open: false }),
}));
