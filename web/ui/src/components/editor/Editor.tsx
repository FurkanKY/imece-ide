/* Editor — Monaco montajı + React sekme çubuğu. Model önbelleği: sekme başına
   bir monaco modeli; sekme değişince model swap edilir (içerik/scroll korunur). */

import { useEffect, useRef } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import { X, Circle } from "lucide-react";
import { initMonaco, langForPath } from "@/lib/monaco";
import { fileIcon } from "@/lib/fileIcons";
import { useEditor } from "@/state/editor";
import { DiffView, DiffTab } from "./DiffView";

function TabBar() {
  const { tabs, activeRel, activate, close } = useEditor();
  const diff = useEditor((s) => s.diff);
  return (
    <div className="flex h-9 shrink-0 items-stretch overflow-x-auto border-b border-border-w bg-side">
      {diff && <DiffTab path={diff.path} />}
      {tabs.map((t) => {
        const on = t.rel === activeRel;
        const { Icon, color } = fileIcon(t.name);
        return (
          <div
            key={t.rel}
            role="tab"
            aria-selected={on}
            tabIndex={0}
            onClick={() => activate(t.rel)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(t.rel); }
              if (e.key === "Delete") close(t.rel);
            }}
            onAuxClick={(e) => { if (e.button === 1) close(t.rel); }} // orta tık → kapat
            className={
              "group relative flex cursor-pointer items-center gap-1.5 border-r border-border-w px-3 transition-colors duration-100 " +
              (on ? "bg-panel text-text" : "bg-transparent text-muted hover:bg-card hover:text-text2")
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
              className="flex size-4 items-center justify-center rounded hover:bg-card2"
              aria-label="Sekmeyi kapat"
              title="Kapat"
            >
              {t.dirty ? (
                <Circle size={8} className="fill-text2 text-text2 group-hover:hidden" />
              ) : null}
              <X size={13} className={t.dirty ? "hidden group-hover:block" : ""} />
            </button>
          </div>
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
    if (!hostRef.current) return;
    const ed = monaco.editor.create(hostRef.current, {
      theme: "magent-dark",
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
    });
    edRef.current = ed;
    const sub = ed.onDidChangeModelContent(() => {
      const model = ed.getModel();
      if (model) {
        const rel = model.uri.path.replace(/^\//, "");
        setDraft(rel, model.getValue());
      }
    });
    return () => {
      sub.dispose();
      ed.dispose();
      modelsRef.current.forEach((m) => m.dispose());
      modelsRef.current.clear();
    };
  }, [setDraft]);

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
