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

  // app
  "app.info": { params: {}; result: { version: string; platform: string; chromium: string } };
  "app.openExternal": { params: { url: string }; result: {} };
  "app.pickFolder": { params: {}; result: { path: string | null } };
  "app.revealInOS": { params: { rel: string }; result: {} };

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
  "fs.delete": { params: { rel: string }; result: {} };
}

export interface DirEntry {
  name: string;
  rel: string;
  isDir: boolean;
  ext: string;
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
