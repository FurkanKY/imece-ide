/* lsp.ts — ince LSP istemcisi (Python / basedpyright, köprü üzerinden).
   monaco-languageclient KULLANILMAZ (VS Code shim'leri ağır/kırılgan; ihtiyaç dar) —
   bkz. docs/IDE-PLUS-PLAN.md P7. Görevler:
     1) belge senkronu: Monaco model yaşam döngüsü → didOpen/didChange(200ms)/didClose
     2) provider'lar: tamamlama · hover · tanıma-git · imza yardımı
     3) publishDiagnostics → setModelMarkers (hata alt çizgileri)
   URI çevirisi: Monaco modelleri `file:///<rel>`, LSP mutlak `file:///C%3A/...` ister —
   köprü İKİ yönde de burada çevrilir. */

import type { editor, languages, IRange } from "monaco-editor";
import { create } from "zustand";
import { bridge } from "@/bridge";
import { initMonaco } from "@/lib/monaco";
import { useWorkspace } from "@/state/workspace";

type LspStatus = "off" | "starting" | "ready";
export const useLsp = create<{ status: LspStatus }>(() => ({ status: "off" }));

let installed = false;
let root = "";                                   // normalize edilmiş proje kökü
const versions = new Map<string, number>();      // rel → didChange sürüm sayacı
const changeTimers = new Map<string, ReturnType<typeof setTimeout>>();

// ---------------- URI çevirisi ----------------

const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "");

function toLspUri(rel: string): string {
  return "file:///" + (root + "/" + rel).split("/").map(encodeURIComponent).join("/");
}

function fromLspUri(uri: string): string | null {
  if (!uri.startsWith("file://")) return null;
  const path = norm(uri.replace(/^file:\/\/\/?/, "").split("/")
    .map((s) => { try { return decodeURIComponent(s); } catch { return s; } }).join("/"));
  if (!path.toLowerCase().startsWith(root.toLowerCase() + "/")) return null; // kök dışı (site-packages vb.)
  return path.slice(root.length + 1);
}

// ---------------- konum/aralık çevirisi (LSP 0-tabanlı ↔ Monaco 1-tabanlı) ----------------

interface LspRange { start: { line: number; character: number }; end: { line: number; character: number } }

const toMonacoRange = (r: LspRange): IRange => ({
  startLineNumber: r.start.line + 1, startColumn: r.start.character + 1,
  endLineNumber: r.end.line + 1, endColumn: r.end.character + 1,
});

const toLspPosition = (p: { lineNumber: number; column: number }) =>
  ({ line: p.lineNumber - 1, character: p.column - 1 });

// ---------------- belge senkronu ----------------

function relOf(model: editor.ITextModel): string | null {
  if (model.uri.scheme !== "file" || model.getLanguageId() !== "python") return null;
  return model.uri.path.replace(/^\//, "");
}

function didOpen(model: editor.ITextModel) {
  const rel = relOf(model);
  if (!rel) return;
  versions.set(rel, 1);
  void bridge.call("lsp.notify", {
    method: "textDocument/didOpen",
    params: { textDocument: { uri: toLspUri(rel), languageId: "python",
                              version: 1, text: model.getValue() } },
  });
}

function flushChange(rel: string, text: string) {
  const t = changeTimers.get(rel);
  if (t) { clearTimeout(t); changeTimers.delete(rel); }
  const v = (versions.get(rel) ?? 1) + 1;
  versions.set(rel, v);
  void bridge.call("lsp.notify", {
    method: "textDocument/didChange",
    params: { textDocument: { uri: toLspUri(rel), version: v },
              contentChanges: [{ text }] },  // tam metin senkronu (yeterli + basit)
  });
}

/** istek atmadan önce bekleyen didChange'i hemen gönder — bayat tamamlama önlenir */
function ensureSynced(model: editor.ITextModel) {
  const rel = relOf(model);
  if (rel && changeTimers.has(rel)) flushChange(rel, model.getValue());
}

// ---------------- istekler ----------------

async function req<T>(method: string, params: unknown): Promise<T | null> {
  if (useLsp.getState().status !== "ready") return null;
  try {
    const { result } = await bridge.call("lsp.request", { method, params });
    return (result as T) ?? null;
  } catch {
    return null; // LS kapandı/yanıt hatası — dil özelliği sessizce yok sayılır
  }
}

const docAt = (model: editor.ITextModel, position: { lineNumber: number; column: number }) => ({
  textDocument: { uri: toLspUri(relOf(model)!) },
  position: toLspPosition(position),
});

// ---------------- provider'lar ----------------

function markupToString(c: unknown): string {
  if (!c) return "";
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map(markupToString).join("\n\n");
  const o = c as { value?: string };
  return o.value ?? "";
}

function registerProviders(monaco: ReturnType<typeof initMonaco>) {
  const K = monaco.languages.CompletionItemKind;
  // LSP CompletionItemKind (1-tabanlı) → Monaco enum'u
  const KIND: Record<number, languages.CompletionItemKind> = {
    1: K.Text, 2: K.Method, 3: K.Function, 4: K.Constructor, 5: K.Field,
    6: K.Variable, 7: K.Class, 8: K.Interface, 9: K.Module, 10: K.Property,
    11: K.Unit, 12: K.Value, 13: K.Enum, 14: K.Keyword, 15: K.Snippet,
    16: K.Color, 17: K.File, 18: K.Reference, 19: K.Folder, 20: K.EnumMember,
    21: K.Constant, 22: K.Struct, 23: K.Event, 24: K.Operator, 25: K.TypeParameter,
  };

  interface LspCompletionItem {
    label: string; kind?: number; detail?: string; documentation?: unknown;
    insertText?: string; sortText?: string; filterText?: string;
    textEdit?: { range: LspRange; newText: string };
  }

  monaco.languages.registerCompletionItemProvider("python", {
    triggerCharacters: [".", "(", "[", '"', "'"],
    async provideCompletionItems(model, position) {
      if (!relOf(model)) return null;
      ensureSynced(model);
      const res = await req<{ items?: LspCompletionItem[] } | LspCompletionItem[]>(
        "textDocument/completion", docAt(model, position));
      const items = Array.isArray(res) ? res : res?.items;
      if (!items?.length) return null;
      const word = model.getWordUntilPosition(position);
      const fallback: IRange = {
        startLineNumber: position.lineNumber, startColumn: word.startColumn,
        endLineNumber: position.lineNumber, endColumn: word.endColumn,
      };
      return {
        suggestions: items.map((it) => ({
          label: it.label,
          kind: KIND[it.kind ?? 1] ?? K.Text,
          detail: it.detail,
          documentation: { value: markupToString(it.documentation) },
          insertText: it.textEdit?.newText ?? it.insertText ?? it.label,
          sortText: it.sortText,
          filterText: it.filterText,
          range: it.textEdit ? toMonacoRange(it.textEdit.range) : fallback,
        })),
      };
    },
  });

  monaco.languages.registerHoverProvider("python", {
    async provideHover(model, position) {
      if (!relOf(model)) return null;
      ensureSynced(model);
      const res = await req<{ contents: unknown; range?: LspRange }>(
        "textDocument/hover", docAt(model, position));
      const value = markupToString(res?.contents);
      if (!value) return null;
      return {
        contents: [{ value }],
        range: res?.range ? toMonacoRange(res.range) : undefined,
      };
    },
  });

  interface LspLocation { uri?: string; range?: LspRange;
                          targetUri?: string; targetSelectionRange?: LspRange }

  monaco.languages.registerDefinitionProvider("python", {
    async provideDefinition(model, position) {
      if (!relOf(model)) return null;
      ensureSynced(model);
      const res = await req<LspLocation | LspLocation[]>(
        "textDocument/definition", docAt(model, position));
      const locs = res ? (Array.isArray(res) ? res : [res]) : [];
      const out: languages.Location[] = [];
      for (const l of locs) {
        const rel = fromLspUri(l.targetUri ?? l.uri ?? "");
        const range = l.targetSelectionRange ?? l.range;
        if (rel && range)
          out.push({ uri: monaco.Uri.parse("file:///" + rel), range: toMonacoRange(range) });
      }
      return out.length ? out : null;
    },
  });

  monaco.languages.registerSignatureHelpProvider("python", {
    signatureHelpTriggerCharacters: ["(", ","],
    async provideSignatureHelp(model, position) {
      if (!relOf(model)) return null;
      ensureSynced(model);
      const res = await req<{
        signatures: { label: string; documentation?: unknown;
                      parameters?: { label: string | [number, number];
                                     documentation?: unknown }[] }[];
        activeSignature?: number; activeParameter?: number;
      }>("textDocument/signatureHelp", docAt(model, position));
      if (!res?.signatures?.length) return null;
      return {
        value: {
          signatures: res.signatures.map((s) => ({
            label: s.label,
            documentation: { value: markupToString(s.documentation) },
            parameters: (s.parameters ?? []).map((p) => ({
              label: p.label,
              documentation: { value: markupToString(p.documentation) },
            })),
          })),
          activeSignature: res.activeSignature ?? 0,
          activeParameter: res.activeParameter ?? 0,
        },
        dispose() {},
      };
    },
  });
}

// ---------------- diagnostics ----------------

interface LspDiagnostic { range: LspRange; message: string; severity?: number;
                          code?: string | number; source?: string }

function applyDiagnostics(monaco: ReturnType<typeof initMonaco>,
                          uri: string, diags: LspDiagnostic[]) {
  const rel = fromLspUri(uri);
  if (!rel) return;
  const model = monaco.editor.getModel(monaco.Uri.parse("file:///" + rel));
  if (!model) return;
  const SEV = monaco.MarkerSeverity;
  const sevMap: Record<number, number> = { 1: SEV.Error, 2: SEV.Warning, 3: SEV.Info, 4: SEV.Hint };
  monaco.editor.setModelMarkers(model, "basedpyright", diags.map((d) => ({
    severity: sevMap[d.severity ?? 1] ?? SEV.Error,
    message: d.message,
    code: d.code != null ? String(d.code) : undefined,
    source: d.source ?? "basedpyright",
    ...toMonacoRange(d.range),
  })));
}

// ---------------- kurulum ----------------

export function installLsp() {
  if (installed || !bridge.isNative) return; // mock'ta dil sunucusu yok
  installed = true;
  const monaco = initMonaco();
  registerProviders(monaco);

  // model yaşam döngüsü → belge senkronu (editör store'dan bağımsız; tüm python
  // modellerini yakalar — diff'in URI'siz inmemory modelleri relOf'ta elenir)
  monaco.editor.onDidCreateModel((m) => {
    didOpen(m);
    m.onDidChangeContent(() => {
      const rel = relOf(m);
      if (!rel || useLsp.getState().status === "off") return;
      const t = changeTimers.get(rel);
      if (t) clearTimeout(t);
      changeTimers.set(rel, setTimeout(() => flushChange(rel, m.getValue()), 200));
    });
  });
  monaco.editor.onWillDisposeModel((m) => {
    const rel = relOf(m);
    if (!rel) return;
    const t = changeTimers.get(rel);
    if (t) { clearTimeout(t); changeTimers.delete(rel); }
    versions.delete(rel);
    void bridge.call("lsp.notify", {
      method: "textDocument/didClose",
      params: { textDocument: { uri: toLspUri(rel) } },
    });
  });

  // sunucu olayları
  bridge.on("lsp.event", ({ method, params }) => {
    if (method === "$/magentReady") {
      useLsp.setState({ status: "ready" });
      monaco.editor.getModels().forEach(didOpen); // taze sunucuya açıkları tanıt
    } else if (method === "$/magentExited") {
      useLsp.setState({ status: "off" });
    } else if (method === "textDocument/publishDiagnostics") {
      const p = params as { uri: string; diagnostics: LspDiagnostic[] };
      applyDiagnostics(monaco, p.uri, p.diagnostics ?? []);
    }
  });

  // proje açılınca sunucuyu başlat / değişince yeniden başlat
  useWorkspace.subscribe((s, prev) => {
    if (s.root === prev.root) return;
    if (s.root) {
      root = norm(s.root);
      useLsp.setState({ status: "starting" });
      void bridge.call("lsp.start", {}).catch(() => useLsp.setState({ status: "off" }));
    } else {
      root = "";
      useLsp.setState({ status: "off" });
      void bridge.call("lsp.stop", {});
    }
  });
  // proje bu kurulumdan ÖNCE açılmışsa (oturum geri yükleme yarışı) yakala
  const cur = useWorkspace.getState().root;
  if (cur) {
    root = norm(cur);
    useLsp.setState({ status: "starting" });
    void bridge.call("lsp.start", {}).catch(() => useLsp.setState({ status: "off" }));
  }
}
