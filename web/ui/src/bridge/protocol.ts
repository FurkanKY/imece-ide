/* protocol.ts — köprü sözleşmesinin TEK doğruluk kaynağı.
   Python tarafı (webhost/protocol.py) bu şemayı birebir aynalar.
   Zarf: istek {id, method, params} → yanıt {id, ok, result|error}
   Olay: {channel, payload}  (bkz. .claude/plans/web-shell-ui.md köprü tablosu) */

// ---- Ortak tipler ----
export type ResizeEdge =
  | "left" | "right" | "top" | "bottom"
  | "topleft" | "topright" | "bottomleft" | "bottomright";

export interface Prefs {
  accent: "blue" | "indigo" | "violet" | "green" | "amber" | "rose";
  density: "comfortable" | "compact";
  enterToSend: boolean;
  animations: boolean;
  lastProject: string | null;
  recentProjects: { path: string; name: string; lastOpened: string }[];
}

export interface RunEvent {
  // project_runner.py olay sözlüğü DEĞİŞMEDEN forward edilir (doğrulandı :80–152)
  type: "stage" | "info" | "output" | "metric" | "diff" | "verdict" | "proposal";
  [key: string]: unknown;
}

// ---- İstek/yanıt yüzeyi ----
export interface Api {
  // window
  "window.minimize": { params: {}; result: {} };
  "window.toggleMaximize": { params: {}; result: { maximized: boolean } };
  "window.close": { params: {}; result: {} };
  "window.startSystemMove": { params: {}; result: {} };
  "window.startSystemResize": { params: { edge: ResizeEdge }; result: {} };
  "window.setZoom": { params: { factor: number }; result: {} };
  "window.confirmClose": { params: {}; result: {} };
  "window.ready": { params: {}; result: {} };

  // session (proje-içi .magent/session.json)
  "session.get": { params: {}; result: SessionData };
  "session.save": { params: SessionData; result: {} };

  // app
  "app.info": { params: {}; result: { version: string; platform: string; chromium: string } };
  "app.openExternal": { params: { url: string }; result: {} };
  "app.pickFolder": { params: {}; result: { path: string | null } };
  "app.revealInOS": { params: { path: string }; result: {} };
  "app.clipboardWrite": { params: { text: string }; result: {} };

  // settings
  "settings.get": { params: {}; result: Prefs };
  "settings.set": { params: Prefs; result: {} };

  // ---- project / fs (P1) ----
  "project.open": { params: { path: string }; result: { root: string; name: string } };
  "project.listFiles": { params: {}; result: { files: string[] } };
  "fs.listDir": { params: { rel: string }; result: { entries: DirEntry[] } };
  "fs.readFile": {
    params: { rel: string };
    result: { content: string; truncated: boolean; tooLarge?: boolean };
  };
  "fs.writeFile": { params: { rel: string; content: string }; result: {} };
  "fs.createFile": { params: { rel: string }; result: { rel: string } };
  "fs.createFolder": { params: { rel: string }; result: { rel: string } };
  "fs.rename": { params: { rel: string; newName: string }; result: { rel: string } };
  "fs.move": { params: { rel: string; newDir: string }; result: { rel: string } };
  "fs.delete": { params: { rel: string }; result: {} };

  // ---- run / history (P2) ----
  "run.providers": {
    params: {};
    result: { providers: string[]; defaultRouting: Routing };
  };
  "run.start": { params: { task: string; routing: Routing }; result: { runId: string } };
  "run.cancel": { params: { runId?: string }; result: {} };
  "run.applyProposals": {
    params: { paths: string[] };
    result: { applied: string[]; errors: { path: string; message: string }[] };
  };
  "run.rejectProposals": { params: {}; result: {} };
  "history.list": { params: {}; result: { items: HistoryItem[] } };

  // ---- terminal (P3, ConPTY) ----
  "terminal.create": {
    params: { cwd?: string; cols: number; rows: number };
    result: { termId: string };
  };
  "terminal.write": { params: { termId: string; data: string }; result: {} };
  "terminal.resize": { params: { termId: string; cols: number; rows: number }; result: {} };
  "terminal.kill": { params: { termId: string }; result: {} };

  // ---- search (P4) ----
  "search.start": {
    params: { query: string; regex?: boolean; caseSensitive?: boolean };
    result: { searchId: string };
  };
  "search.cancel": { params: { searchId?: string }; result: {} };

  // ---- lsp (P7 — Python dil sunucusu, basedpyright) ----
  "lsp.start": { params: {}; result: { running: boolean; ready: boolean } };
  "lsp.stop": { params: {}; result: {} };
  "lsp.status": {
    params: {};
    result: { running: boolean; ready: boolean; server: string };
  };
  /** LSP istek/yanıt geçidi — method LSP metodu (textDocument/completion vb.) */
  "lsp.request": { params: { method: string; params?: unknown }; result: { result: unknown } };
  /** tek yön bildirim (didOpen/didChange/didClose/didSave) */
  "lsp.notify": { params: { method: string; params?: unknown }; result: {} };

  // ---- scm / git (P4) ----
  "scm.status": { params: {}; result: ScmStatus };
  "scm.diff": {
    params: { path: string; staged?: boolean };
    result: { original: string; modified: string };
  };
  "scm.stage": { params: { paths: string[] }; result: {} };
  "scm.unstage": { params: { paths: string[] }; result: {} };
  "scm.discard": { params: { path: string; untracked?: boolean }; result: {} };
  "scm.commit": { params: { message: string }; result: { summary: string } };
}

// status harfleri: M değişti, A eklendi, D silindi, R adlandı, C kopya, U izlenmiyor
export interface ScmChange {
  path: string;
  status: string;
  origPath?: string | null;
}

export interface ScmStatus {
  isRepo: boolean;
  branch: string;
  ahead: number;
  behind: number;
  staged: ScmChange[];
  unstaged: ScmChange[];
}

export interface SearchMatch {
  path: string;
  line: number;
  col: number;
  preview: string;
}

export type Role = "planner" | "coder" | "reviewer";
export type Routing = Record<Role, string>;

export interface HistoryItem {
  ts: number;
  task: string;
  verdict: string;
  tokens: number;
  cost_usd: number;
  files: string[];
}

export interface Proposal {
  path: string;
  new: string;
  diff: string;
  is_new: boolean;
}

export interface DirEntry {
  name: string;
  rel: string;
  isDir: boolean;
  ext: string;
}

/** kabuk düzeni (P4): panel görünürlükleri + boyutları + aktif kenar görünümü */
export interface SessionLayout {
  sideView?: string;
  sidebarVisible?: boolean;
  aiPanelVisible?: boolean;
  bottomVisible?: boolean;
  sidebarWidth?: number;
  aiPanelWidth?: number;
  bottomHeight?: number;
}

export interface SessionData {
  openTabs: string[];
  activeTab: string | null;
  layout?: SessionLayout | null;
}

// ---- Olay kanalları ----
export interface Events {
  "window.state": { maximized: boolean; focused: boolean };
  "window.closeRequested": {};
  "fs.changed": { kind: "created" | "deleted" | "modified" | "renamed"; paths: string[] };
  "run.event": { runId: string; ev: RunEvent };
  "run.finished": { runId: string; status: "done" | "failed" | "cancelled"; error?: string };
  "terminal.data": { termId: string; data: string };
  "terminal.exit": { termId: string; code: number };
  "search.results": { searchId: string; matches: SearchMatch[] };
  "search.done": { searchId: string; total: number; limitHit: boolean };
  /** LSP sunucu bildirimleri (publishDiagnostics, $/magentReady, $/magentExited) */
  "lsp.event": { method: string; params: unknown };
}

export interface Bridge {
  call<M extends keyof Api>(method: M, params: Api[M]["params"]): Promise<Api[M]["result"]>;
  on<C extends keyof Events>(channel: C, cb: (payload: Events[C]) => void): () => void;
  /** true → gerçek Qt host; false → tarayıcı/mock */
  readonly isNative: boolean;
}

export class BridgeError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}
