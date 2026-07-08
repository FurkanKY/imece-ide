/* mock — düz tarayıcıda geliştirme + görsel doğrulama için sahte host.
   Senaryo seçimi: ?scenario=empty|project|running|result|error (P2'de fixtures/run.ts genişler). */

import { Api, Bridge, Events, Prefs, Proposal, ScmChange } from "../protocol";
import * as vfs from "./vfs";
import { RUN_PARTIAL, RUN_FULL } from "./fixtures/run";

const DEFAULT_PREFS: Prefs = {
  accent: "blue",
  density: "comfortable",
  enterToSend: true,
  animations: true,
  lastProject: null,
  recentProjects: [
    { path: "C:/Projeler/multi-agent", name: "multi-agent", lastOpened: "2026-07-05T12:00:00Z" },
    { path: "C:/Projeler/demo-api", name: "demo-api", lastOpened: "2026-07-01T09:30:00Z" },
  ],
};

export class MockBridge implements Bridge {
  readonly isNative = false;
  private listeners = new Map<string, Set<(payload: unknown) => void>>();
  private maximized = false;
  private runCancelled = false;
  private mockProposals: Proposal[] = [];
  private termCounter = 0;
  // sahte git durumu — ScmView geliştirme/webshot senaryosu
  private scmStaged: ScmChange[] = [{ path: "src/utils.ts", status: "M" }];
  private scmUnstaged: ScmChange[] = [
    { path: "src/App.tsx", status: "M" },
    { path: "README.md", status: "M" },
    { path: "src/notlar.md", status: "U" },
  ];
  private prefs: Prefs = (() => {
    try {
      const raw = localStorage.getItem("magent.prefs");
      return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : DEFAULT_PREFS;
    } catch {
      return DEFAULT_PREFS;
    }
  })();

  get scenario(): string {
    return new URLSearchParams(location.search).get("scenario") ?? "empty";
  }

  private emit<C extends keyof Events>(channel: C, payload: Events[C]) {
    this.listeners.get(channel)?.forEach((cb) => cb(payload));
  }

  async call<M extends keyof Api>(method: M, params: Api[M]["params"]): Promise<Api[M]["result"]> {
    await new Promise((r) => setTimeout(r, 15)); // köprü gecikmesi hissi
    type R = Api[M]["result"];
    switch (method) {
      case "window.minimize":
      case "window.close":
      case "window.startSystemMove":
      case "window.startSystemResize":
      case "window.confirmClose":
      case "window.ready":
        return {} as R;
      // ---- exec / F5 (P8.1): sahte akışlı koşu ----
      case "exec.run": {
        const rel = (params as { rel?: string | null }).rel;
        const cmd = rel ? `python "${rel}"` : localStorage.getItem("magent.mock.runcmd") ?? 'python "main.py"';
        const execId = "x" + ++this.termCounter;
        const lines = [
          `\x1b[36m$ ${cmd}\x1b[0m\r\n`,
          "Sunucu ayağa kalkıyor…\r\n",
          "\x1b[32mOK\x1b[0m 3 test geçti · \x1b[33m1 uyarı\x1b[0m\r\n",
        ];
        lines.forEach((l, i) =>
          setTimeout(() => this.emit("exec.output", { execId, data: l }), 200 + i * 350));
        setTimeout(() =>
          this.emit("exec.exited", { execId, code: 0, durationS: 1.4 }), 200 + lines.length * 350);
        return { execId, command: cmd } as R;
      }
      case "exec.stop":
        return {} as R;
      case "exec.getCommand":
        return { command: localStorage.getItem("magent.mock.runcmd") ?? 'python "main.py"' } as R;
      case "exec.setCommand":
        localStorage.setItem("magent.mock.runcmd", (params as { command: string }).command);
        return {} as R;
      // ---- lsp (P7): tarayıcıda dil sunucusu yok — zararsız no-op ----
      case "lsp.start":
        return { running: false, ready: false } as R;
      case "lsp.stop":
      case "lsp.notify":
        return {} as R;
      case "lsp.status":
        return { running: false, ready: false, server: "mock" } as R;
      case "lsp.request":
        return { result: null } as R;
      case "session.get":
        try {
          return JSON.parse(sessionStorage.getItem("magent.session") ?? "") as R;
        } catch {
          return { openTabs: [], activeTab: null } as R;
        }
      case "session.save":
        sessionStorage.setItem("magent.session", JSON.stringify(params));
        return {} as R;
      case "window.toggleMaximize":
        this.maximized = !this.maximized;
        this.emit("window.state", { maximized: this.maximized, focused: true });
        return { maximized: this.maximized } as R;
      case "app.info":
        return { version: "0.1.0-mock", platform: "browser", chromium: "-" } as R;
      case "app.pickFolder":
        return { path: "C:/Projeler/demo-api" } as R;
      case "project.open":
        return { root: (params as { path: string }).path, name: "demo-api" } as R;
      case "project.listFiles":
        return { files: vfs.listAllFiles() } as R;
      case "fs.listDir":
        return vfs.listDir((params as { rel: string }).rel) as R;
      case "fs.readFile":
        return vfs.readFile((params as { rel: string }).rel) as R;
      case "fs.writeFile": {
        const p = params as { rel: string; content: string };
        vfs.writeFile(p.rel, p.content);
        return {} as R;
      }
      case "fs.createFile": {
        const p = params as { rel: string };
        vfs.createNode(p.rel, false);
        return { rel: p.rel } as R;
      }
      case "fs.createFolder": {
        const p = params as { rel: string };
        vfs.createNode(p.rel, true);
        return { rel: p.rel } as R;
      }
      case "fs.rename": {
        const p = params as { rel: string; newName: string };
        return { rel: vfs.renameNode(p.rel, p.newName) } as R;
      }
      case "fs.move": {
        const p = params as { rel: string; newDir: string };
        return { rel: vfs.moveNode(p.rel, p.newDir) } as R;
      }
      case "fs.delete":
        vfs.deleteNode((params as { rel: string }).rel);
        return {} as R;
      case "window.setZoom":
        document.documentElement.style.zoom = String((params as { factor: number }).factor);
        return {} as R;
      case "app.clipboardWrite":
      case "app.revealInOS":
      case "app.openExternal":
        return {} as R;
      case "settings.get":
        return this.prefs as R;
      case "settings.set":
        this.prefs = params as Prefs;
        localStorage.setItem("magent.prefs", JSON.stringify(this.prefs));
        return {} as R;
      case "run.providers":
        return {
          providers: ["claude", "deepseek", "gemini"],
          defaultRouting: { planner: "claude", coder: "deepseek", reviewer: "gemini" },
        } as R;
      case "run.start": {
        this.runCancelled = false;
        void this.streamRun();
        return { runId: "mock-1" } as R;
      }
      case "run.cancel":
        this.runCancelled = true;
        this.emit("run.finished", { runId: "mock-1", status: "cancelled" });
        return {} as R;
      case "run.applyProposals": {
        const wanted = new Set((params as { paths: string[] }).paths);
        const applied: string[] = [];
        for (const p of this.mockProposals) {
          if (wanted.has(p.path)) {
            vfs.writeFile(p.path, p.new);
            applied.push(p.path);
          }
        }
        this.mockProposals = this.mockProposals.filter((p) => !wanted.has(p.path));
        return { applied, errors: [] } as R;
      }
      case "run.rejectProposals":
        this.mockProposals = [];
        return {} as R;
      case "history.list":
        return {
          items: [
            { ts: Date.now() / 1000 - 3600, task: "utils.py'deki tarih biçimini ISO 8601 yap", verdict: "APPROVED", tokens: 1031, cost_usd: 0.0294, files: ["src/utils.ts"] },
            { ts: Date.now() / 1000 - 86400, task: "config.py'ye loglama seviyesi ekle", verdict: "NEEDS_FIX", tokens: 2140, cost_usd: 0.041, files: ["config.py", "main.py"] },
          ],
        } as R;
      case "terminal.create": {
        const id = `mock-t${++this.termCounter}`;
        setTimeout(() => {
          this.emit("terminal.data", {
            termId: id,
            data: "Windows PowerShell (mock)\r\n\x1b[38;2;106;161;255mPS C:\\Projeler\\demo-api>\x1b[0m ",
          });
        }, 120);
        return { termId: id } as R;
      }
      case "terminal.write": {
        const { termId, data } = params as { termId: string; data: string };
        // basit echo: Enter'da sahte prompt bas
        const echoed = data === "\r"
          ? "\r\n\x1b[38;2;139;143;155m(mock çıktı)\x1b[0m\r\n\x1b[38;2;106;161;255mPS C:\\Projeler\\demo-api>\x1b[0m "
          : data;
        setTimeout(() => this.emit("terminal.data", { termId, data: echoed }), 10);
        return {} as R;
      }
      case "terminal.resize":
      case "terminal.kill":
        return {} as R;
      case "search.start": {
        const p = params as { query: string; caseSensitive?: boolean };
        const id = `mock-s${++this.termCounter}`;
        setTimeout(() => {
          const matches: { path: string; line: number; col: number; preview: string }[] = [];
          for (const path of vfs.listAllFiles()) {
            const { content } = vfs.readFile(path);
            const lines = content.split("\n");
            for (let i = 0; i < lines.length; i++) {
              const hay = p.caseSensitive ? lines[i] : lines[i].toLowerCase();
              const needle = p.caseSensitive ? p.query : p.query.toLowerCase();
              const col = hay.indexOf(needle);
              if (col >= 0) matches.push({ path, line: i + 1, col: col + 1, preview: lines[i].trim() });
            }
          }
          this.emit("search.results", { searchId: id, matches });
          this.emit("search.done", { searchId: id, total: matches.length, limitHit: false });
        }, 250);
        return { searchId: id } as R;
      }
      case "search.cancel":
        return {} as R;
      case "scm.status":
        return {
          isRepo: true, branch: "web-shell", ahead: 2, behind: 0,
          staged: [...this.scmStaged], unstaged: [...this.scmUnstaged],
        } as R;
      case "scm.diff": {
        const p = params as { path: string };
        let modified = "";
        try { modified = vfs.readFile(p.path).content; } catch { /* vfs'te yok */ }
        const original = modified
          ? modified.replace(/ISO 8601|export/g, (m) => (m === "export" ? "// eski\nexport" : "eski biçim"))
          : "";
        return { original, modified: modified || "// yeni dosya (mock)" } as R;
      }
      case "scm.stage": {
        const wanted = new Set((params as { paths: string[] }).paths);
        const moving = this.scmUnstaged.filter((c) => wanted.has(c.path));
        this.scmUnstaged = this.scmUnstaged.filter((c) => !wanted.has(c.path));
        for (const c of moving) {
          if (!this.scmStaged.some((s) => s.path === c.path)) {
            this.scmStaged.push({ ...c, status: c.status === "U" ? "A" : c.status });
          }
        }
        return {} as R;
      }
      case "scm.unstage": {
        const wanted = new Set((params as { paths: string[] }).paths);
        const moving = this.scmStaged.filter((c) => wanted.has(c.path));
        this.scmStaged = this.scmStaged.filter((c) => !wanted.has(c.path));
        for (const c of moving) {
          if (!this.scmUnstaged.some((s) => s.path === c.path)) {
            this.scmUnstaged.push({ ...c, status: c.status === "A" ? "U" : c.status });
          }
        }
        return {} as R;
      }
      case "scm.discard": {
        const p = params as { path: string };
        this.scmUnstaged = this.scmUnstaged.filter((c) => c.path !== p.path);
        return {} as R;
      }
      case "scm.commit": {
        const n = this.scmStaged.length;
        this.scmStaged = [];
        return { summary: `[web-shell abc1234] mock commit (${n} dosya)` } as R;
      }
      default:
        console.warn("[mock] karşılıksız metot:", method, params);
        return {} as R;
    }
  }

  /** senaryoya göre koşu olaylarını zamanlamalı akıt */
  private async streamRun() {
    const seq = this.scenario === "running" ? RUN_PARTIAL : RUN_FULL;
    const errorAt = this.scenario === "error" ? 5 : -1; // plan metriği sonrası patla
    let i = 0;
    for (const [delay, ev] of seq) {
      await new Promise((r) => setTimeout(r, delay));
      if (this.runCancelled) return;
      if (i === errorAt) {
        this.emit("run.finished", {
          runId: "mock-1", status: "failed",
          error: "DeepSeek API: 429 Too Many Requests (kota aşıldı)",
        });
        return;
      }
      if (ev.type === "proposal") {
        this.mockProposals = (ev.proposals as Proposal[]) ?? [];
      }
      this.emit("run.event", { runId: "mock-1", ev });
      i++;
    }
    if (this.scenario !== "running") {
      this.emit("run.finished", { runId: "mock-1", status: "done" });
    }
  }

  on<C extends keyof Events>(channel: C, cb: (payload: Events[C]) => void): () => void {
    let set = this.listeners.get(channel);
    if (!set) {
      set = new Set();
      this.listeners.set(channel, set);
    }
    set.add(cb as (payload: unknown) => void);
    return () => set!.delete(cb as (payload: unknown) => void);
  }
}
