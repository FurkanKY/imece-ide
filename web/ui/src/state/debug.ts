/* debug store — P8.2 (debugpy/DAP): breakpoint'ler + oturum durumu + yığın.
   Breakpoint'ler proje köküne göre localStorage'da kalıcı (magent.bp.<root>).
   Program çıktısı ÇIKTI sekmesine akar (exec store'un dış-koşu kanalı). */

import { create } from "zustand";
import { bridge } from "@/bridge";
import type { DebugFrame, DebugScope, DebugVariable } from "@/bridge/protocol";
import { toast } from "@/components/toasts/toasts";
import { useUi } from "@/state/ui";
import { useEditor } from "@/state/editor";
import { useWorkspace } from "@/state/workspace";
import { beginExternalRun, feedExternalOutput, endExternalRun } from "@/state/exec";

export type DebugStatus = "idle" | "starting" | "running" | "stopped";

function bpKey(root: string) {
  return "magent.bp." + root;
}

function loadBp(root: string | null): Record<string, number[]> {
  if (!root) return {};
  try {
    return JSON.parse(localStorage.getItem(bpKey(root)) ?? "{}");
  } catch {
    return {};
  }
}

function saveBp(bp: Record<string, number[]>) {
  const root = useWorkspace.getState().root;
  if (root) localStorage.setItem(bpKey(root), JSON.stringify(bp));
}

interface DebugState {
  status: DebugStatus;
  /** rel → sıralı satır listesi */
  breakpoints: Record<string, number[]>;
  stoppedAt: { path: string; line: number; reason: string } | null;
  frames: DebugFrame[];
  /** debug edilen dosya (başlıkta gösterim) */
  file: string | null;
  install: () => void;
  toggleBreakpoint: (rel: string, line: number) => void;
  removeBreakpoint: (rel: string, line: number) => void;
  clearBreakpoints: () => void;
  start: (rel?: string | null) => Promise<void>;
  cont: () => void;
  next: () => void;
  stepIn: () => void;
  stepOut: () => void;
  stop: () => Promise<void>;
  fetchScopes: (frameId: number) => Promise<DebugScope[]>;
  fetchVariables: (ref: number) => Promise<DebugVariable[]>;
}

let installed = false;

/** oturum aktifken bir dosyanın breakpoint'lerini sunucuya it (doğrulananları geri yaz) */
async function syncFile(rel: string, lines: number[]) {
  const st = useDebug.getState();
  if (st.status === "idle" || st.status === "starting") return;
  try {
    const { lines: verified } = await bridge.call("debug.setBreakpoints", {
      path: rel,
      lines,
    });
    useDebug.setState((s) => {
      const bp = { ...s.breakpoints };
      if (verified.length) bp[rel] = [...verified].sort((a, b) => a - b);
      else delete bp[rel];
      saveBp(bp);
      return { breakpoints: bp };
    });
  } catch {
    // oturum bu arada bitmiş olabilir — yereldeki liste geçerli kalır
  }
}

export const useDebug = create<DebugState>((set, get) => ({
  status: "idle",
  breakpoints: loadBp(useWorkspace.getState().root),
  stoppedAt: null,
  frames: [],
  file: null,

  install: () => {
    if (installed) return;
    installed = true;
    // proje değişince o projenin breakpoint'leri yüklenir
    useWorkspace.subscribe((s, prev) => {
      if (s.root !== prev.root) {
        set({ breakpoints: loadBp(s.root), status: "idle", stoppedAt: null, frames: [] });
      }
    });
    bridge.on("debug.stopped", ({ reason, path, line, frames }) => {
      set({ status: "stopped", stoppedAt: { path, line, reason }, frames });
      if (path) void useEditor.getState().openAt(path, line, 1);
    });
    bridge.on("debug.continued", () => {
      if (get().status !== "idle") set({ status: "running", stoppedAt: null, frames: [] });
    });
    bridge.on("debug.output", ({ data }) => feedExternalOutput(data));
    bridge.on("debug.terminated", ({ code, durationS }) => {
      endExternalRun(code, durationS);
      set({ status: "idle", stoppedAt: null, frames: [], file: null });
    });
  },

  toggleBreakpoint: (rel, line) => {
    const bp = { ...get().breakpoints };
    const cur = new Set(bp[rel] ?? []);
    if (cur.has(line)) cur.delete(line);
    else cur.add(line);
    if (cur.size) bp[rel] = [...cur].sort((a, b) => a - b);
    else delete bp[rel];
    saveBp(bp);
    set({ breakpoints: bp });
    void syncFile(rel, bp[rel] ?? []);
  },

  removeBreakpoint: (rel, line) => {
    const bp = { ...get().breakpoints };
    const cur = (bp[rel] ?? []).filter((n) => n !== line);
    if (cur.length) bp[rel] = cur;
    else delete bp[rel];
    saveBp(bp);
    set({ breakpoints: bp });
    void syncFile(rel, cur);
  },

  clearBreakpoints: () => {
    const old = get().breakpoints;
    saveBp({});
    set({ breakpoints: {} });
    for (const rel of Object.keys(old)) void syncFile(rel, []);
  },

  start: async (rel) => {
    const target = rel ?? useEditor.getState().activeRel;
    if (!target || !target.endsWith(".py")) {
      toast.info("Debug için bir Python dosyası aç (aktif sekme .py olmalı).");
      return;
    }
    const bp = get().breakpoints;
    set({ status: "starting", file: target, stoppedAt: null, frames: [] });
    useUi.getState().showSideView("debug");
    useUi.getState().showBottom("output");
    beginExternalRun("debug: " + target);
    try {
      await bridge.call("debug.start", {
        rel: target,
        breakpoints: Object.entries(bp).map(([path, lines]) => ({ path, lines })),
      });
      // stopped olayı gelene dek program koşuyor
      set((s) => (s.status === "starting" ? { status: "running" } : {}));
    } catch (e) {
      endExternalRun(null, 0);
      set({ status: "idle", file: null });
      toast.err(e instanceof Error ? e.message : "Debug başlatılamadı.");
    }
  },

  cont: () => void bridge.call("debug.continue", {}).catch(() => {}),
  next: () => void bridge.call("debug.next", {}).catch(() => {}),
  stepIn: () => void bridge.call("debug.stepIn", {}).catch(() => {}),
  stepOut: () => void bridge.call("debug.stepOut", {}).catch(() => {}),

  stop: async () => {
    try {
      await bridge.call("debug.stop", {});
    } catch {
      // zaten bitmiş olabilir
    }
  },

  fetchScopes: async (frameId) => {
    try {
      const { scopes } = await bridge.call("debug.scopes", { frameId });
      return scopes;
    } catch {
      return [];
    }
  },

  fetchVariables: async (ref) => {
    try {
      const { variables } = await bridge.call("debug.variables", { ref });
      return variables;
    } catch {
      return [];
    }
  },
}));
