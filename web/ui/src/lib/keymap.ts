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
  parts.push(e.key.toLowerCase());
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
