/* BottomPanel — VS Code tarzı alt panel: çok sekmeli terminal.
   bottom_panel.py'nin halefi. Son sekme kapanınca panel gizlenir. */

import { useEffect } from "react";
import { Plus, X, TerminalSquare, ChevronDown } from "lucide-react";
import { useTerminals } from "@/state/terminal";
import { useUi } from "@/state/ui";
import { bridge } from "@/bridge";
import { TermView } from "./TermView";

export function BottomPanel() {
  const { terms, activeId, create, kill, activate, onExit } = useTerminals();
  const toggleBottom = useUi((s) => s.toggleBottom);

  // ilk açılışta terminal yoksa bir tane aç
  useEffect(() => {
    if (terms.length === 0) void create();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // süreç kendi kendine biterse (exit yazıldı) sekmeyi düşür
  useEffect(() => {
    const off = bridge.on("terminal.exit", ({ termId }) => onExit(termId));
    return off;
  }, [onExit]);

  // son sekme kapanınca paneli gizle (eski davranış)
  useEffect(() => {
    if (terms.length === 0) {
      const t = setTimeout(() => {
        if (useTerminals.getState().terms.length === 0 && useUi.getState().bottomVisible) {
          useUi.getState().toggleBottom();
        }
      }, 150);
      return () => clearTimeout(t);
    }
  }, [terms.length]);

  return (
    <section className="flex h-full flex-col border-t border-border-w bg-[#0b0c0f]">
      {/* sekme şeridi */}
      <div className="flex h-8 shrink-0 items-center border-b border-border-w bg-side px-1">
        <span
          className="flex items-center gap-1.5 px-2 text-muted"
          style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
        >
          <TerminalSquare size={12} /> TERMİNAL
        </span>
        <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
          {terms.map((t) => (
            <div
              key={t.id}
              onClick={() => activate(t.id)}
              className={
                "group flex cursor-pointer items-center gap-1.5 rounded-[var(--r-xs)] px-2 py-1 " +
                (t.id === activeId ? "bg-card text-text" : "text-muted hover:bg-card/50")
              }
              style={{ fontSize: "var(--t-caption)" }}
            >
              {t.title}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  void kill(t.id);
                }}
                aria-label="Terminali kapat"
                className="rounded p-0.5 opacity-0 hover:bg-card2 group-hover:opacity-100"
              >
                <X size={11} />
              </button>
            </div>
          ))}
        </div>
        <button
          onClick={() => void create()}
          title="Yeni terminal (Ctrl+Shift+`)"
          aria-label="Yeni terminal"
          className="rounded p-1 text-faint transition-colors hover:bg-card hover:text-text2"
        >
          <Plus size={14} />
        </button>
        <button
          onClick={toggleBottom}
          title="Paneli daralt (Ctrl+`)"
          aria-label="Paneli daralt"
          className="rounded p-1 text-faint transition-colors hover:bg-card hover:text-text2"
        >
          <ChevronDown size={14} />
        </button>
      </div>

      {/* terminal gövdeleri — hepsi mount kalır (scrollback korunur), aktif görünür */}
      <div className="relative min-h-0 flex-1">
        {terms.map((t) => (
          <TermView key={t.id} termId={t.id} visible={t.id === activeId} />
        ))}
        {terms.length === 0 && (
          <div className="flex h-full items-center justify-center text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Terminal kapandı — Ctrl+Shift+` ile yeni aç.
          </div>
        )}
      </div>
    </section>
  );
}
