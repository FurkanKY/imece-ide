/* Titlebar — HTML pencere başlığı: marka + merkez komut kapsülü (Cursor/Linear
   deseni; tık → Ctrl+K paleti) + pencere düğmeleri. Sürükleme: pointerdown →
   bridge window.startSystemMove (Tauri deseni; chrome.py portu). */

import { Minus, Play, Square, Copy, X, Search } from "lucide-react";
import { bridge } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useExec } from "@/state/exec";
import { openCommandsPalette } from "@/lib/commands";
import { Logo } from "@/components/brand/Logo";
import { S } from "@/lib/strings.tr";

/** P8.1: ▶ koş / ■ durdur — proje açıkken görünür (F5 kısayolunun görünür yüzü) */
function RunButton() {
  const project = useWorkspace((s) => s.root);
  const running = useExec((s) => s.running);
  if (!project) return null;
  return (
    <button
      data-no-drag
      onClick={() => {
        const ex = useExec.getState();
        if (running) void ex.stop();
        else void ex.run(useEditor.getState().activeRel);
      }}
      title={running ? "Koşuyu durdur (Shift+F5)" : "Çalıştır (F5)"}
      aria-label={running ? "Koşuyu durdur" : "Çalıştır"}
      className={
        "ml-2 flex h-[26px] w-[30px] items-center justify-center rounded-[var(--r-sm)] border transition-colors " +
        (running
          ? "border-err/40 text-err hover:bg-err/10"
          : "border-border-w text-muted hover:border-border-w2 hover:bg-card hover:text-accent")
      }
    >
      {running ? <Square size={11} strokeWidth={2.5} /> : <Play size={12} strokeWidth={2.5} />}
    </button>
  );
}

function WinButton(props: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      aria-label={props.label}
      title={props.label}
      onClick={props.onClick}
      className={
        "flex h-full w-[46px] items-center justify-center text-muted transition-colors duration-[var(--dur-fast)] " +
        (props.danger
          ? "hover:bg-err hover:text-white"
          : "hover:bg-card2 hover:text-text")
      }
    >
      {props.children}
    </button>
  );
}

export function Titlebar({ maximized }: { maximized: boolean }) {
  const project = useWorkspace((s) => s.name);

  const onDragStart = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest("button, input, [data-no-drag]")) return;
    void bridge.call("window.startSystemMove", {});
  };

  return (
    <header
      className="relative z-10 flex shrink-0 items-center border-b border-border-w"
      style={{
        height: "var(--titlebar-h)",
        background: "linear-gradient(to bottom, var(--panel2), var(--activity))",
        boxShadow: "var(--bevel)",
      }}
      onPointerDown={onDragStart}
      onDoubleClick={(e) => {
        const target = e.target as HTMLElement;
        if (target.closest("button, [data-no-drag]")) return;
        void bridge.call("window.toggleMaximize", {});
      }}
    >
      {/* Marka */}
      <div className="flex items-center gap-2 pl-3 pr-2">
        <Logo size={18} glow />
        <span
          className="text-text"
          style={{
            fontSize: "var(--t-title)",
            fontWeight: "var(--w-title)",
            letterSpacing: "var(--ls-title)",
          }}
        >
          {S.appName}
        </span>
      </div>

      {/* Esnek sürükleme alanı (sol) */}
      <div className="min-w-2 flex-1 self-stretch" />

      {/* Merkez komut kapsülü — tık → komut paleti */}
      <button
        data-no-drag
        onClick={() => openCommandsPalette()}
        className="group flex w-[340px] max-w-[36vw] items-center gap-2 rounded-[var(--r-sm)] border border-border-w bg-field px-3 py-[5px] transition-colors hover:border-border-w2 hover:bg-card"
        title="Komut paleti (Ctrl+K)"
      >
        <Search size={12.5} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1 truncate text-left text-faint transition-colors group-hover:text-muted"
              style={{ fontSize: "var(--t-caption)" }}>
          {project ?? S.common.noProject}
        </span>
        <kbd
          className="shrink-0 rounded border border-border-w bg-card px-1.5 py-px text-faint"
          style={{ fontSize: "10px", fontFamily: "var(--font-ui)" }}
        >
          Ctrl K
        </kbd>
      </button>
      <RunButton />

      {/* Esnek sürükleme alanı (sağ) */}
      <div className="min-w-2 flex-1 self-stretch" />

      {/* Pencere düğmeleri */}
      <div className="flex h-full items-stretch" data-no-drag>
        <WinButton label={S.window.minimize} onClick={() => void bridge.call("window.minimize", {})}>
          <Minus size={15} strokeWidth={2} />
        </WinButton>
        <WinButton
          label={maximized ? S.window.restore : S.window.maximize}
          onClick={() => void bridge.call("window.toggleMaximize", {})}
        >
          {maximized ? <Copy size={13} strokeWidth={2} /> : <Square size={12.5} strokeWidth={2} />}
        </WinButton>
        <WinButton label={S.window.close} danger onClick={() => void bridge.call("window.close", {})}>
          <X size={16} strokeWidth={2} />
        </WinButton>
      </div>
    </header>
  );
}
