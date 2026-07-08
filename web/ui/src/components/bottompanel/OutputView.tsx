/* OutputView — F5 koşu çıktısı: SALT-OKUNUR xterm (ANSI renkler bedava; klavye
   girdisi yok). Yeni koşuda sıfırlanır; mount'ta ham tampon baştan yazılır,
   sonrası canlı chunk kanalından akar. */

import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { useExec, onExecChunk } from "@/state/exec";

// TermView ile aynı token teması
const THEME = {
  background: "#0b0c0f",
  foreground: "#c6cad3",
  cursor: "#0b0c0f", // salt-okunur: imleç görünmez
  selectionBackground: "#26344f",
  black: "#1b1d23", red: "#ff6b74", green: "#4bd48a", yellow: "#e9b45a",
  blue: "#6aa1ff", magenta: "#b07cf0", cyan: "#4fd0e6", white: "#c6cad3",
  brightBlack: "#565a63", brightRed: "#ff8a91", brightGreen: "#6fe3a5",
  brightYellow: "#f0c87e", brightBlue: "#8ab6ff", brightMagenta: "#c69af5",
  brightCyan: "#7adfef", brightWhite: "#f2f3f6",
};

export function OutputView({ visible }: { visible: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const execId = useExec((s) => s.execId);

  useEffect(() => {
    if (!hostRef.current) return;
    const term = new Terminal({
      fontFamily: "JetBrains Mono, Consolas, monospace",
      fontSize: 13,
      lineHeight: 1.25,
      theme: THEME,
      disableStdin: true,
      cursorBlink: false,
      scrollback: 10000,
      allowProposedApi: true,
      convertEol: true, // yakalanan boru çıktısı çıplak \n içerebilir
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current);
    void import("@xterm/addon-webgl")
      .then(({ WebglAddon }) => {
        const gl = new WebglAddon();
        gl.onContextLoss(() => gl.dispose());
        term.loadAddon(gl);
      })
      .catch(() => {});
    fit.fit();

    onExecChunk((data) => term.write(data));
    const ro = new ResizeObserver(() => {
      if (hostRef.current && hostRef.current.offsetHeight > 0) fit.fit();
    });
    ro.observe(hostRef.current);
    termRef.current = term;
    return () => {
      onExecChunk(null);
      ro.disconnect();
      term.dispose();
      termRef.current = null;
    };
  }, []);

  // yeni koşu (veya mount): sıfırla + ham tamponu bas
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.reset();
    const raw = useExec.getState().raw;
    if (raw) term.write(raw);
  }, [execId]);

  return (
    <div
      className={"absolute inset-0 bg-[#0b0c0f] px-2 pt-1 " + (visible ? "" : "invisible")}
      ref={hostRef}
    />
  );
}
