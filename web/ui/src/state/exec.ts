/* exec store — F5 koşusu (P8.1): durum + ham çıktı tamponu.
   Ham metin saklanır çünkü P9.2 "Hatayı ekibe gönder" bunu görev bağlamı yapacak.
   xterm'e canlı akış için hafif chunk kanalı (zustand'ı byte akışıyla yormadan). */

import { create } from "zustand";
import { bridge } from "@/bridge";
import { toast } from "@/components/toasts/toasts";
import { useUi } from "@/state/ui";

const RAW_MAX = 2 * 1024 * 1024; // ham tampon üst sınırı (bellek)

type ChunkCb = (data: string) => void;
let chunkCb: ChunkCb | null = null;
/** OutputView kayıt olur: yeni chunk'lar doğrudan xterm'e yazılır */
export function onExecChunk(cb: ChunkCb | null) {
  chunkCb = cb;
}

// exec.run yanıtı store'a yazılmadan olay gelirse (köprü sıralaması) kaybolmasın
const pendingChunks = new Map<string, string>();

/** çıktıyı temizle: ham tampon + görünür xterm (ANSI clear) */
export function clearExecOutput() {
  useExec.setState({ raw: "" });
  chunkCb?.("\x1b[2J\x1b[3J\x1b[H");
}

/* ---- dış koşu kanalı (P8.2 debug): debugger çıktısı aynı ÇIKTI sekmesine akar ---- */

export function beginExternalRun(label: string) {
  useExec.setState({
    execId: "external", command: label, running: true,
    exitCode: null, durationS: null, raw: "",
  });
  chunkCb?.("\x1b[2J\x1b[3J\x1b[H");
}

export function feedExternalOutput(data: string) {
  useExec.setState((s) => ({ raw: (s.raw + data).slice(-RAW_MAX) }));
  chunkCb?.(data);
}

export function endExternalRun(code: number | null, durationS: number) {
  useExec.setState({ running: false, exitCode: code ?? 0, durationS });
}

interface ExecState {
  execId: string | null;
  command: string;
  running: boolean;
  exitCode: number | null;
  durationS: number | null;
  /** ham çıktı (ANSI'li) — OutputView (yeniden) mount olunca baştan yazar */
  raw: string;
  install: () => void;
  /** rel verilirse dosyayı, verilmezse projeyi koşar */
  run: (rel?: string | null) => Promise<void>;
  stop: () => Promise<void>;
}

let installed = false;

export const useExec = create<ExecState>((set, get) => ({
  execId: null,
  command: "",
  running: false,
  exitCode: null,
  durationS: null,
  raw: "",

  install: () => {
    if (installed) return;
    installed = true;
    bridge.on("exec.output", ({ execId, data }) => {
      if (execId !== get().execId) {
        pendingChunks.set(execId, (pendingChunks.get(execId) ?? "") + data);
        return;
      }
      set((s) => ({ raw: (s.raw + data).slice(-RAW_MAX) }));
      chunkCb?.(data);
    });
    bridge.on("exec.exited", ({ execId, code, durationS }) => {
      if (execId !== get().execId) return;
      set({ running: false, exitCode: code, durationS });
    });
  },

  run: async (rel) => {
    try {
      useUi.getState().showBottom("output");
      const { execId, command } = await bridge.call("exec.run", { rel: rel ?? null });
      const early = pendingChunks.get(execId) ?? "";
      pendingChunks.clear();
      set({ execId, command, running: true, exitCode: null, durationS: null, raw: early });
      if (early) chunkCb?.(early);
    } catch (e) {
      toast.err(e instanceof Error ? e.message : "Çalıştırılamadı.");
    }
  },

  stop: async () => {
    // ÇIKTI sekmesi bir debug koşusunu gösteriyorsa durdurma oraya gider
    if (get().execId === "external") {
      const { useDebug } = await import("@/state/debug");
      void useDebug.getState().stop();
      return;
    }
    try {
      await bridge.call("exec.stop", {});
    } catch {
      // zaten bitmiş olabilir
    }
  },
}));
