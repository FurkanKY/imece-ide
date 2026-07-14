/* Editor — Monaco montajı + React sekme çubuğu. Model önbelleği: sekme başına
   bir monaco modeli; sekme değişince model swap edilir (içerik/scroll korunur). */

import { useEffect, useRef, useState } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import { X, Circle } from "lucide-react";
import { initMonaco } from "@/lib/monaco";
import { langForPath } from "@/lib/languages";
import { fileIcon } from "@/lib/fileIcons";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";
import { useDebug } from "@/state/debug";
import { DiffView, DiffTab } from "./DiffView";
import { TabMenu } from "./TabMenu";

// F9 gibi kısayolların imleç satırına erişimi için (keymap.ts)
let activeCodeEditor: MonacoEditor.IStandaloneCodeEditor | null = null;
export function getActiveCodeEditor() {
  return activeCodeEditor;
}

function TabBar() {
  const { tabs, activeRel, activate, close, reorder } = useEditor();
  const diff = useEditor((s) => s.diff);
  // sürükle-sırala (P6.3): HTML5 drag — sürüklenen sekmenin rel'i
  const [dragRel, setDragRel] = useState<string | null>(null);
  // taşan sekme çubuğunda aktif sekme görünür kalsın (drag sırasında karışmasın)
  const activeTabRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!dragRel) activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeRel, dragRel]);
  return (
    <div className="flex h-9 shrink-0 items-stretch overflow-x-auto border-b border-border-w bg-side">
      {diff && <DiffTab path={diff.path} />}
      {tabs.map((t) => {
        const on = t.rel === activeRel;
        const { Icon, color } = fileIcon(t.name);
        return (
          <TabMenu key={t.rel} rel={t.rel}>
          <div
            role="tab"
            aria-selected={on}
            tabIndex={0}
            ref={on ? activeTabRef : undefined}
            draggable
            onDragStart={(e) => {
              setDragRel(t.rel);
              e.dataTransfer.effectAllowed = "move";
            }}
            onDragOver={(e) => {
              if (dragRel && dragRel !== t.rel) {
                e.preventDefault(); // bırakmaya izin ver
                reorder(dragRel, t.rel); // canlı yer değiştir (VS Code hissi)
              }
            }}
            onDragEnd={() => setDragRel(null)}
            onClick={() => activate(t.rel)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(t.rel); }
              if (e.key === "Delete") close(t.rel);
            }}
            onAuxClick={(e) => { if (e.button === 1) close(t.rel); }} // orta tık → kapat
            className={
              "group relative flex shrink-0 cursor-pointer items-center gap-1.5 border-r border-border-w px-3 outline-none transition-colors duration-100 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent " +
              (on ? "bg-panel text-text" : "bg-transparent text-muted hover:bg-surface-hover/55 hover:text-text2") +
              (dragRel === t.rel ? " opacity-50" : "")
            }
            style={{ fontSize: "var(--t-label)" }}
          >
            {on && <span className="absolute inset-x-0 top-0 h-[2px] bg-accent" />}
            <Icon size={14} strokeWidth={1.8} style={{ color }} className="shrink-0" />
            <span className="max-w-[160px] truncate">{t.name}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                close(t.rel);
              }}
              className={
                "group/close flex size-4 shrink-0 items-center justify-center rounded outline-none hover:bg-card2 focus-visible:ring-1 focus-visible:ring-accent " +
                (t.dirty || on ? "" : "opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100")
              }
              aria-label={t.dirty ? "Kaydedilmemiş değişiklik var. Sekmeyi kapat" : "Sekmeyi kapat"}
              title="Kapat"
            >
              {t.dirty ? (
                <Circle size={8} className="fill-text2 text-text2 group-hover/close:hidden" />
              ) : null}
              <X size={13} className={t.dirty ? "hidden group-hover/close:block" : ""} />
            </button>
          </div>
          </TabMenu>
        );
      })}
    </div>
  );
}

function Breadcrumb() {
  const activeRel = useEditor((s) => s.activeRel);
  const diff = useEditor((s) => s.diff);
  const path = diff?.path ?? activeRel;
  if (!path) return null;
  const parts = path.split("/");
  return (
    <div
      className="flex h-6 shrink-0 items-center gap-1 overflow-hidden border-b border-border-w bg-panel px-3 text-faint"
      style={{ fontSize: "var(--t-caption)" }}
    >
      {parts.map((p, i) => (
        <span key={i} className="flex min-w-0 items-center gap-1">
          {i > 0 && <span className="shrink-0 opacity-50">›</span>}
          <span className={"truncate " + (i === parts.length - 1 ? "text-text2" : "")}>{p}</span>
        </span>
      ))}
    </div>
  );
}

export function Editor() {
  const hostRef = useRef<HTMLDivElement>(null);
  const edRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const modelsRef = useRef<Map<string, MonacoEditor.ITextModel>>(new Map());
  const viewStatesRef = useRef<Map<string, MonacoEditor.ICodeEditorViewState | null>>(new Map());

  const tabs = useEditor((s) => s.tabs);
  const activeRel = useEditor((s) => s.activeRel);
  const setDraft = useEditor((s) => s.setDraft);

  // Monaco'yu bir kez kur
  useEffect(() => {
    const monaco = initMonaco();
    // LSP Monaco modellerine bağlanır; bu nedenle yalnız editör gerçekten mount
    // olduğunda kurulur. İlk workspace render'ını ağır Monaco paketinden korur.
    void import("@/lib/lsp").then(({ installLsp }) => installLsp());
    if (!hostRef.current) return;
    const ed = monaco.editor.create(hostRef.current, {
      theme: "imece-dark",
      automaticLayout: true,
      fontFamily: "JetBrains Mono",
      fontSize: 13,
      lineHeight: 20,
      minimap: { enabled: true, size: "proportional" },
      scrollBeyondLastLine: false,
      smoothScrolling: true,
      padding: { top: 10 },
      renderLineHighlight: "line",
      cursorBlinking: "smooth",
      guides: { indentation: true },
      wordWrap: useUi.getState().wordWrap ? "on" : "off",
      glyphMargin: true, // P8.2: breakpoint şeridi
    });
    edRef.current = ed;
    activeCodeEditor = ed;
    // süsleme koleksiyonu editörle birlikte doğar/ölür (StrictMode remount tuzağı:
    // eski editörün koleksiyonu ref'te kalırsa süslemeler görünmez)
    decoRef.current = ed.createDecorationsCollection();
    // glyph margin tık → breakpoint aç/kapat (P8.2)
    const mouseSub = ed.onMouseDown((e) => {
      if (
        e.target.type === monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN &&
        e.target.position
      ) {
        const model = ed.getModel();
        if (!model) return;
        const rel = model.uri.path.replace(/^\//, "");
        useDebug.getState().toggleBreakpoint(rel, e.target.position.lineNumber);
      }
    });
    const sub = ed.onDidChangeModelContent(() => {
      const model = ed.getModel();
      if (model) {
        const rel = model.uri.path.replace(/^\//, "");
        setDraft(rel, model.getValue());
      }
    });
    // P7: F12 tanıma-git başka dosyaya çıkarsa sekmede aç + satıra git.
    const opener = monaco.editor.registerEditorOpener({
      openCodeEditor(_source, resource, selectionOrPosition) {
        if (resource.scheme !== "file") return false;
        const rel = resource.path.replace(/^\//, "");
        let line = 1, col = 1;
        if (selectionOrPosition) {
          if ("startLineNumber" in selectionOrPosition) {
            line = selectionOrPosition.startLineNumber;
            col = selectionOrPosition.startColumn;
          } else {
            line = selectionOrPosition.lineNumber;
            col = selectionOrPosition.column;
          }
        }
        void useEditor.getState().openAt(rel, line, col);
        return true;
      },
    });
    return () => {
      sub.dispose();
      mouseSub.dispose();
      opener.dispose();
      if (activeCodeEditor === ed) activeCodeEditor = null;
      decoRef.current = null;
      ed.dispose();
      modelsRef.current.forEach((m) => m.dispose());
      modelsRef.current.clear();
    };
  }, [setDraft]);

  const breakpoints = useDebug((s) => s.breakpoints);
  const stoppedAt = useDebug((s) => s.stoppedAt);
  const decoRef = useRef<MonacoEditor.IEditorDecorationsCollection | null>(null);

  // Aktif sekme değişince modeli değiştir
  useEffect(() => {
    const monaco = initMonaco();
    const ed = edRef.current;
    if (!ed) return;

    // önceki modelin view state'ini sakla
    const prev = ed.getModel();
    if (prev) {
      const prevRel = prev.uri.path.replace(/^\//, "");
      viewStatesRef.current.set(prevRel, ed.saveViewState());
    }

    if (!activeRel) {
      ed.setModel(null);
      return;
    }
    const tab = tabs.find((t) => t.rel === activeRel);
    if (!tab) return;

    let model = modelsRef.current.get(activeRel);
    if (!model) {
      model = monaco.editor.createModel(
        tab.draft,
        langForPath(activeRel),
        monaco.Uri.parse("file:///" + activeRel),
      );
      modelsRef.current.set(activeRel, model);
    }
    ed.setModel(model);
    ed.updateOptions({ readOnly: tab.tooLarge });
    const vs = viewStatesRef.current.get(activeRel);
    if (vs) ed.restoreViewState(vs);
    ed.focus();
  }, [activeRel, tabs]);

  // P8.2: breakpoint + durulan satır süslemeleri — model-swap'tan SONRA tanımlı
  // (efekt sırası: önce model değişir, sonra süslemeler yeni modele uygulanır)
  useEffect(() => {
    const monaco = initMonaco();
    if (!decoRef.current) return;
    if (!activeRel) {
      decoRef.current.set([]);
      return;
    }
    const decos: MonacoEditor.IModelDeltaDecoration[] = [];
    for (const line of breakpoints[activeRel] ?? []) {
      decos.push({
        range: new monaco.Range(line, 1, line, 1),
        options: {
          glyphMarginClassName: "dbg-bp",
          glyphMarginHoverMessage: { value: "Breakpoint (F9 / tık ile kaldır)" },
          stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges,
        },
      });
    }
    if (stoppedAt && stoppedAt.path === activeRel) {
      decos.push({
        range: new monaco.Range(stoppedAt.line, 1, stoppedAt.line, 1),
        options: {
          isWholeLine: true,
          className: "dbg-stop-line",
          glyphMarginClassName: "dbg-stop-arrow",
        },
      });
    }
    decoRef.current.set(decos);
  }, [breakpoints, stoppedAt, activeRel, tabs]);

  // satır kaydırma değişince canlı uygula (Alt+Z)
  const wordWrap = useUi((s) => s.wordWrap);
  useEffect(() => {
    edRef.current?.updateOptions({ wordWrap: wordWrap ? "on" : "off" });
  }, [wordWrap]);

  // satıra git isteği (arama sonucundan)
  const pendingReveal = useEditor((s) => s.pendingReveal);
  const clearReveal = useEditor((s) => s.clearReveal);
  useEffect(() => {
    const ed = edRef.current;
    if (!ed || !pendingReveal || pendingReveal.rel !== activeRel) return;
    const { line, col } = pendingReveal;
    ed.setPosition({ lineNumber: line, column: col });
    ed.revealLineInCenter(line);
    ed.focus();
    clearReveal();
  }, [pendingReveal, activeRel, clearReveal]);

  // kapanan sekmelerin modellerini temizle
  useEffect(() => {
    const live = new Set(tabs.map((t) => t.rel));
    for (const [rel, model] of modelsRef.current) {
      if (!live.has(rel)) {
        model.dispose();
        modelsRef.current.delete(rel);
        viewStatesRef.current.delete(rel);
      }
    }
  }, [tabs]);

  const diff = useEditor((s) => s.diff);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-panel">
      <TabBar />
      <Breadcrumb />
      {/* diff açıkken editör gizlenir (dispose edilmez — model/scroll korunur) */}
      <div ref={hostRef} className={diff ? "hidden" : "min-h-0 flex-1"} />
      {diff && <DiffView path={diff.path} original={diff.original} modified={diff.modified} />}
    </div>
  );
}
