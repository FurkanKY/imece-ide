/* BottomPanel — VS Code tarzı alt panel: TERMİNAL (çok sekmeli) + ÇIKTI (F5 koşusu).
   bottom_panel.py'nin halefi. Son terminal sekmesi kapanınca (terminal görünümündeyse)
   panel gizlenir; ÇIKTI görünümü koşu durumunu başlıkta gösterir. */

import { useEffect } from "react";
import { Plus, X, TerminalSquare, ChevronDown, Play, Square, Eraser } from "lucide-react";
import { useTerminals } from "@/state/terminal";
import { useExec, clearExecOutput } from "@/state/exec";
import { useUi } from "@/state/ui";
import { bridge } from "@/bridge";
import { Badge, IconButton, StatusDot } from "@/components/ui";
import { TermView } from "./TermView";
import { OutputView } from "./OutputView";

function ViewTab(props: { label: string; active: boolean; onClick: () => void;
                          icon: React.ReactNode; badge?: React.ReactNode }) {
  return (
    <button
      onClick={props.onClick}
      className={
        "pressable flex items-center gap-1.5 border-b-2 px-2 pb-1 pt-1.5 transition-colors " +
        (props.active
          ? "border-accent text-text2"
          : "border-transparent text-muted hover:text-text2")
      }
      style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)",
               letterSpacing: "var(--ls-overline)" }}
    >
      {props.icon} {props.label} {props.badge}
    </button>
  );
}

export function BottomPanel() {
  const { terms, activeId, create, kill, activate, onExit } = useTerminals();
  const toggleBottom = useUi((s) => s.toggleBottom);
  const view = useUi((s) => s.bottomView);
  const setView = useUi((s) => s.setBottomView);
  const exec = useExec();

  // terminal görünümünde sekme yoksa bir tane aç (ÇIKTI için boşuna PowerShell açma)
  useEffect(() => {
    if (view === "terminal" && terms.length === 0) void create();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // süreç kendi kendine biterse (exit yazıldı) sekmeyi düşür
  useEffect(() => {
    const off = bridge.on("terminal.exit", ({ termId }) => onExit(termId));
    return off;
  }, [onExit]);

  // son sekme kapanınca paneli gizle (yalnız terminal görünümündeyken)
  useEffect(() => {
    if (view === "terminal" && terms.length === 0) {
      const t = setTimeout(() => {
        const ui = useUi.getState();
        if (useTerminals.getState().terms.length === 0 && ui.bottomVisible &&
            ui.bottomView === "terminal") {
          ui.toggleBottom();
        }
      }, 150);
      return () => clearTimeout(t);
    }
  }, [terms.length, view]);

  const exitBadge =
    exec.running ? (
      <StatusDot tone="accent" pulse size={6} className="ml-0.5 text-accent" />
    ) : exec.exitCode !== null ? (
      <Badge tone={exec.exitCode === 0 ? "ok" : "err"} className="ml-0.5 font-mono">
        {exec.exitCode}
      </Badge>
    ) : null;

  return (
    <section className="flex h-full flex-col bg-field">
      {/* başlık şeridi: görünüm sekmeleri + bağlama göre araçlar */}
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border-w bg-side px-1">
        <ViewTab label="TERMİNAL" active={view === "terminal"}
                 onClick={() => setView("terminal")} icon={<TerminalSquare size={12} />}
                 badge={terms.length > 0 ? <Badge tone="neutral">{terms.length}</Badge> : undefined} />
        <ViewTab label="ÇIKTI" active={view === "output"}
                 onClick={() => setView("output")} icon={<Play size={11} />} badge={exitBadge} />

        {view === "terminal" ? (
          <>
            <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
              {terms.map((t) => (
                <div
                  key={t.id}
                  role="tab"
                  aria-selected={t.id === activeId}
                  tabIndex={0}
                  onClick={() => activate(t.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(t.id); }
                    if (e.key === "Delete") void kill(t.id);
                  }}
                  onAuxClick={(e) => { if (e.button === 1) void kill(t.id); }} // orta tık → kapat
                  className={
                    "group flex cursor-pointer items-center gap-1.5 border-b-2 px-2 py-1 " +
                    (t.id === activeId ? "border-accent text-text" : "border-transparent text-muted hover:bg-card/50")
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
                    className="icon-btn size-5 opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
                  >
                    <X size={11} />
                  </button>
                </div>
              ))}
            </div>
            <IconButton
              size="sm"
              icon={Plus}
              label="Yeni terminal"
              title="Yeni terminal (Ctrl+Shift+`)"
              onClick={() => void create()}
            />
          </>
        ) : (
          <>
            <span className="min-w-0 flex-1 truncate px-2 font-mono text-faint"
                  style={{ fontSize: "var(--t-caption)" }}>
              {exec.command || "F5: aktif dosyayı koş · Ctrl+F5: projeyi koş"}
              {exec.durationS !== null && !exec.running && (
                <span className="ml-2 text-muted">{exec.durationS.toFixed(1)} sn</span>
              )}
            </span>
            {exec.running ? (
              <IconButton
                size="sm"
                variant="danger"
                icon={Square}
                label="Koşuyu durdur"
                title="Koşuyu durdur (Shift+F5)"
                className="text-err"
                onClick={() => void exec.stop()}
              />
            ) : (
              exec.execId && (
                <IconButton
                  size="sm"
                  icon={Play}
                  label="Yeniden koş"
                  title="Yeniden koş (Ctrl+F5)"
                  onClick={() => void exec.run()}
                />
              )
            )}
            <IconButton size="sm" icon={Eraser} label="Çıktıyı temizle" onClick={clearExecOutput} />
          </>
        )}
        <IconButton
          size="sm"
          icon={ChevronDown}
          label="Paneli daralt"
          title="Paneli daralt (Ctrl+`)"
          onClick={toggleBottom}
        />
      </div>

      {/* gövdeler — hepsi mount kalır (scrollback korunur), aktif görünür */}
      <div className="relative min-h-0 flex-1">
        {terms.map((t) => (
          <TermView key={t.id} termId={t.id} visible={view === "terminal" && t.id === activeId} />
        ))}
        <OutputView visible={view === "output"} />
        {view === "terminal" && terms.length === 0 && (
          <div className="flex h-full items-center justify-center text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Terminal kapandı — Ctrl+Shift+` ile yeni aç.
          </div>
        )}
      </div>
    </section>
  );
}
