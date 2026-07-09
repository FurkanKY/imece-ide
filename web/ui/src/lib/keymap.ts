/* keymap.ts — TEK kısayol kaydı (kısayol sahipliği %100 web tarafında, plan risk 3).
   React ağacının dışında window'a bağlanır; handler'lar store'ları çağırır. */

import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { usePalette } from "@/components/palette/paletteStore";
import { openFilesPalette, openCommandsPalette } from "@/lib/commands";

type Handler = (e: KeyboardEvent) => void;

function combo(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.ctrlKey || e.metaKey) parts.push("mod");
  if (e.shiftKey) parts.push("shift");
  if (e.altKey) parts.push("alt");
  // backquote: shift'li kombinasyonlarda e.key düzene göre değişir ("~") → code kullan
  parts.push(e.code === "Backquote" ? "`" : e.key.toLowerCase());
  return parts.join("+");
}

const MAP: Record<string, Handler> = {
  "mod+s": (e) => {
    e.preventDefault();
    void useEditor.getState().saveActive();
  },
  "mod+w": (e) => {
    e.preventDefault();
    const { activeRel, close } = useEditor.getState();
    if (activeRel) close(activeRel);
  },
  "mod+p": (e) => {
    e.preventDefault();
    void openFilesPalette();
  },
  "mod+k": (e) => {
    e.preventDefault();
    // palet açıksa kapat (toggle hissi)
    const p = usePalette.getState();
    if (p.open) p.close();
    else openCommandsPalette();
  },
  "mod+b": (e) => {
    e.preventDefault();
    useUi.getState().toggleSidebar();
  },
  "mod+j": (e) => {
    e.preventDefault();
    useUi.getState().toggleAiPanel();
  },
  "mod+`": (e) => {
    e.preventDefault();
    useUi.getState().toggleBottom();
  },
  "mod+shift+`": async (e) => {
    e.preventDefault();
    if (!useUi.getState().bottomVisible) useUi.getState().toggleBottom();
    const { useTerminals } = await import("@/state/terminal");
    void useTerminals.getState().create();
  },
  "mod+shift+f": async (e) => {
    e.preventDefault();
    useUi.getState().showSideView("search");
    const { useSearch } = await import("@/state/search");
    useSearch.getState().requestFocus();
  },
  "mod+shift+g": (e) => {
    e.preventDefault();
    useUi.getState().showSideView("scm"); // VS Code standardı
  },
  // ---- UI zoom (P6.6): Ctrl+± / Ctrl+0 — native sayfa zoom'u ----
  "mod+=": (e) => {
    e.preventDefault();
    const ui = useUi.getState();
    ui.setZoom(ui.zoom + 0.1);
  },
  "mod++": (e) => {
    e.preventDefault();
    const ui = useUi.getState();
    ui.setZoom(ui.zoom + 0.1);
  },
  "mod+-": (e) => {
    e.preventDefault();
    const ui = useUi.getState();
    ui.setZoom(ui.zoom - 0.1);
  },
  "mod+0": (e) => {
    e.preventDefault();
    useUi.getState().setZoom(1);
  },
  // ---- editör satır kaydırma (VS Code: Alt+Z) ----
  "alt+z": (e) => {
    e.preventDefault();
    useUi.getState().toggleWordWrap();
  },
  // ---- Koş & Debug (P8.1 + P8.2, VS Code düzeni) ----
  // F5: debug oturumu varsa DEVAM; .py aktifse DEBUG başlat; değilse exec koş.
  "f5": async (e) => {
    e.preventDefault();
    const { useDebug } = await import("@/state/debug");
    const dbg = useDebug.getState();
    if (dbg.status === "stopped") {
      dbg.cont();
      return;
    }
    if (dbg.status === "running" || dbg.status === "starting") return; // koşuyor — bekle
    const rel = useEditor.getState().activeRel;
    if (rel?.endsWith(".py")) {
      void dbg.start(rel);
      return;
    }
    const { useExec } = await import("@/state/exec");
    void useExec.getState().run(rel);
  },
  // Ctrl+F5: debugsuz koş (proje komutu — P8.1 davranışı korunur)
  "mod+f5": async (e) => {
    e.preventDefault();
    const { useExec } = await import("@/state/exec");
    void useExec.getState().run();
  },
  // Shift+F5: debug oturumu varsa onu, yoksa exec koşusunu durdur
  "shift+f5": async (e) => {
    e.preventDefault();
    const { useDebug } = await import("@/state/debug");
    if (useDebug.getState().status !== "idle") {
      void useDebug.getState().stop();
      return;
    }
    const { useExec } = await import("@/state/exec");
    void useExec.getState().stop();
  },
  // F9: imleç satırında breakpoint aç/kapat
  "f9": async (e) => {
    e.preventDefault();
    const rel = useEditor.getState().activeRel;
    if (!rel) return;
    const { getActiveCodeEditor } = await import("@/components/editor/Editor");
    const line = getActiveCodeEditor()?.getPosition()?.lineNumber;
    if (!line) return;
    const { useDebug } = await import("@/state/debug");
    useDebug.getState().toggleBreakpoint(rel, line);
  },
  // F10 / F11 / Shift+F11: adımlama (yalnız durmuşken)
  "f10": async (e) => {
    e.preventDefault();
    const { useDebug } = await import("@/state/debug");
    if (useDebug.getState().status === "stopped") useDebug.getState().next();
  },
  "f11": async (e) => {
    e.preventDefault();
    const { useDebug } = await import("@/state/debug");
    if (useDebug.getState().status === "stopped") useDebug.getState().stepIn();
  },
  "shift+f11": async (e) => {
    e.preventDefault();
    const { useDebug } = await import("@/state/debug");
    if (useDebug.getState().status === "stopped") useDebug.getState().stepOut();
  },
};

let bound = false;

export function installKeymap() {
  if (bound) return;
  bound = true;
  window.addEventListener(
    "keydown",
    (e) => {
      const h = MAP[combo(e)];
      if (h) h(e);
    },
    { capture: true },
  );
}
