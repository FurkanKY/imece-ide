/* monaco.ts — Monaco kurulumu tek yerde: worker kablolaması + token'lardan türetilmiş
   koyu tema + uzantı→dil eşlemesi. (Worker'lar P0 smoke testinde bu ortamda kanıtlandı.) */

import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
export { langForPath } from "@/lib/languages";

let inited = false;

/* ---- tokens.css → Monaco renk eşlemesi ----
   Monaco hex ister; CSS var() kullanamaz. Accent'tan BAĞIMSIZ (kararlı) token'lar
   runtime'da :root'tan okunur — tek doğruluk kaynağı tokens.css kalır. Fallback'ler
   tokens.css'teki mevcut değerlerin kopyasıdır (stylesheet henüz yoksa görünüm değişmez). */

const h2 = (n: number) => n.toString(16).padStart(2, "0");

/** "#rrggbb(aa)" | "rgb(a)(r,g,b(,a))" → Monaco'nun kabul ettiği "#rrggbb(aa)" */
function toMonacoHex(v: string): string | null {
  if (v.startsWith("#")) return v;
  const m = v.match(/^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)(?:[\s,/]+([\d.]+))?\s*\)$/);
  if (!m) return null;
  const a = m[4] === undefined ? "" : h2(Math.round(parseFloat(m[4]) * 255));
  return `#${h2(+m[1])}${h2(+m[2])}${h2(+m[3])}${a}`;
}

function cssToken(name: string, fallback: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return (raw && toMonacoHex(raw)) || fallback;
}

/** 6 haneli hex'e alpha kanalı ("00".."ff") ekler */
const withAlpha = (hex: string, aa: string) => hex.slice(0, 7) + aa;

/* Accent ailesi SABİT (varsayılan blue): Monaco teması bir kez tanımlanır ve runtime
   accent değişimini takip edemez; mevcut davranış (accent'tan bağımsız hep mavi) korunur. */
const ACCENT_BLUE = "#6aa1ff"; // tokens.css [data-accent="blue"] --accent
const ACCENT_BLUE_DIM = "#26344f"; // tokens.css [data-accent="blue"] --accentdim
/* Syntax paleti UI accent'ından bilinçli bağımsız (okunabilirlik sabit kalır). */
const SYNTAX_KEYWORD = "#b07cf0"; // tokens.css violet accent değeriyle aynı
/* tokens.css'te karşılığı olmayan editör-yerel ara tonlar (görünüm birebir korunur). */
const LINE_NUMBER = "#3a3d45";
const LINE_HIGHLIGHT = "#15171c";
const WIDGET_BG = "#131418";
const SCROLLBAR = "#ffffff14";
const SCROLLBAR_HOVER = "#ffffff24";
const TRANSPARENT = "#00000000";

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

  // Token'lardan türetilmiş koyu tema: kararlı değerler tokens.css'ten okunur.
  const side = cssToken("--side", "#0e0f12");
  const text2 = cssToken("--text2", "#c6cad3");
  const muted = cssToken("--muted", "#8b8f9b");
  const faint = cssToken("--faint", "#565a63");
  const line = cssToken("--line", "#1b1d23");
  const border = cssToken("--border", "#ffffff1a"); // rgba(255,255,255,.1)
  const green = cssToken("--green", "#4bd48a");
  const red = cssToken("--red", "#ff6b74");
  const amber = cssToken("--amber", "#e9b45a");

  monaco.editor.defineTheme("imece-dark", {
    base: "vs-dark",
    inherit: true,
    // rules[].foreground '#' istemez (colors map'i ister)
    rules: [
      { token: "comment", foreground: faint.slice(1), fontStyle: "italic" },
      { token: "keyword", foreground: SYNTAX_KEYWORD.slice(1) },
      { token: "string", foreground: green.slice(1) },
      { token: "number", foreground: amber.slice(1) },
      { token: "type", foreground: ACCENT_BLUE.slice(1) },
      { token: "function", foreground: ACCENT_BLUE.slice(1) },
    ],
    colors: {
      "editor.background": side,
      "editor.foreground": text2,
      "editorLineNumber.foreground": LINE_NUMBER,
      "editorLineNumber.activeForeground": muted,
      "editor.selectionBackground": ACCENT_BLUE_DIM,
      "editor.lineHighlightBackground": LINE_HIGHLIGHT,
      "editorCursor.foreground": ACCENT_BLUE,
      "editorIndentGuide.background1": line,
      "editorWidget.background": WIDGET_BG,
      "editorWidget.border": border,
      "editorGutter.background": side,
      "scrollbarSlider.background": SCROLLBAR,
      "scrollbarSlider.hoverBackground": SCROLLBAR_HOVER,
      "editorOverviewRuler.border": TRANSPARENT,
      // ---- diff editörü: tokens.css semantik paletine bağlı (green/red) ----
      "diffEditor.insertedTextBackground": withAlpha(green, "2b"), // --green %17
      "diffEditor.removedTextBackground": withAlpha(red, "28"), // --red %16
      "diffEditor.insertedLineBackground": withAlpha(green, "14"),
      "diffEditor.removedLineBackground": withAlpha(red, "12"),
      "diffEditorGutter.insertedLineBackground": withAlpha(green, "1f"),
      "diffEditorGutter.removedLineBackground": withAlpha(red, "1c"),
      "diffEditorOverview.insertedForeground": withAlpha(green, "80"),
      "diffEditorOverview.removedForeground": withAlpha(red, "80"),
      "diffEditor.unchangedRegionBackground": WIDGET_BG,
      "diffEditor.unchangedRegionForeground": muted,
      "diffEditor.unchangedCodeBackground": TRANSPARENT,
      "diffEditor.border": border,
    },
  });
  // global tema: editörler + colorize (sohbet kod blokları) aynı paleti kullanır
  monaco.editor.setTheme("imece-dark");

  // İmleç kayması düzeltmesi: @font-face JetBrains Mono yüklenmeden Monaco karakter
  // genişliği ölçerse imleç metnin içine kayar → fontlar hazır olunca yeniden ölç.
  // (loadingdone: editör açıkken sonradan yüklenen ağırlıklar için ikinci ağ.)
  document.fonts.ready.then(() => monaco.editor.remeasureFonts());
  document.fonts.addEventListener("loadingdone", () => monaco.editor.remeasureFonts());

  return monaco;
}
