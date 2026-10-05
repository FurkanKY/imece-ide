/* mock — düz tarayıcıda geliştirme + görsel doğrulama için sahte host.
   Senaryo seçimi: ?scenario=empty|project|running|result|error|nochanges (P2'de fixtures/run.ts genişler). */

import { Api, Bridge, BridgeError, Events, DebugFrame, DecisionProviderInfo, Prefs, Proposal, ProviderInfo, ScmChange } from "../protocol";
import * as vfs from "./vfs";
import { RUN_PARTIAL, RUN_FULL, RUN_FOLLOWUP, RUN_NO_CHANGES } from "./fixtures/run";
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
  decisionLayer: "off",
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
  private agentProviderId: string | null = null;
  private mockProposals: Proposal[] = [];
  private checkpoints: MockCheckpoint[] = [];
  private receipts = new Map<string, Api["receipt.get"]["result"]["receipt"]>();
  private termCounter = 0;
  private collabPreviews = new Map<string, Api["collab.preview"]["result"]>();
  private collabApprovals = new Map<string, { memberId: string; taskId: string }>();
  private collabStatus: Api["collab.status"]["result"]["collaboration"] = null;
  private deliveryTickets = new Map<string, Api["collab.delivery.preview"]["result"]>();
  private participantTickets = new Map<string, Api["collab.taskStatus.preview"]["result"]>();
  private deliveryProposals: Api["collab.delivery.list"]["result"]["proposals"] = [];
  private ownerStatus: Api["collab.owner.status"]["result"] = { state: "unconfigured", projectRoot: null, sessionId: null, targetVersion: null, baseCommit: null, revision: null, goal: null, ownerId: null, memberIds: [], tasks: [], storePath: null, hubPath: null, endpoint: null, epoch: 0, exportedMembers: [], retryRequired: false, createdPaths: [] };
  private ownerPreview: Api["collab.owner.previewCreate"]["result"] | null = null;
  private ownerShared = new Set<string>();
  private ownerContext: Api["collab.owner.snapshot"]["result"]["context"] = { goal: "", decisions: [], interfaces: {} };
  private productRevision = 1;
  private productHistory: Api["collab.owner.snapshot"]["result"][] = [];
  private mockRoot = "C:/Projeler/demo-api";

  private rememberProduct(): void {
    const s = this.ownerStatus;
    if (!s.sessionId || !s.baseCommit || !s.revision) return;
    this.productHistory.push({ projectRoot: this.mockRoot, sessionId: s.sessionId, baseCommit: s.baseCommit,
      targetVersion: s.targetVersion!, revision: s.revision, contextHash: this.mockContextHash(), context: structuredClone(this.ownerContext),
      ownerId: s.ownerId!, memberIds: [...s.memberIds], tasks: structuredClone(s.tasks), overlaps: [],
      waitingTaskIds: s.tasks.filter((task) => task.status === "waiting").map((task) => task.id), epoch: s.epoch, state: s.state });
    this.productHistory = this.productHistory.slice(-65);
  }

  private mockContextHash(): string {
    const text = JSON.stringify(this.ownerContext);
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    return `mock-context-${(hash >>> 0).toString(16)}`;
  }
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
        // Jev (TypeSafe) ayrı karar sağlayıcısı — providers kataloğuna
        // karışmaz, yönlendirmeye katılmaz (bkz. decision_credentials.py).
        const tsKey = this.mockKeys()["typesafe"] ?? "";
        const decisionProviders: Record<string, DecisionProviderInfo> = {
          typesafe: {
            id: "typesafe",
            label: "Jev (TypeSafe)",
            ok: !!tsKey,
            masked: tsKey ? "•••• " + tsKey.slice(-4) : "",
            keyHint: "ts-…",
            docsUrl: "https://docs.typesafe.ai",
            sdkAvailable: true,
          },
        };
        return { providers, decisionProviders, envPath: "C:/mock/.env" } as R;
      }
      case "keys.set": {
        // typesafe (Jev) ayrı karar sağlayıcısı: decisionProviders'ı etkiler,
        // normal sağlayıcı kataloğuna/yönlendirmeye karışmaz (bkz.
        // decision_credentials.py doğrulaması).
        const { typesafe, ...rest } = params as Record<string, string>;
        const saved = this.mockKeys();
        if (typeof typesafe === "string" && typesafe.trim()) {
          const key = typesafe.trim();
          const printableAscii = [...key].every((ch) => {
            const code = ch.charCodeAt(0);
            return code >= 32 && code !== 127 && code < 128;
          });
          if (/\s/.test(key) || !printableAscii || /[#"'`\\&;|%<>$]/.test(key)) {
            throw new Error("Anahtar boşluksuz printable ASCII olmalı; sorunlu karakter içeremez.");
          }
          saved["typesafe"] = key;
        }
        for (const [id, key] of Object.entries(rest)) {
          if (key) saved[id] = key;
        }
        localStorage.setItem("imece.mock.keys", JSON.stringify(saved));
        return {} as R;
      }
      case "keys.test": {
        // typesafe (Jev): salt bağlantı testi — proje içeriği yok; anahtar
        // verilmezse kayıtlı anahtar; hiçbiri yoksa no_key (köprü ok döner).
        const p = params as { provider: string; key?: string };
        if (p.provider === "typesafe") {
          const key = (p.key ?? this.mockKeys()["typesafe"] ?? "").trim();
          if (!key) {
            return { ok: false, code: "no_key", detail: "Önce bir anahtar kaydedin." } as R;
          }
          return { ok: true, code: "", detail: "" } as R;
        }
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
        this.mockRoot = (params as { path: string }).path;
        this.collabPreviews.clear();
        this.collabApprovals.clear();
        this.participantTickets.clear();
        this.collabStatus = null;
        return { root: this.mockRoot, name: "demo-api" } as R;
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
      case "collab.preview": {
        const p = params as Api["collab.preview"]["params"];
        if (!p.endpoint.trim() || !p.memberId.trim() || !p.taskId.trim() || !p.credential) throw new Error("Uç nokta, erişim bilgisi, üye ve görev kimliği gerekli.");
        const previewId = `mock-preview-${Date.now()}`;
        const projectRoot = this.mockRoot;
        const result: Api["collab.preview"]["result"] = { previewId, projectRoot, endpoint: p.endpoint, memberId: p.memberId, taskId: p.taskId, sessionId: "mock-session", targetVersion: "mock-v1", baseCommit: "mock-base", revision: "mock-r1", task: { owner: p.memberId, goal: "Sahte paylaşılan görev", scopes: ["src/"], status: "active", contextRevision: "mock-r1" }, context: { goal: "Tarayıcı önizleme verisi", decisions: ["Mock servis; HTTP/Git/model çağrısı yapılmadı."], interfaces: {} } };
        this.collabPreviews.set(previewId, result);
        return result as R;
      }
      case "collab.approve": {
        const { previewId, resetCursor = false } = params as Api["collab.approve"]["params"];
        const preview = this.collabPreviews.get(previewId);
        if (!preview) throw new Error("Önizleme bulunamadı.");
        const approvalHandle = `mock-approval-${Date.now()}`;
        this.collabApprovals.set(approvalHandle, { memberId: preview.memberId, taskId: preview.taskId });
        return { approvalHandle, preview, resetCursor } as R;
      }
      case "collab.discard": {
        const p = params as Api["collab.discard"]["params"];
        if (p.previewId) this.collabPreviews.delete(p.previewId);
        if (p.approvalHandle) this.collabApprovals.delete(p.approvalHandle);
        return {} as R;
      }
      case "collab.status":
        return { collaboration: this.collabStatus } as R;
      case "collab.taskStatus.preview": {
        const p = params as Api["collab.taskStatus.preview"]["params"], current = this.collabStatus;
        if (!current || current.runId !== p.runId || current.active || !current.memberId || !current.taskId ||
            !["queued", "running", "waiting"].includes(p.targetStatus)) throw new Error("Katılımcı görevi şu anda düzenlenemiyor.");
        const ticketId = `mock-participant-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const preview: Api["collab.taskStatus.preview"]["result"] = {
          ticketId, runId: p.runId, projectRoot: this.mockRoot, sessionId: current.sessionId,
          memberId: current.memberId, taskId: current.taskId, fromStatus: "running",
          targetStatus: p.targetStatus, expectedRevision: "mock-task-revision", contextHash: "mock-context",
          freshContextDiffersFromAccepted: false,
        };
        if (this.participantTickets.size >= 2) this.participantTickets.delete(this.participantTickets.keys().next().value!);
        this.participantTickets.set(ticketId, preview);
        return preview as R;
      }
      case "collab.taskStatus.confirm": {
        const p = params as Api["collab.taskStatus.confirm"]["params"], ticket = this.participantTickets.get(p.ticketId);
        if (p.confirm !== true || !ticket || ticket.runId !== p.runId || !this.collabStatus || this.collabStatus.active ||
            this.collabStatus.memberId !== ticket.memberId || this.collabStatus.taskId !== ticket.taskId) throw new Error("Katılımcı görev durumu onayı geçersiz.");
        this.participantTickets.delete(p.ticketId);
        return { revision: "mock-task-revision+1", taskId: ticket.taskId, status: ticket.targetStatus } as R;
      }
      case "collab.taskStatus.discard": {
        const p = params as Api["collab.taskStatus.discard"]["params"];
        this.participantTickets.delete(p.ticketId);
        return {} as R;
      }
      case "collab.owner.previewCreate": {
        const p = params as Api["collab.owner.previewCreate"]["params"];
        if (!p.memberIds.length || !p.ownerId || !p.tasks.length) throw new Error("En az bir üye ve görev gereklidir.");
        const result: Api["collab.owner.previewCreate"]["result"] = { previewId: `mock-owner-preview-${Date.now()}`, projectRoot: this.mockRoot, sessionId: p.sessionId, targetVersion: p.targetVersion, baseCommit: "mock-source-head", goal: p.goal, ownerId: p.ownerId, memberIds: [...p.memberIds], tasks: p.tasks.map((task) => ({ ...task, status: task.status ?? "queued", contextRevision: "mock-r1" })), mode: "create", warnings: ["MOCK: özel metadata işlemi simüle edildi; dizin oluşturulmadı."] };
        this.ownerPreview = result; return result as R;
      }
      case "collab.owner.create": {
        const p = params as Api["collab.owner.create"]["params"];
        if (!this.ownerPreview || this.ownerPreview.previewId !== p.previewId || this.ownerPreview.projectRoot !== this.mockRoot) throw new Error("Önizleme süresi doldu; yeniden deneyin.");
        const plan = this.ownerPreview; this.ownerPreview = null; this.ownerShared.clear();
        this.productRevision = 1; this.ownerContext = { goal: plan.goal, decisions: [], interfaces: {} };
        this.ownerStatus = { ...this.ownerStatus, state: "configured", projectRoot: this.mockRoot, sessionId: plan.sessionId, targetVersion: plan.targetVersion, baseCommit: plan.baseCommit, revision: "mock-r1", goal: plan.goal, ownerId: plan.ownerId, memberIds: plan.memberIds, tasks: plan.tasks, storePath: "C:/mock/private/store", hubPath: "C:/mock/private/hub", endpoint: null, exportedMembers: [], retryRequired: false, createdPaths: ["C:/mock/private/session.json" ] };
        this.productHistory = []; this.rememberProduct();
        return this.ownerStatus as R;
      }
      case "collab.owner.select": {
        const p = params as Api["collab.owner.select"]["params"];
        this.ownerShared.clear(); this.productRevision = 1; this.ownerContext = { goal: "", decisions: [], interfaces: {} }; this.ownerStatus = { ...this.ownerStatus, state: "configured", projectRoot: this.mockRoot, revision: "mock-r1", ownerId: p.ownerId, memberIds: [...p.memberIds], tasks: [], storePath: p.storePath, hubPath: p.hubPath, endpoint: null, exportedMembers: [], retryRequired: false, createdPaths: [] };
        return this.ownerStatus as R;
      }
      case "collab.owner.status": return this.ownerStatus as R;
      case "collab.owner.snapshot": {
        if (!this.ownerStatus.projectRoot || this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Sahip oturumu bu projeye bağlı değil.");
        const board: Api["collab.owner.snapshot"]["result"] = {
          projectRoot: this.mockRoot, sessionId: this.ownerStatus.sessionId!, baseCommit: this.ownerStatus.baseCommit!,
          targetVersion: this.ownerStatus.targetVersion!, revision: this.ownerStatus.revision!, contextHash: this.mockContextHash(),
          context: structuredClone(this.ownerContext), ownerId: this.ownerStatus.ownerId!,
          memberIds: [...this.ownerStatus.memberIds], tasks: this.ownerStatus.tasks.map((task) => ({ ...task })), overlaps: [],
          waitingTaskIds: this.ownerStatus.tasks.filter((task) => task.status === "waiting").map((task) => task.id),
          epoch: this.ownerStatus.epoch, state: this.ownerStatus.state,
        };
        return board as R;
      }
      case "collab.owner.updateContext": {
        const p = params as Api["collab.owner.updateContext"]["params"];
        if (p.confirm !== true) throw new BridgeError("owner_confirmation_required", "Açık onay gerekli.");
        if (this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Proje değişti.");
        if (p.expectedSessionId !== this.ownerStatus.sessionId || p.expectedEpoch !== this.ownerStatus.epoch) throw new BridgeError("owner_epoch_mismatch", "Oturum kimliği veya dönemi değişti.");
        if (this.ownerStatus.state !== "running") throw new BridgeError("owner_not_running", "Oturum çalışmıyor.");
        if (p.expectedRevision !== this.ownerStatus.revision) throw new BridgeError("owner_product_stale_revision", "Revizyon güncel değil.");
        this.ownerContext = structuredClone(p.context); this.productRevision += 1;
        this.ownerStatus = { ...this.ownerStatus, revision: `mock-r${this.productRevision}`, goal: p.context.goal };
        this.rememberProduct();
        return { revision: this.ownerStatus.revision!, sessionId: p.expectedSessionId, epoch: p.expectedEpoch, action: "context" } as R;
      }
      case "collab.owner.updateTaskStatus": {
        const p = params as Api["collab.owner.updateTaskStatus"]["params"];
        if (p.confirm !== true) throw new BridgeError("owner_confirmation_required", "Açık onay gerekli.");
        if (this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Proje değişti.");
        if (p.expectedSessionId !== this.ownerStatus.sessionId || p.expectedEpoch !== this.ownerStatus.epoch) throw new BridgeError("owner_epoch_mismatch", "Oturum kimliği veya dönemi değişti.");
        if (this.ownerStatus.state !== "running") throw new BridgeError("owner_not_running", "Oturum çalışmıyor.");
        if (p.expectedRevision !== this.ownerStatus.revision) throw new BridgeError("owner_product_stale_revision", "Revizyon güncel değil.");
        if (!this.ownerStatus.tasks.some((task) => task.id === p.taskId)) throw new BridgeError("owner_product_invalid", "Görev bulunamadı.");
        this.productRevision += 1;
        this.ownerStatus = { ...this.ownerStatus, revision: `mock-r${this.productRevision}`, tasks: this.ownerStatus.tasks.map((task) => task.id === p.taskId ? { ...task, status: p.status } : task) };
        this.rememberProduct();
        return { revision: this.ownerStatus.revision!, sessionId: p.expectedSessionId, epoch: p.expectedEpoch, action: "task", taskId: p.taskId, status: p.status } as R;
      }
      case "collab.owner.createTask": {
        const p = params as Api["collab.owner.createTask"]["params"], task = p.task;
        if (p.confirm !== true) throw new BridgeError("owner_confirmation_required", "Açık onay gerekli.");
        if (this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Proje değişti.");
        if (p.expectedSessionId !== this.ownerStatus.sessionId || p.expectedEpoch !== this.ownerStatus.epoch) throw new BridgeError("owner_epoch_mismatch", "Oturum kimliği veya dönemi değişti.");
        if (this.ownerStatus.state !== "running") throw new BridgeError("owner_not_running", "Oturum çalışmıyor.");
        if (p.expectedRevision !== this.ownerStatus.revision) throw new BridgeError("owner_product_stale_revision", "Revizyon güncel değil.");
        if (!task || Object.keys(task).sort().join(",") !== "goal,id,owner,scopes" || !task.id || task.id.length > 128 || !task.goal.trim() || task.goal.length > 4000 || !this.ownerStatus.memberIds.includes(task.owner) || !Array.isArray(task.scopes) || task.scopes.length > 32 || task.scopes.some((scope) => typeof scope !== "string" || !scope || scope.length > 512)) throw new BridgeError("owner_invalid", "Yeni görev bilgileri geçersiz.");
        if (this.ownerStatus.tasks.some((item) => item.id === task.id)) throw new BridgeError("owner_task_exists", "Görev kimliği zaten var.");
        const added = { ...task, scopes: [...task.scopes], status: "queued" as const, contextRevision: p.expectedRevision };
        this.productRevision += 1;
        this.ownerStatus = { ...this.ownerStatus, revision: `mock-r${this.productRevision}`, tasks: [...this.ownerStatus.tasks, added] };
        this.rememberProduct();
        return { revision: this.ownerStatus.revision!, sessionId: p.expectedSessionId, epoch: p.expectedEpoch, action: "createTask", taskId: added.id, status: "queued", owner: added.owner, contextRevision: p.expectedRevision } as R;
      }
      case "collab.owner.proposals": {
        const p = params as Api["collab.owner.proposals"]["params"];
        if (this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Proje değişti.");
        if (p.expectedSessionId !== this.ownerStatus.sessionId) throw new BridgeError("owner_session_identity_mismatch", "Oturum değişti.");
        return { sessionId: p.expectedSessionId, baseCommit: this.ownerStatus.baseCommit!, revision: this.ownerStatus.revision!, contextHash: this.mockContextHash(), epoch: this.ownerStatus.epoch, proposals: this.deliveryProposals.filter((proposal) => proposal.sessionId === p.expectedSessionId && proposal.baseCommit === this.ownerStatus.baseCommit).map((proposal) => ({ proposalId: proposal.proposalId, proposalRevision: proposal.proposalRevision, sessionId: proposal.sessionId, baseCommit: proposal.baseCommit, taskId: proposal.taskId, owner: proposal.owner, contextRevision: proposal.contextRevision, contextHash: proposal.contextHash, fileCount: proposal.fileCount, staleContext: proposal.contextHash !== this.mockContextHash() })) } as R;
      }
      case "collab.owner.changes": {
        const p = params as Api["collab.owner.changes"]["params"];
        if (!this.ownerStatus.projectRoot || this.ownerStatus.projectRoot !== this.mockRoot) throw new BridgeError("owner_wrong_project", "Sahip oturumu bu projeye bağlı değil.");
        if (p.expectedSessionId !== this.ownerStatus.sessionId) throw new BridgeError("owner_session_identity_mismatch", "Oturum değişti.");
        if (!["configured", "stopped", "running"].includes(this.ownerStatus.state)) throw new BridgeError("owner_not_running", "Sahip oturumu geçmişi okuyamıyor.");
        const count = p.limit ?? 16;
        if (typeof count !== "number" || !Number.isInteger(count) || count < 1 || count > 16) throw new BridgeError("owner_invalid", "Geçmiş sayfa boyutu geçersiz.");
        const history = this.productHistory.filter((snapshot) => snapshot.sessionId === p.expectedSessionId);
        const start = history.findIndex((snapshot) => snapshot.revision === p.afterRevision);
        if (start < 0) throw new BridgeError("owner_product_history_unavailable", "Güncel panodan başlangıç alın.");
        const page = history.slice(start + 1, start + count + 1);
        const events = page.map((next, index) => {
          const prev = history[start + index];
          // Valid schema IDs such as constructor/toString are data, not prototypes.
          const oldKeys = Object.assign(Object.create(null), prev.context.interfaces);
          const newKeys = Object.assign(Object.create(null), next.context.interfaces);
          const changed = [...new Set([...prev.tasks, ...next.tasks].map((task) => task.id))].sort().filter((id) => JSON.stringify(prev.tasks.find((task) => task.id === id)) !== JSON.stringify(next.tasks.find((task) => task.id === id)));
          const canonicalContext = (context: typeof prev.context) => JSON.stringify({ goal: context.goal, decisions: context.decisions, interfaces: Object.fromEntries(Object.entries(context.interfaces).sort(([a], [b]) => a.localeCompare(b))) });
          const contextChanged = canonicalContext(prev.context) !== canonicalContext(next.context);
          const affected = [...new Set([...changed, ...(contextChanged ? next.tasks.filter((task) => task.status !== "done").map((task) => task.id) : [])])].sort();
          return { fromRevision: prev.revision, toRevision: next.revision, metadataCheckpoint: canonicalContext(prev.context) === canonicalContext(next.context) && JSON.stringify(prev.tasks) === JSON.stringify(next.tasks), goalChanged: prev.context.goal !== next.context.goal, decisionsChanged: JSON.stringify(prev.context.decisions) !== JSON.stringify(next.context.decisions), interfaces: { added: Object.keys(newKeys).sort().filter((key) => !(key in oldKeys)), removed: Object.keys(oldKeys).sort().filter((key) => !(key in newKeys)), changed: Object.keys(newKeys).sort().filter((key) => key in oldKeys && newKeys[key] !== oldKeys[key]) }, taskChanges: changed.slice(0, 32).map((taskId) => { const before = prev.tasks.find((task) => task.id === taskId), after = next.tasks.find((task) => task.id === taskId); const fields = before && after ? [before.goal !== after.goal ? "goal" : "", before.owner !== after.owner ? "assignment" : "", JSON.stringify(before.scopes) !== JSON.stringify(after.scopes) ? "scopes" : "", before.contextRevision !== after.contextRevision ? "contextRevision" : "", before.status !== after.status ? "status" : ""].filter(Boolean) : []; return { taskId, change: !before ? "added" : !after ? "removed" : "changed", previousStatus: before?.status ?? null, status: after?.status ?? null, previousOwner: before?.owner ?? null, owner: after?.owner ?? null, fields }; }), taskChangeCount: changed.length, taskChangesTruncated: changed.length > 32, affectedTaskIds: affected.slice(0, 32), affectedTaskCount: affected.length, affectedTasksTruncated: affected.length > 32, contextChanged };
        });
        const lastRevision = page.at(-1)?.revision ?? p.afterRevision;
        return { sessionId: p.expectedSessionId, baseCommit: this.ownerStatus.baseCommit!, epoch: this.ownerStatus.epoch, headRevision: history.at(-1)!.revision, lastRevision, hasMore: start + count < history.length - 1, events } as R;
      }
      case "collab.owner.start":
        if (this.ownerStatus.state === "cleanup_failed") throw new Error("Sunucu kapatılamadı; yeniden deneyin.");
        if (!this.ownerStatus.projectRoot) throw new Error("Önce oturumu yapılandırın.");
        this.ownerStatus = { ...this.ownerStatus, state: "running", endpoint: "http://127.0.0.1:0", epoch: this.ownerStatus.epoch + 1, exportedMembers: [] }; this.ownerShared.clear(); return this.ownerStatus as R;
      case "collab.owner.stop":
        this.ownerStatus = { ...this.ownerStatus, state: "stopped", endpoint: null, exportedMembers: [] }; this.ownerShared.clear(); return this.ownerStatus as R;
      case "collab.owner.shareOnce": {
        const p = params as Api["collab.owner.shareOnce"]["params"];
        const key = `${this.ownerStatus.epoch}:${p.memberId}`;
        if (this.ownerStatus.state !== "running" || !this.ownerStatus.memberIds.includes(p.memberId) || this.ownerShared.has(key)) throw new Error("Bu üyeye erişim verilemiyor.");
        this.ownerShared.add(key); this.ownerStatus = { ...this.ownerStatus, exportedMembers: [...this.ownerStatus.exportedMembers, p.memberId] };
        return { endpoint: this.ownerStatus.endpoint!, sessionId: this.ownerStatus.sessionId!, baseCommit: this.ownerStatus.baseCommit!, targetVersion: this.ownerStatus.targetVersion!, memberId: p.memberId, credential: "MOCK: erişim datasimülasyon", storePath: this.ownerStatus.storePath!, hubPath: this.ownerStatus.hubPath!, taskIds: this.ownerStatus.tasks.filter((task) => task.owner === p.memberId).map((task) => task.id), epoch: this.ownerStatus.epoch, scope: "loopback-only" } as R;
      }
      case "collab.owner.localPreview": {
        const p = params as Api["collab.owner.localPreview"]["params"];
        const task = this.ownerStatus.tasks.find((item) => item.id === p.taskId && item.owner === p.memberId && (item.status === "queued" || item.status === "running"));
        if (this.ownerStatus.state !== "running" || this.ownerStatus.projectRoot !== this.mockRoot || !task) throw new Error("Görev bu üyeye atanmadı veya etkin değil.");
        const preview: Api["collab.preview"]["result"] = { previewId: `mock-local-preview-${Date.now()}`, projectRoot: this.mockRoot, endpoint: this.ownerStatus.endpoint!, memberId: p.memberId, taskId: task.id, sessionId: this.ownerStatus.sessionId!, targetVersion: this.ownerStatus.targetVersion!, baseCommit: this.ownerStatus.baseCommit!, revision: this.ownerStatus.revision!, task, context: structuredClone(this.ownerContext) };
        this.collabPreviews.set(preview.previewId, preview); return { preview, storePath: this.ownerStatus.storePath!, hubPath: this.ownerStatus.hubPath!, endpoint: this.ownerStatus.endpoint!, memberId: p.memberId, taskId: task.id, epoch: this.ownerStatus.epoch } as R;
      }
      case "collab.delivery.preview": {
        const p = params as Api["collab.delivery.preview"]["params"];
        if (!p.runId || !p.storePath.trim() || !p.hubPath.trim() || !p.paths.length || p.paths.length > 64) throw new Error("Koşu, yerel store/hub yolları ve en fazla 64 dosya gerekli.");
        const capturedPaths = this.scenario === "delivery-subset" && p.paths.length > 1 ? p.paths.slice(0, 1) : [...p.paths];
        const preview: Api["collab.delivery.preview"]["result"] = {
          previewId: `mock-ticket-${Date.now()}`, proposalId: p.proposalId ?? `mock-proposal-${Date.now()}`,
          runId: p.runId, sessionId: "mock-session", taskId: "mock-task", owner: "mock-owner",
          baseCommit: "mock-base", contextRevision: "mock-context-1", contextHash: "mock-context-hash", expectedRevision: "mock-r1",
          paths: capturedPaths, outOfScopePaths: capturedPaths.filter((path) => !path.startsWith("src/")), fileCount: capturedPaths.length,
          artifactBytes: 256 * capturedPaths.length, contentDigest: "mock-digest-no-source-bytes",
          warnings: ["MOCK: hiçbir kaynak dosyası okunmadı veya yayımlanmadı; gerçek Git/ağ/model işlemi yok."],
        };
        this.deliveryTickets.set(preview.previewId, preview);
        return preview as R;
      }
      case "collab.delivery.publish": {
        const p = params as Api["collab.delivery.publish"]["params"];
        const ticket = this.deliveryTickets.get(p.previewId);
        if (!ticket || ticket.runId !== p.runId) throw new Error("MOCK önizleme bileti geçersiz veya süresi dolmuş.");
        if (ticket.outOfScopePaths.length && !p.allowOutOfScope) throw new Error("Kapsam dışı dosyalar için ayrı onay gerekli.");
        this.deliveryTickets.delete(p.previewId);
        this.deliveryProposals = [{ proposalId: ticket.proposalId, proposalRevision: "mock-proposal-r1", taskId: ticket.taskId, owner: ticket.owner, sessionId: ticket.sessionId, baseCommit: ticket.baseCommit, contextRevision: ticket.contextRevision, contextHash: ticket.contextHash, fileCount: ticket.fileCount, currentContextHashMatches: true }, ...this.deliveryProposals];
        return { proposalId: ticket.proposalId, sessionRevision: "mock-r2", proposalRevision: "mock-proposal-r1", contextRevision: ticket.contextRevision, contextHash: ticket.contextHash, paths: ticket.paths, outOfScopePaths: ticket.outOfScopePaths, outOfScopeAuthorized: ticket.outOfScopePaths.length === 0 || !!p.allowOutOfScope } as R;
      }
      case "collab.delivery.discard": {
        this.deliveryTickets.delete((params as Api["collab.delivery.discard"]["params"]).previewId);
        return {} as R;
      }
      case "collab.delivery.list":
        return { proposals: this.deliveryProposals.map((proposal) => ({ ...proposal })) } as R;
      case "collab.delivery.candidate": {
        const p = params as Api["collab.delivery.candidate"]["params"];
        if (!p.proposalIds.length || !p.outputPath.trim()) throw new Error("En az bir öneri ve yeni çıktı dizini seçin.");
        if (p.proposalIds.length > 16) throw new Error("En fazla 16 öneri seçilebilir.");
        const selected = this.deliveryProposals.filter((proposal) => p.proposalIds.includes(proposal.proposalId));
        if (selected.length !== p.proposalIds.length) throw new Error("Bir veya daha fazla öneri bulunamadı.");
        const conflicts = selected.flatMap((proposal) => proposal.currentContextHashMatches ? [] : [`${proposal.proposalId}: bağlam eski`]);
        if (conflicts.length) return { candidate: null, conflicts } as R;
        return { candidate: { session_id: "mock-session", session_revision: "mock-r2", binding_revision: "mock-binding-r1", context_hash: "mock-context-hash", base_commit: "mock-base", proposal_ids: p.proposalIds, proposals: selected.map((proposal) => ({ proposal_id: proposal.proposalId, task_id: proposal.taskId, owner: proposal.owner, context_revision: proposal.contextRevision, paths: [] })), candidate_dir: p.outputPath, file_count: selected.reduce((count, proposal) => count + proposal.fileCount, 0), content_fingerprint: "mock-fingerprint", verification: { status: p.verify ? "pass" : "not_run", plan_id: null, checks: [], changed_content: false, ...(p.verify ? { fingerprint_complete: true, fingerprint_before: "mock-before", fingerprint_after: "mock-before" } : {}) }, conflicts: [], notes: ["MOCK: yeni aday sonucu simüle edildi; dosya yazımı ve proje komutu çalıştırılmadı."] }, conflicts: [] } as R;
      }
      case "run.start": {
        const startParams = params as Api["run.start"]["params"];
        if ("providerId" in startParams) {
          if (startParams.collabApprovalHandle) throw new Error("Tek ajan akışında ortak bağlam henüz bağlı değil.");
          this.agentProviderId = startParams.providerId;
          this.runCancelled = false;
          this.collabStatus = null;
          const mentions = startParams.mentions ?? [];
          for (const mention of mentions) if (!vfs.pathExists(mention)) this.emit("run.event", { runId: "mock-1", ev: { type: "info", text: `Bahsedilen dosya bulunamadı: ${mention}` } });
          void this.streamAgentRun();
          void this.streamActivity();
          return { runId: "mock-1" } as R;
        }
        this.agentProviderId = null;
        const collabHandle = startParams.collabApprovalHandle;
        const approvedCollab = collabHandle ? this.collabApprovals.get(collabHandle) : undefined;
        if (collabHandle && !approvedCollab) throw new Error("Ortak bağlam onayı geçersiz.");
        if (collabHandle) this.collabApprovals.delete(collabHandle);
        this.participantTickets.clear();
        this.collabStatus = approvedCollab ? { state: "streaming", code: null, consumedRevision: null, receivedRevision: "mock-r1", pendingCount: 1, sessionId: "mock-session", taskId: approvedCollab.taskId, memberId: approvedCollab.memberId, active: true, runId: "mock-1" } : null;
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
        this.participantTickets.clear();
        if (this.collabStatus) this.collabStatus = { ...this.collabStatus, active: false, state: "inactive" };
        this.emit("run.finished", { runId: "mock-1", status: "cancelled" });
        return {} as R;
      case "run.followUp": {
        const followParams = params as Api["run.followUp"]["params"];
        const { feedback } = followParams;
        if (this.agentProviderId) {
          if ("collabApprovalHandle" in followParams && followParams.collabApprovalHandle) throw new Error("Tek ajan akışında ortak bağlam henüz bağlı değil.");
          this.runCancelled = false;
          void this.streamAgentRun(feedback);
          return { runId: "mock-1" } as R;
        }
        const collabHandle = "collabApprovalHandle" in params ? params.collabApprovalHandle : undefined;
        if (collabHandle && !this.collabApprovals.has(collabHandle)) throw new Error("Ortak bağlam onayı geçersiz.");
        if (collabHandle) this.collabApprovals.delete(collabHandle);
        this.participantTickets.clear();
        if (this.collabStatus) this.collabStatus = { ...this.collabStatus, active: true, state: "streaming" };
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

  private async streamAgentRun(feedback?: string) {
    const proposalsEvent = RUN_FULL.find(([, ev]) => ev.type === "proposal")?.[1];
    const diffEvent = RUN_FULL.find(([, ev]) => ev.type === "diff")?.[1];
    if (!proposalsEvent || !diffEvent) return;
    if (feedback) this.emit("run.event", { runId: "mock-1", ev: { type: "followUpStarted", feedback } });
    this.emit("run.event", { runId: "mock-1", ev: { type: "stage", stage: "code", provider: this.agentProviderId } });
    this.emit("run.event", { runId: "mock-1", ev: { type: "output", stage: "code", text: feedback ? `Takip isteği: ${feedback}` : "MOCK ajan çalışması başladı. Gerçek sağlayıcı çalıştırması değildir." } });
    await new Promise((resolve) => setTimeout(resolve, 450));
    if (this.runCancelled) return;
    this.emit("run.event", { runId: "mock-1", ev: { type: "stage", stage: "verifying", provider: "MOCK" } });
    if (this.scenario === "running") return;
    const proposal = proposalsEvent.proposals as Proposal[];
    this.mockProposals = proposal;
    this.emit("run.event", { runId: "mock-1", ev: { ...diffEvent, type: "diff" } });
    const outcome = this.scenario === "agent-fail" ? "fail" : this.scenario === "agent-not-run" ? "not_run" : this.scenario === "agent-invalidated" ? "invalidated" : "pass";
    this.emit("run.event", { runId: "mock-1", ev: {
      type: "evidence", reason: "single_agent_proposal", execution_id: `mock-execution-${Date.now()}`,
      agent_message: "MOCK: tek ajan önerisi; proje doğrulama komutu çalıştırılmadı.",
      attempt_receipt: { model_turns: null, tool_calls: null }, changed_paths: proposal.map((item) => item.path), diff_sha256: "mock-diff-sha256",
      verification: { outcome, fingerprint_complete: outcome === "pass", changed_content: false, verification_id: "mock-verification", plan_id: "mock-plan", checks: [{ check_id: "mock-simulated", status: outcome === "pass" ? "pass" : outcome }] },
    } });
    this.emit("run.event", { runId: "mock-1", ev: { ...proposalsEvent, totals: { latency_s: null, tokens: null, cost_usd: null }, verdict: undefined } });
    this.emit("run.finished", { runId: "mock-1", status: "done", engine: "agent" });
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
    const seq =
      this.scenario === "running" ? RUN_PARTIAL : this.scenario === "nochanges" ? RUN_NO_CHANGES : RUN_FULL;
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
      this.participantTickets.clear();
      if (this.collabStatus) this.collabStatus = { ...this.collabStatus, active: false, state: "inactive" };
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
    this.participantTickets.clear();
    if (this.collabStatus) this.collabStatus = { ...this.collabStatus, active: false, state: "inactive" };
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
