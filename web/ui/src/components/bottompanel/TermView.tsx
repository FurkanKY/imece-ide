/* TermView — tek bir xterm örneği: klavye → terminal.write, terminal.data → yaz,
   boyut değişince fit + terminal.resize. WebGL renderer (başarısızsa DOM'a düşer). */

import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { bridge } from "@/bridge";

// tokens.css ile uyumlu xterm teması
const THEME = {
  background: "#0b0c0f",
  foreground: "#c6cad3",
  cursor: "#6aa1ff",
  cursorAccent: "#0b0c0f",
  selectionBackground: "#26344f",
  black: "#1b1d23", red: "#ff6b74", green: "#4bd48a", yellow: "#e9b45a",
  blue: "#6aa1ff", magenta: "#b07cf0", cyan: "#4fd0e6", white: "#c6cad3",
  brightBlack: "#565a63", brightRed: "#ff8a91", brightGreen: "#6fe3a5",
  brightYellow: "#f0c87e", brightBlue: "#8ab6ff", brightMagenta: "#c69af5",
  brightCyan: "#7adfef", brightWhite: "#f2f3f6",
};

export function TermView({ termId, visible }: { termId: string; visible: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);

  useEffect(() => {
    if (!hostRef.current) return;
    const term = new Terminal({
      fontFamily: "JetBrains Mono, Consolas, monospace",
      fontSize: 13,
      lineHeight: 1.25,
      theme: THEME,
      cursorBlink: true,
      scrollback: 10000, // bellek sınırı (plan risk 2)
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current);
    // WebGL hızlandırma — context alınamazsa sessizce DOM renderer kalır
    void import("@xterm/addon-webgl")
      .then(({ WebglAddon }) => {
        const gl = new WebglAddon();
        gl.onContextLoss(() => gl.dispose());
        term.loadAddon(gl);
      })
      .catch(() => {});
    fit.fit();
    term.focus();

    const offData = bridge.on("terminal.data", ({ termId: id, data }) => {
      if (id === termId) term.write(data);
    });
    const onData = term.onData((data) => {
      void bridge.call("terminal.write", { termId, data });
    });
    const onResize = term.onResize(({ cols, rows }) => {
      void bridge.call("terminal.resize", { termId, cols, rows });
    });
    const ro = new ResizeObserver(() => {
      if (hostRef.current && hostRef.current.offsetHeight > 0) fit.fit();
    });
    ro.observe(hostRef.current);

    termRef.current = term;
    fitRef.current = fit;
    return () => {
      ro.disconnect();
      offData();
      onData.dispose();
      onResize.dispose();
      term.dispose();
    };
  }, [termId]);

  // görünür olunca yeniden sığdır + odakla
  useEffect(() => {
    if (visible) {
      requestAnimationFrame(() => {
        fitRef.current?.fit();
        termRef.current?.focus();
      });
    }
  }, [visible]);

  return (
    <div
      ref={hostRef}
      className={"h-full w-full px-2 pt-1 " + (visible ? "" : "hidden")}
      style={{ background: "#0b0c0f" }}
    />
  );
}
