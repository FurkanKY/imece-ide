/* mock — düz tarayıcıda geliştirme + görsel doğrulama için sahte host.
   Senaryo seçimi: ?scenario=empty|project|running|result|error (P2'de fixtures/run.ts genişler). */

import { Api, Bridge, Events, DebugFrame, Prefs, Proposal, ProviderInfo, ScmChange } from "../protocol";
import * as vfs from "./vfs";
import { RUN_PARTIAL, RUN_FULL, RUN_FOLLOWUP } from "./fixtures/run";
import { ACTIVITY_FEED } from "./fixtures/activity";

interface MockCheckpoint {
  id: string;
  ts: number;
  runId?: string;
  files: string[];
  snapshots: { path: string; exists: boolean; content: string }[];
}

const DEFAULT_PREFS: Prefs = {
  accent: "blue",
  density: "comfortable",
  enterToSend: true,
  animations: true,
  aiEngine: "auto",
  routing: null,
  lastProject: null,
  recentProjects: [
    { path: "C:/Projeler/imece", name: "imece", lastOpened: "2026-07-05T12:00:00Z" },
    { path: "C:/Projeler/demo-api", name: "demo-api", lastOpened: "2026-07-01T09:30:00Z" },
  ],
};

export class MockBridge implements Bridge {
  readonly isNative = false;
  private listeners = new Map<string, Set<(payload: unknown) => void>>();
  private maximized = false;
  private runCancelled = false;
  private mockProposals: Proposal[] = [];
  private checkpoints: MockCheckpoint[] = [];
  private receipts = new Map<string, Api["receipt.get"]["result"]["receipt"]>();
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
      const raw = localStorage.getItem("imece.prefs");
      return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : DEFAULT_PREFS;
    } catch {
      return DEFAULT_PREFS;
    }
  })();

  get scenario(): string {
    return new URLSearchParams(location.search).get("scenario") ?? "empty";
  }

  private mockKeys(): Record<string, string> {
    try {
      return JSON.parse(localStorage.getItem("imece.mock.keys") ?? "{}");
    } catch {
      return {};
    }
  }

  /** engine_factory.role_supported'ın sahte aynası — hangi id'ler yeni motoru sürebilir */
  private mockEngineSupport(id: string): { engineSupported: boolean; engineReason: string } {
    if (id === "qwen-code") return { engineSupported: false, engineReason: "Qwen Code yeni motor tarafından henüz desteklenmiyor." };
    if (["claude", "codex-cli", "gemini-cli"].includes(id)) return { engineSupported: true, engineReason: "hesap (ACP) girişi" };
    return { engineSupported: true, engineReason: "openai-uyumlu API" };
  }

  /** sahte sağlayıcı kataloğu — gerçek providers.py kataloğunun küçük aynası */
  private mockProviders(): ProviderInfo[] {
    const keys = this.mockKeys();
    const models = JSON.parse(localStorage.getItem("imece.mock.models") ?? "{}");
    const api = (id: string, label: string, model: string, list: string[], hint: string, kind: "openai" | "anthropic" = "openai"): ProviderInfo => ({
      id, label, kind, custom: false, ok: !!keys[id],
      docsUrl: "https://example.com", model: models[id] ?? model, models: list,
      keyHint: hint, keyless: false,
      masked: keys[id] ? "•••• " + keys[id].slice(-4) : "",
      ...this.mockEngineSupport(id),
    });
    const cli = (id: string, label: string, docsUrl: string, cliAvailable: boolean, npxAvailable = true): ProviderInfo => ({
      id, label, kind: "cli", custom: false, ok: cliAvailable, docsUrl,
      detail: cliAvailable ? "C:\\mock\\" + id + ".exe" : `'${id}' PATH'te bulunamadı`,
      cliAvailable, npxAvailable,
      ...this.mockEngineSupport(id),
    });
    const out: ProviderInfo[] = [
      cli("claude", "Claude Code", "https://claude.com/claude-code", true),
      api("anthropic", "Claude API", "claude-opus-5", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5-5"], "sk-ant-…", "anthropic"),
      api("deepseek", "DeepSeek", "deepseek-chat", ["deepseek-chat", "deepseek-reasoner"], "sk-…"),
      cli("gemini-cli", "Gemini CLI", "https://github.com/google-gemini/gemini-cli", false),
      api("gemini", "Gemini", "gemini-2.5-flash", ["gemini-2.5-flash", "gemini-3.1-pro-preview"], "AIza…"),
      cli("codex-cli", "Codex CLI", "https://github.com/openai/codex", false),
      api("openai", "OpenAI", "gpt-5.1", ["gpt-5.1", "gpt-5.1-mini"], "sk-…"),
      api("mistral", "Mistral", "mistral-large-latest", ["mistral-large-latest"], "…"),
      api("openrouter", "OpenRouter", "openrouter/auto", ["openrouter/auto"], "sk-or-…"),
      { id: "ollama", label: "Ollama (yerel)", kind: "openai", custom: false, ok: false, docsUrl: "https://ollama.com", model: "qwen2.5-coder", models: ["qwen2.5-coder"], keyHint: "", keyless: true, masked: "", ...this.mockEngineSupport("ollama") },
      cli("qwen-code", "Qwen Code", "https://github.com/QwenLM/qwen-code", false),
    ];
    const custom = JSON.parse(localStorage.getItem("imece.mock.custom") ?? "[]");
    for (const c of custom) {
      out.push({
        id: c.id, label: c.label, kind: "openai", custom: true, ok: !!keys[c.id],
        docsUrl: "", model: models[c.id] ?? c.model, models: [c.model],
        keyHint: "", keyless: false, masked: keys[c.id] ? "•••• " + keys[c.id].slice(-4) : "",
        ...this.mockEngineSupport(c.id),
      });
    }
    return out;
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
        const { rel, command } = params as { rel?: string | null; command?: string };
        const cmd = command
          ? command
          : rel
            ? `python "${rel}"`
            : localStorage.getItem("imece.mock.runcmd") ?? 'python "main.py"';
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
        return { command: localStorage.getItem("imece.mock.runcmd") ?? 'python "main.py"', source: "project_config" } as R;
      case "exec.preflight": {
        const p = params as { rel?: string | null; command?: string };
        const cmd = p.command ?? (p.rel ? `python "${p.rel}"` : localStorage.getItem("imece.mock.runcmd") ?? 'python "main.py"');
        const key = `imece.mock.trusted.${cmd}`;
        return { command: cmd, source: p.command ? "explicit" : p.rel ? "file" : "project_config", cwd: "C:/Projeler/demo-api", fingerprint: key, requiresConfirmation: !p.command && !p.rel && !localStorage.getItem(key) } as R;
      }
      case "exec.approveCommand": {
        const p = params as { rel?: string | null; command?: string };
        const cmd = p.command ?? (p.rel ? `python "${p.rel}"` : localStorage.getItem("imece.mock.runcmd") ?? 'python "main.py"');
        localStorage.setItem(`imece.mock.trusted.${cmd}`, "1");
        return {} as R;
      }
      case "exec.setCommand":
        localStorage.setItem("imece.mock.runcmd", (params as { command: string }).command);
        return {} as R;
      // ---- debug (P8.2): sahte DAP oturumu — breakpoint'te durur, adımlar ----
      case "debug.start": {
        const p = params as Api["debug.start"]["params"];
        const bp = p.breakpoints[0];
        this.dbgFile = p.rel;
        this.dbgLine = bp?.lines[0] ?? 2;
        this.dbgActive = true;
        setTimeout(() => this.emit("debug.output", { data: "bas\r\n" }), 250);
        setTimeout(() => this.dbgEmitStopped("breakpoint"), 500);
        return { started: true } as R;
      }
      case "debug.setBreakpoints":
        return { lines: (params as { lines: number[] }).lines } as R;
      case "debug.continue": {
        this.emit("debug.continued", {});
        setTimeout(() => {
          this.emit("debug.output", { data: "son 5\r\n" });
          this.emit("debug.terminated", { code: 0, durationS: 1.2 });
          this.dbgActive = false;
        }, 400);
        return {} as R;
      }
      case "debug.next":
      case "debug.stepIn":
      case "debug.stepOut": {
        this.emit("debug.continued", {});
        this.dbgLine += 1;
        setTimeout(() => this.dbgEmitStopped("step"), 250);
        return {} as R;
      }
      case "debug.stack":
        return { frames: this.dbgFrames() } as R;
      case "debug.scopes":
        return { scopes: [{ name: "Locals", ref: 101, expensive: false }] } as R;
      case "debug.variables": {
        const ref = (params as { ref: number }).ref;
        return {
          variables: ref === 101
            ? [
                { name: "a", value: "2", type: "int", ref: 0 },
                { name: "b", value: "3", type: "int", ref: 0 },
                { name: "liste", value: "[1, 2]", type: "list", ref: 102 },
              ]
            : [
                { name: "0", value: "1", type: "int", ref: 0 },
                { name: "1", value: "2", type: "int", ref: 0 },
              ],
        } as R;
      }
      case "debug.evaluate":
        return { result: "5", ref: 0 } as R;
      case "debug.stop": {
        if (this.dbgActive) {
          this.emit("debug.terminated", { code: null, durationS: 0.6 });
          this.dbgActive = false;
        }
        return {} as R;
      }
      case "debug.status":
        return { active: this.dbgActive, stopped: this.dbgActive } as R;
      // ---- app.log (Beta-2): tarayıcıda konsola ----
      case "app.log": {
        const p = params as { level: string; message: string; stack?: string };
        // eslint-disable-next-line no-console
        console.warn("[app.log:" + p.level + "]", p.message, p.stack ?? "");
        return { logPath: "C:/mock/app.log" } as R;
      }
      // ---- keys + providers (sağlayıcı kataloğu): sahte durum ----
      case "keys.status": {
        const providers: Record<string, unknown> = {};
        for (const p of this.mockProviders()) providers[p.id] = p;
        return { providers, envPath: "C:/mock/.env" } as R;
      }
      case "keys.set": {
        const saved = this.mockKeys();
        for (const [id, key] of Object.entries(params as Record<string, string>)) {
          if (key) saved[id] = key;
        }
        localStorage.setItem("imece.mock.keys", JSON.stringify(saved));
        return {} as R;
      }
      case "keys.test": {
        const p = params as { provider: string; key?: string };
        const ok = !!(p.key ?? this.mockKeys()[p.provider]);
        return { ok, code: ok ? "" : "auth", detail: ok ? "" : "Anahtar reddedildi (401/403)." } as R;
      }
      case "providers.list":
        return {
          providers: this.mockProviders(),
          defaultRouting: { planner: "claude", coder: "deepseek", reviewer: "gemini" },
        } as R;
      case "providers.setModel": {
        const p = params as { provider: string; model: string };
        const models = JSON.parse(localStorage.getItem("imece.mock.models") ?? "{}");
        models[p.provider] = p.model;
        localStorage.setItem("imece.mock.models", JSON.stringify(models));
        return {} as R;
      }
      case "providers.addCustom": {
        const p = params as { id: string; label: string; baseUrl: string; model: string };
        const custom = JSON.parse(localStorage.getItem("imece.mock.custom") ?? "[]");
        custom.push({ id: p.id, label: p.label, model: p.model });
        localStorage.setItem("imece.mock.custom", JSON.stringify(custom));
        const added = this.mockProviders().find((x) => x.id === p.id);
        return { provider: added } as R;
      }
      case "providers.removeCustom": {
        const p = params as { provider: string };
        const custom = JSON.parse(localStorage.getItem("imece.mock.custom") ?? "[]")
          .filter((c: { id: string }) => c.id !== p.provider);
        localStorage.setItem("imece.mock.custom", JSON.stringify(custom));
        return {} as R;
      }
      case "receipt.get": {
        const id = (params as { receiptId: string }).receiptId;
        const receipt = this.receipts.get(id) ?? {
          id, createdAt: Date.now() / 1000 - 60, finishedAt: Date.now() / 1000,
          status: "applied", task: "Tarih biçimini ISO 8601 yap", routing: { planner: "claude", coder: "deepseek", reviewer: "gemini" },
          plan: { summary: "Tarih yardımcılarını incele ve ISO 8601 çıktısına geçir.", files: ["src/utils.ts"] },
          proposals: [{ path: "src/utils.ts", is_new: false, diff: "-return date.toLocaleString()\n+return date.toISOString()" }],
          review: { verdict: "APPROVED", note: "Değişiklik kapsamla uyumlu." },
          metrics: { latency_s: 12.4, tokens: 1031, cost_usd: 0.0294 }, applied: ["src/utils.ts"], rejected: [], checkpointId: "mock-checkpoint",
          verification: { status: "not_run", detail: "Bu koşuda doğrulama komutu çalıştırılmadı." },
        };
        return { receipt } as R;
      }
      case "receipt.export":
        return { path: "C:/mock/imece-receipt.md" } as R;
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
          return JSON.parse(sessionStorage.getItem("imece.session") ?? "") as R;
        } catch {
          return { openTabs: [], activeTab: null } as R;
        }
      case "session.save":
        sessionStorage.setItem("imece.session", JSON.stringify(params));
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
        localStorage.setItem("imece.prefs", JSON.stringify(this.prefs));
        return {} as R;
      case "run.providers": {
        const providers = this.mockProviders();
        // providers.recommended_routing'in sahte aynası: önce hesap (claude ->
        // codex-cli -> gemini-cli), yoksa anahtarı olan API sağlayıcısı.
        const priority = ["claude", "codex-cli", "gemini-cli", "anthropic", "openai", "deepseek", "gemini"];
        const best = priority.find((id) => providers.find((p) => p.id === id)?.ok)
          ?? providers.find((p) => p.ok)?.id
          ?? "claude";
        return {
          providers,
          recommendedRouting: { planner: best, coder: best, reviewer: best },
        } as R;
      }
      case "run.start": {
        this.runCancelled = false;
        // F6 (@-mentions): backend'in run.start doğrulamasını taklit eder —
        // var olmayan bahisler sessizce düşürülür + bir "info" olayı yayar
        // (bkz. webhost/api/run.py _validate_mentions).
        const mentions = (params as { mentions?: string[] }).mentions ?? [];
        for (const m of mentions) {
          if (!vfs.pathExists(m)) {
            this.emit("run.event", {
              runId: "mock-1",
              ev: { type: "info", text: `Bahsedilen dosya bulunamadı: ${m}` },
            });
          }
        }
        void this.streamRun();
        void this.streamActivity(); // F1: run.activity mock akışı
        return { runId: "mock-1" } as R;
      }
      case "run.cancel":
        this.runCancelled = true;
        this.emit("run.finished", { runId: "mock-1", status: "cancelled" });
        return {} as R;
      case "run.followUp": {
        const { feedback } = params as { feedback: string; mentions?: string[] };
        if (this.scenario === "legacy") {
          throw new Error("Klasik motorda takip isteği desteklenmiyor; yeni bir görev başlatın.");
        }
        this.runCancelled = false;
        void this.streamFollowUp(feedback);
        return { runId: "mock-1" } as R;
      }
      case "run.applyProposals": {
        const wanted = new Set((params as { paths: string[] }).paths);
        const selected = this.mockProposals.filter((proposal) => wanted.has(proposal.path));
        const snapshots = selected.map((proposal) => ({
          path: proposal.path,
          exists: vfs.fileExists(proposal.path),
          content: vfs.readFile(proposal.path).content,
        }));
        const applied: string[] = [];
        for (const p of selected) {
          vfs.writeFile(p.path, p.new);
          applied.push(p.path);
        }
        this.mockProposals = this.mockProposals.filter((p) => !wanted.has(p.path));
        const checkpointId = applied.length ? `mock-c${this.checkpoints.length + 1}` : null;
        if (checkpointId) {
          this.checkpoints.unshift({
            id: checkpointId,
            ts: Date.now() / 1000,
            runId: "mock-1",
            files: applied,
            snapshots,
          });
          this.emit("fs.changed", { kind: "modified", paths: applied });
        }
        return { applied, errors: [], conflicts: [], checkpointId } as R;
      }
      case "checkpoint.list":
        return {
          checkpoints: this.checkpoints.map(({ snapshots: _snapshots, ...checkpoint }) => checkpoint),
        } as R;
      case "checkpoint.restore": {
        const id = (params as { checkpointId: string }).checkpointId;
        const checkpoint = this.checkpoints.find((item) => item.id === id);
        if (!checkpoint) throw new Error("Checkpoint bulunamadı.");
        for (const snapshot of checkpoint.snapshots) {
          if (snapshot.exists) vfs.writeFile(snapshot.path, snapshot.content);
          else vfs.deleteNode(snapshot.path);
        }
        this.emit("fs.changed", { kind: "modified", paths: checkpoint.files });
        return { restored: checkpoint.files } as R;
      }
      case "run.rejectProposals":
        this.mockProposals = [];
        return {} as R;
      case "history.list":
        return {
          items: [
            { ts: Date.now() / 1000 - 3600, task: "utils.py'deki tarih biçimini ISO 8601 yap", verdict: "APPROVED", tokens: 1031, cost_usd: 0.0294, files: ["src/utils.ts"], receipt_id: "00000000-0000-0000-0000-000000000001", status: "applied" },
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
        return { termId: id, shell: "powershell" } as R;
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

  // ---- debug mock durumu (P8.2) ----
  private dbgActive = false;
  private dbgFile = "main.py";
  private dbgLine = 2;

  private dbgFrames(): DebugFrame[] {
    return [
      { id: 1, name: "topla", path: this.dbgFile, line: this.dbgLine },
      { id: 2, name: "<module>", path: this.dbgFile, line: 6 },
    ];
  }

  private dbgEmitStopped(reason: string) {
    if (!this.dbgActive) return;
    this.emit("debug.stopped", {
      reason,
      threadId: 1,
      path: this.dbgFile,
      line: this.dbgLine,
      frames: this.dbgFrames(),
    });
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
      // F2: ?scenario=legacy, Composer'ın takip-isteği devre dışı ipucunu
      // görsel olarak doğrulamak için -- diğer TÜM senaryolar pipeline'dır
      // (F1'in run.activity akışı zaten yalnızca pipeline motorunda anlamlı).
      this.emit("run.finished", {
        runId: "mock-1", status: "done",
        engine: this.scenario === "legacy" ? "legacy" : "pipeline",
      });
    }
  }

  /** F2 (takip isteği): run.followUp mock akışı -- YENİ bir Planner
      denemesi olmadan doğrudan code -> review -> proposal. */
  private async streamFollowUp(feedback: string) {
    this.emit("run.event", { runId: "mock-1", ev: { type: "followUpStarted", feedback } });
    for (const [delay, ev] of RUN_FOLLOWUP) {
      await new Promise((r) => setTimeout(r, delay));
      if (this.runCancelled) return;
      if (ev.type === "proposal") {
        this.mockProposals = (ev.proposals as Proposal[]) ?? [];
      }
      this.emit("run.event", { runId: "mock-1", ev });
    }
    this.emit("run.finished", { runId: "mock-1", status: "done", engine: "pipeline" });
  }

  /** F1: run.activity kanalını ayrı bir zaman çizelgesiyle akıtır (running
      senaryosu yarıda durur; diğerleri tam akış gösterir). */
  private async streamActivity() {
    const feed = this.scenario === "running" ? ACTIVITY_FEED.slice(0, 9) : ACTIVITY_FEED;
    let seq = 1;
    for (const [delay, item] of feed) {
      await new Promise((r) => setTimeout(r, delay));
      if (this.runCancelled) return;
      this.emit("run.activity", { ...item, runId: "mock-1", seq: seq++, ts: new Date().toISOString() });
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
