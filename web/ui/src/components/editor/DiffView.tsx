/* DiffView — merkez inline diff (Cursor deseni): öneri tam boy Monaco diff
   editöründe incelenir. editor.js openDiff/closeDiff'in React karşılığı. */

import { useEffect, useRef } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import { initMonaco, langForPath } from "@/lib/monaco";
import { useEditor } from "@/state/editor";

export function DiffView({ path, original, modified }: {
  path: string;
  original: string;
  modified: string;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const edRef = useRef<MonacoEditor.IStandaloneDiffEditor | null>(null);

  useEffect(() => {
    const monaco = initMonaco();
    if (!hostRef.current) return;
    const ed = monaco.editor.createDiffEditor(hostRef.current, {
      theme: "magent-dark",
      automaticLayout: true,
      readOnly: true,
      renderSideBySide: false, // inline (Cursor deseni)
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

  return <div ref={hostRef} className="min-h-0 flex-1" />;
}

/** Diff açıkken sekme çubuğunda görünen özel sekme */
export function DiffTab({ path }: { path: string }) {
  const closeDiff = useEditor((s) => s.closeDiff);
  return (
    <div
      className="flex items-center gap-1.5 border-r border-border-w bg-panel px-3 text-text"
      style={{ fontSize: "var(--t-label)" }}
    >
      <span className="text-accent" style={{ fontWeight: 700 }}>⇄</span>
      <span className="max-w-[200px] truncate">Diff: {path.split("/").pop()}</span>
      <button
        onClick={closeDiff}
        aria-label="Diff'i kapat"
        className="flex size-4 items-center justify-center rounded hover:bg-card2"
      >
        ×
      </button>
    </div>
  );
}
