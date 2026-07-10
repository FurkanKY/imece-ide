/* commands.ts — Ctrl+K komut kataloğu. Store'lara buradan erişilir (döngüsel import yok:
   keymap → commands → store'lar). */

import {
  Bug, CircleDot, FolderOpen, GitBranch, Play, Save, FileSearch, FilePlus2,
  FolderPlus, PanelLeft, PanelRight, Search, Settings, SlidersHorizontal, Square,
  TerminalSquare, WrapText, X, ZoomIn, ZoomOut,
  Sparkles, Wand2, MessageSquareCode, FileDiff, History, FlaskConical, Send,
} from "lucide-react";
import { bridge } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { useRun } from "@/state/run";
import { useExec } from "@/state/exec";
import { usePalette, Command } from "@/components/palette/paletteStore";
import { toast } from "@/components/toasts/toasts";
import {
  giveTask, inlineEditSelection, explainSelection, sendOutputErrorToTeam,
  reviewChanges, restoreLastCheckpoint, runTests,
} from "@/lib/aiActions";

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
  usePalette.getState().openCommands([...buildAiCommands(), ...buildCommands()]);
}

/* AI-native command center (R4): görev/kod/hata/inceleme/checkpoint/test eylemleri.
   Bağlama göre görünür; hepsi aiActions üzerinden aynı run lifecycle'ına düşer. */
function buildAiCommands(): Command[] {
  const ws = useWorkspace.getState();
  const ed = useEditor.getState();
  const run = useRun.getState();
  const ex = useExec.getState();
  const cmds: Command[] = [
    {
      id: "ai-task", label: "AI: Ekibe görev ver", hint: "composer",
      Icon: Sparkles, run: () => giveTask(),
    },
  ];
  if (ed.activeRel) {
    cmds.push(
      {
        id: "ai-edit-selection", label: "AI: Seçili kodu düzenle", hint: "Ctrl+K (seçim)",
        Icon: Wand2, run: () => inlineEditSelection(),
      },
      {
        id: "ai-explain-selection", label: "AI: Seçili kodu açıkla", hint: "seçili satırlar",
        Icon: MessageSquareCode, run: () => explainSelection(),
      },
    );
  }
  if (ex.raw.trim()) {
    cmds.push({
      id: "ai-error-to-team", label: "AI: Çıktı hatasını ekibe gönder", hint: "ÇIKTI → ekip",
      Icon: Send, run: () => sendOutputErrorToTeam(),
    });
  }
  if (run.diffs.length || run.proposals.length) {
    cmds.push({
      id: "ai-review", label: "AI: Değişiklikleri incele", hint: `${run.diffs.length || run.proposals.length} dosya`,
      Icon: FileDiff, run: () => reviewChanges(),
    });
  }
  if (run.checkpointId) {
    cmds.push({
      id: "ai-restore", label: "AI: Checkpoint'e dön", hint: "son apply",
      Icon: History, run: () => restoreLastCheckpoint(),
    });
  }
  if (ws.root) {
    cmds.push({
      id: "ai-run-tests", label: "AI: Testleri çalıştır", hint: "ÇIKTI",
      Icon: FlaskConical, run: () => runTests(),
    });
  }
  return cmds;
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
  if (ws.root) {
    cmds.push(
      // ---- debug (P8.2) ----
      {
        id: "debug-view", label: "Çalıştır ve Debug Görünümü", hint: "kenar çubuğu",
        Icon: Bug, run: () => ui.showSideView("debug"),
      },
      {
        id: "debug-start", label: "Debug: Başlat", hint: "F5 (.py)",
        Icon: Bug,
        run: async () => {
          const { useDebug } = await import("@/state/debug");
          void useDebug.getState().start(useEditor.getState().activeRel);
        },
      },
      {
        id: "debug-stop", label: "Debug: Durdur", hint: "Shift+F5",
        Icon: Square,
        run: async () => {
          const { useDebug } = await import("@/state/debug");
          void useDebug.getState().stop();
        },
      },
    );
  }
  if (ed.activeRel) {
    cmds.push(
      {
        id: "toggle-bp", label: "Breakpoint Ekle/Kaldır", hint: "F9",
        Icon: CircleDot,
        run: async () => {
          const rel = useEditor.getState().activeRel;
          if (!rel) return;
          const { getActiveCodeEditor } = await import("@/components/editor/Editor");
          const line = getActiveCodeEditor()?.getPosition()?.lineNumber;
          if (!line) return;
          const { useDebug } = await import("@/state/debug");
          useDebug.getState().toggleBreakpoint(rel, line);
        },
      },
      {
        id: "run-file", label: "Çalıştır: Aktif Dosya (debugsuz)", hint: "▶",
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
