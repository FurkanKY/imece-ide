/* commands.ts — Ctrl+K komut kataloğu. Store'lara buradan erişilir (döngüsel import yok:
   keymap → commands → store'lar). */

import {
  FolderOpen, GitBranch, Play, Save, FileSearch, FilePlus2, FolderPlus, PanelLeft,
  PanelRight, Search, Settings, SlidersHorizontal, Square, TerminalSquare,
  WrapText, X, ZoomIn, ZoomOut,
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
      id: "scm", label: "Kaynak Denetimi", hint: "Ctrl+Shift+G",
      Icon: GitBranch, run: () => ui.showSideView("scm"),
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
    {
      id: "zoom-in", label: "Yakınlaştır", hint: "Ctrl+=",
      Icon: ZoomIn, run: () => ui.setZoom(ui.zoom + 0.1),
    },
    {
      id: "zoom-out", label: "Uzaklaştır", hint: "Ctrl+-",
      Icon: ZoomOut, run: () => ui.setZoom(ui.zoom - 0.1),
    },
    {
      id: "zoom-reset", label: "Zoom'u Sıfırla", hint: "Ctrl+0",
      Icon: ZoomOut, run: () => ui.setZoom(1),
    },
    {
      id: "word-wrap", label: "Satır Kaydırmayı Aç/Kapat", hint: "Alt+Z",
      Icon: WrapText, run: () => ui.toggleWordWrap(),
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
      // ---- F5 çalıştır (P8.1) ----
      {
        id: "run-project", label: "Çalıştır: Proje", hint: "Ctrl+F5",
        Icon: Play,
        run: async () => {
          const { useExec } = await import("@/state/exec");
          void useExec.getState().run();
        },
      },
      {
        id: "run-stop", label: "Koşuyu Durdur", hint: "Shift+F5",
        Icon: Square,
        run: async () => {
          const { useExec } = await import("@/state/exec");
          void useExec.getState().stop();
        },
      },
      {
        id: "run-config", label: "Çalıştırma Komutunu Değiştir…", hint: ".magent/run.json",
        Icon: SlidersHorizontal,
        run: async () => {
          const { promptDialog } = await import("@/components/dialogs/dialogs");
          const { command } = await bridge.call("exec.getCommand", {});
          const next = await promptDialog({
            title: "Proje çalıştırma komutu",
            message: "Ctrl+F5 bu komutu proje kökünde koşar.",
            initial: command ?? "",
            okLabel: "Kaydet",
            placeholder: 'ör. python "main.py" · npm run dev',
          });
          if (next?.trim()) {
            await bridge.call("exec.setCommand", { command: next.trim() });
            toast.ok("Çalıştırma komutu kaydedildi.");
          }
        },
      },
    );
  }
  if (ed.activeRel) {
    cmds.push(
      {
        id: "run-file", label: "Çalıştır: Aktif Dosya", hint: "F5",
        Icon: Play,
        run: async () => {
          const { useExec } = await import("@/state/exec");
          void useExec.getState().run(useEditor.getState().activeRel);
        },
      },
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
