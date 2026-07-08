/* monaco.ts — Monaco kurulumu tek yerde: worker kablolaması + token'lardan türetilmiş
   koyu tema + uzantı→dil eşlemesi. (Worker'lar P0 smoke testinde bu ortamda kanıtlandı.) */

import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";

let inited = false;

export function initMonaco() {
  if (inited) return monaco;
  inited = true;

  self.MonacoEnvironment = {
    getWorker(_moduleId: string, label: string) {
      if (label === "typescript" || label === "javascript") return new tsWorker();
      if (label === "json") return new jsonWorker();
      if (label === "css" || label === "scss" || label === "less") return new cssWorker();
      if (label === "html" || label === "handlebars" || label === "razor") return new htmlWorker();
      return new editorWorker();
    },
  };

  // ---- P7.1: TS/JS dil zekâsı (Monaco'nun kendi worker servisi) ----
  // eagerModelSync: tüm açık modeller worker'a senkron → dosyalar arası tanıma-git/
  // tamamlama açık sekmeler kapsamında çalışır (worker'ın doğal sınırı; tam proje
  // grafiği değil — bilinçli kapsam, bkz. docs/IDE-PLUS-PLAN.md 7.1).
  for (const d of [monaco.languages.typescript.typescriptDefaults,
                   monaco.languages.typescript.javascriptDefaults]) {
    d.setEagerModelSync(true);
    d.setCompilerOptions({
      target: monaco.languages.typescript.ScriptTarget.ESNext,
      module: monaco.languages.typescript.ModuleKind.ESNext,
      moduleResolution: monaco.languages.typescript.ModuleResolutionKind.NodeJs,
      jsx: monaco.languages.typescript.JsxEmit.ReactJSX,
      allowJs: true,
      esModuleInterop: true,
      allowNonTsExtensions: true, // uzantısız/diff modelleri worker'ı düşürmesin
    });
    d.setDiagnosticsOptions({
      noSemanticValidation: false,
      noSyntaxValidation: false,
      // 2307 "Cannot find module": harici importlar (react vb.) editörde çözülemez —
      // sahte hata üretmesin; proje-içi görece importlar eagerModelSync ile çözülür.
      diagnosticCodesToIgnore: [2307],
    });
  }

  // Token değerleriyle uyumlu koyu tema (Monaco hex ister — CSS var kullanamaz).
  monaco.editor.defineTheme("magent-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "565a63", fontStyle: "italic" },
      { token: "keyword", foreground: "b07cf0" },
      { token: "string", foreground: "4bd48a" },
      { token: "number", foreground: "e9b45a" },
      { token: "type", foreground: "6aa1ff" },
      { token: "function", foreground: "6aa1ff" },
    ],
    colors: {
      "editor.background": "#0e0f12",
      "editor.foreground": "#c6cad3",
      "editorLineNumber.foreground": "#3a3d45",
      "editorLineNumber.activeForeground": "#8b8f9b",
      "editor.selectionBackground": "#26344f",
      "editor.lineHighlightBackground": "#15171c",
      "editorCursor.foreground": "#6aa1ff",
      "editorIndentGuide.background1": "#1b1d23",
      "editorWidget.background": "#131418",
      "editorWidget.border": "#ffffff1a",
      "editorGutter.background": "#0e0f12",
      "scrollbarSlider.background": "#ffffff14",
      "scrollbarSlider.hoverBackground": "#ffffff24",
      "editorOverviewRuler.border": "#00000000",
      // ---- diff editörü: tokens.css semantik paletine bağlı (green/red) ----
      "diffEditor.insertedTextBackground": "#4bd48a2b", // --green %17
      "diffEditor.removedTextBackground": "#ff6b7428", // --red %16
      "diffEditor.insertedLineBackground": "#4bd48a14",
      "diffEditor.removedLineBackground": "#ff6b7412",
      "diffEditorGutter.insertedLineBackground": "#4bd48a1f",
      "diffEditorGutter.removedLineBackground": "#ff6b741c",
      "diffEditorOverview.insertedForeground": "#4bd48a80",
      "diffEditorOverview.removedForeground": "#ff6b7480",
      "diffEditor.unchangedRegionBackground": "#131418",
      "diffEditor.unchangedRegionForeground": "#8b8f9b",
      "diffEditor.unchangedCodeBackground": "#00000000",
      "diffEditor.border": "#ffffff1a",
    },
  });
  // global tema: editörler + colorize (sohbet kod blokları) aynı paleti kullanır
  monaco.editor.setTheme("magent-dark");

  return monaco;
}

const EXT_LANG: Record<string, string> = {
  ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
  ".mjs": "javascript", ".json": "json", ".css": "css", ".scss": "scss", ".less": "less",
  ".html": "html", ".md": "markdown", ".py": "python", ".rs": "rust", ".go": "go",
  ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
  ".rb": "ruby", ".php": "php", ".sql": "sql", ".sh": "shell", ".yml": "yaml",
  ".yaml": "yaml", ".xml": "xml", ".toml": "ini", ".ini": "ini", ".cfg": "ini",
};

export function langForPath(path: string): string {
  const i = path.lastIndexOf(".");
  const ext = i >= 0 ? path.slice(i).toLowerCase() : "";
  return EXT_LANG[ext] ?? "plaintext";
}
