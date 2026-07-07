/* commands.ts — Ctrl+K komut kataloğu. Store'lara buradan erişilir (döngüsel import yok:
   keymap → commands → store'lar). */

import {
  FolderOpen, Save, FileSearch, FilePlus2, FolderPlus, PanelLeft, PanelRight,
  Search, Settings, TerminalSquare, X,
} from "lucide-react";
import { bridge } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { usePalette, Command } from "@/components/palette/paletteStore";
import { toast } from "@/components/toasts/toasts";

export async function openFilesPalette() {
  const ws = useWorkspace.getState();
  if (!ws.root) {
    toast.info("Önce bir proje klasörü aç.");
    return;
  }
  try {
    const { files } = await bridge.call("project.listFiles", {});
    usePalette.getState().openFiles(files);
  } catch {
    toast.err("Dosya listesi alınamadı.");
  }
}

export function openCommandsPalette() {
  usePalette.getState().openCommands(buildCommands());
}

function buildCommands(): Command[] {
  const ws = useWorkspace.getState();
  const ed = useEditor.getState();
  const ui = useUi.getState();
  const cmds: Command[] = [
    {
      id: "open-folder", label: "Klasör Aç", hint: "proje seç",
      Icon: FolderOpen, run: () => ws.pickAndOpen(),
    },
    {
      id: "goto-file", label: "Dosyaya Git…", hint: "Ctrl+P",
      Icon: FileSearch, run: () => openFilesPalette(),
    },
    {
      id: "global-search", label: "Projede Ara…", hint: "Ctrl+Shift+F",
      Icon: Search,
      run: async () => {
        ui.showSideView("search");
        const { useSearch } = await import("@/state/search");
        useSearch.getState().requestFocus();
      },
    },
    {
      id: "settings", label: "Ayarlar", hint: "accent · yoğunluk · animasyon",
      Icon: Settings, run: () => ui.setSettingsOpen(true),
    },
    {
      id: "toggle-sidebar", label: "Gezgini Aç/Kapat", hint: "Ctrl+B",
      Icon: PanelLeft, run: () => ui.toggleSidebar(),
    },
    {
      id: "toggle-aipanel", label: "AI Panelini Aç/Kapat", hint: "Ctrl+J",
      Icon: PanelRight, run: () => ui.toggleAiPanel(),
    },
    {
      id: "toggle-terminal", label: "Terminal Aç/Kapat", hint: "Ctrl+`",
      Icon: TerminalSquare, run: () => ui.toggleBottom(),
    },
    {
      id: "new-terminal", label: "Yeni Terminal", hint: "Ctrl+Shift+`",
      Icon: TerminalSquare,
      run: async () => {
        if (!ui.bottomVisible) ui.toggleBottom();
        const { useTerminals } = await import("@/state/terminal");
        void useTerminals.getState().create();
      },
    },
  ];
  if (ws.root) {
    cmds.push(
      {
        id: "new-file", label: "Yeni Dosya", hint: "kökte",
        Icon: FilePlus2, run: () => ws.newFile(""),
      },
      {
        id: "new-folder", label: "Yeni Klasör", hint: "kökte",
        Icon: FolderPlus, run: () => ws.newFolder(""),
      },
    );
  }
  if (ed.activeRel) {
    cmds.push(
      {
        id: "save", label: "Kaydet", hint: "Ctrl+S",
        Icon: Save, run: () => ed.saveActive(),
      },
      {
        id: "close-tab", label: "Sekmeyi Kapat", hint: "Ctrl+W",
        Icon: X, run: () => ed.close(ed.activeRel!),
      },
    );
  }
  return cmds;
}
