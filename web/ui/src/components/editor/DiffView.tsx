/* DiffView — merkez inline diff (Cursor deseni): öneri tam boy Monaco diff
   editöründe incelenir. editor.js openDiff/closeDiff'in React karşılığı. */

import { useEffect, useRef } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import { ArrowLeftRight, Columns2, Rows3, X } from "lucide-react";
import { initMonaco } from "@/lib/monaco";
import { langForPath } from "@/lib/languages";
import { useEditor } from "@/state/editor";
import { useUi } from "@/state/ui";

export function DiffView({ path, original, modified }: {
  path: string;
  original: string;
  modified: string;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const edRef = useRef<MonacoEditor.IStandaloneDiffEditor | null>(null);
  const sideBySide = useUi((s) => s.diffSideBySide);

  useEffect(() => {
    const monaco = initMonaco();
    if (!hostRef.current) return;
    const ed = monaco.editor.createDiffEditor(hostRef.current, {
      theme: "magent-dark",
      automaticLayout: true,
      readOnly: true,
      renderSideBySide: useUi.getState().diffSideBySide, // varsayılan inline (Cursor deseni)
      // dar merkezde Monaco yan-yana isteğini sessizce inline'a düşürür — kullanıcı
      // tercihi açıkça verildiği için bu düşüşü kapat (P6.5 toggle her genişlikte işlesin)
      useInlineViewWhenSpaceIsLimited: false,
      fontFamily: "JetBrains Mono",
      fontSize: 13,
      lineHeight: 20,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      renderOverviewRuler: false,
      hideUnchangedRegions: { enabled: true, contextLineCount: 3 },
    });
    const lang = langForPath(path);
    ed.setModel({
      original: monaco.editor.createModel(original, lang),
      modified: monaco.editor.createModel(modified, lang),
    });
    edRef.current = ed;
    return () => {
      const m = ed.getModel();
      ed.dispose();
      m?.original.dispose();
      m?.modified.dispose();
    };
  }, [path, original, modified]);

  // yan-yana ↔ inline geçişi (P6.5) — editör yeniden kurulmaz
  useEffect(() => {
    edRef.current?.updateOptions({ renderSideBySide: sideBySide });
  }, [sideBySide]);

  return <div ref={hostRef} className="review-diff-canvas min-h-0 flex-1" />;
}

/** Diff açıkken sekme çubuğunda görünen özel sekme */
export function DiffTab({ path }: { path: string }) {
  const closeDiff = useEditor((s) => s.closeDiff);
  const sideBySide = useUi((s) => s.diffSideBySide);
  const toggle = useUi((s) => s.toggleDiffSideBySide);
  return (
    <div
      className="flex items-center gap-1.5 border-r border-border-w bg-panel px-3 text-text"
      style={{ fontSize: "var(--t-label)" }}
    >
      <ArrowLeftRight size={13} strokeWidth="var(--icon-stroke)" className="shrink-0 text-accent" />
      <span className="max-w-[200px] truncate">İnceleme: {path.split("/").pop()}</span>
      <button
        onClick={toggle}
        aria-label={sideBySide ? "Inline görünüme geç" : "Yan-yana görünüme geç"}
        title={sideBySide ? "Inline görünüm" : "Yan-yana görünüm"}
        className="flex size-4 items-center justify-center rounded text-muted hover:bg-card2 hover:text-text"
      >
        {sideBySide ? <Rows3 size={12} strokeWidth="var(--icon-stroke)" /> : <Columns2 size={12} strokeWidth="var(--icon-stroke)" />}
      </button>
      <button
        onClick={closeDiff}
        aria-label="Diff'i kapat"
        className="flex size-4 items-center justify-center rounded hover:bg-card2"
      >
        <X size={12} strokeWidth="var(--icon-stroke)" />
      </button>
    </div>
  );
}
