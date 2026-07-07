/* Titlebar — HTML'de çizilen pencere başlığı.
   Sürükleme: pointerdown → bridge window.startSystemMove (Tauri deseni; chrome.py portu).
   Çift tık → maximize/restore. Durum `window.state` olayından gelir. */

import { Bot, Minus, Square, Copy, X } from "lucide-react";
import { bridge } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { S } from "@/lib/strings.tr";

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
    // yalnız sol tuş + doğrudan sürükleme bölgesi (interaktif çocuklar hariç)
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest("button, input, [data-no-drag]")) return;
    void bridge.call("window.startSystemMove", {});
  };

  return (
    <header
      className="relative z-10 flex shrink-0 items-center border-b border-border-w bg-panel2"
      style={{ height: "var(--titlebar-h)" }}
      onPointerDown={onDragStart}
      onDoubleClick={(e) => {
        const target = e.target as HTMLElement;
        if (target.closest("button, [data-no-drag]")) return;
        void bridge.call("window.toggleMaximize", {});
      }}
    >
      {/* Marka */}
      <div className="flex items-center gap-2 pl-3 pr-2">
        <Bot size={17} className="text-accent" strokeWidth={2.2} />
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

      {/* Proje çipi */}
      <div
        className="ml-1 rounded-[var(--r-pill)] border border-border-w bg-card px-2.5 py-0.5 text-muted"
        style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-caption)" }}
      >
        {project ?? S.common.noProject}
      </div>

      {/* Esnek sürükleme alanı */}
      <div className="min-w-4 flex-1 self-stretch" />

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
