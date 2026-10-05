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
  /** T1.2 — "auto" (git projelerinde izole pipeline motoru + doğrulama +
      inceleme, uygun değilse klasik motor) | "legacy" (her zaman klasik). */
  aiEngine: "auto" | "legacy";
  /** Kullanıcının Composer'dan seçtiği rol->sağlayıcı ataması; null ->
      run.providers'ın recommendedRouting'i kullanılır (bkz. state/run.ts). */
  routing: Routing | null;
  /** Jev System One karar katmanı (deneysel, bkz. decision_runtime/,
      engine_factory.py, decision_credentials.py): "off" (varsayılan,
      davranış bugünküyle bayt-bayt aynı) | "rules" (belirlenimci triage
      kuralları, hiçbir şey makineden çıkmaz) | "jev" (Jev/TypeSafe kararı —
      TYPESAFE_API_KEY gerekir; SDK yokken veya bağlantı/SDK hatasında
      belirlenimci kurallara düşer; DÜŞÜK güvenilirlikte ise kurallara
      düşmez — bugünkü davranış, yani düzeltme döngüsü sürer). */
  decisionLayer: "off" | "rules" | "jev";
}

export interface RunEvent {
  // project_runner.py olay sözlüğü DEĞİŞMEDEN forward edilir (doğrulandı :80–152)
  // "followUpStarted": F2 (takip isteği) — webhost/api/run.py'nin
  // run.followUp handler'ı, continuation'ı BAŞLATMADAN önce yayınlar; UI bu
  // olayla diff/proposal durumunu sıfırlar ve kullanıcının takip metnini
  // kendi mesajı olarak akışa (flow) ekler (bkz. state/run.ts consume()).
  // "summary": Worker'ın son mesajı (bkz. webhost/api/run.py
  // _last_worker_final_message) -- yalnızca değişiklik/öneri çıkmadığında
  // "info" ile birlikte gönderilir.
  type: "stage" | "info" | "output" | "metric" | "plan" | "diff" | "verdict" | "proposal" | "followUpStarted" | "summary" | "evidence";
  [key: string]: unknown;
}

export interface AgentEvidence {
  unknown: boolean;
  truncated: boolean;
  reason: string;
  execution_id: string;
  agent_message: string;
  attempt_receipt: { model_turns: number | null; tool_calls: number | null };
  changed_paths: string[];
  diff_sha256: string | null;
  verification: {
    outcome: "pass" | "fail" | "not_run" | "error" | "invalidated" | "timeout" | string;
    fingerprint_complete: boolean | null;
    changed_content: boolean | null;
    verification_id: string | null;
    plan_id: string | null;
    checks: { check_id: string; status: string }[];
  };
}

export interface CollaborationPreview {
  previewId: string;
  projectRoot: string;
  endpoint: string;
  memberId: string;
  taskId: string;
  sessionId: string;
  targetVersion: string;
  baseCommit: string;
  revision: string;
  task: { owner: string; goal: string; scopes: string[]; status: string; contextRevision: string };
  context: { goal: string; decisions: string[]; interfaces: Record<string, string> };
}

export interface CollaborationStatus {
  state: string;
  code: string | null;
  recoveryState?: string;
  recoveryCode?: string | null;
  consumedRevision: string | null;
  receivedRevision: string | null;
  pendingCount: number;
  sessionId: string;
  taskId: string;
  memberId: string;
  active: boolean | null;
  runId: string;
}

export interface ParticipantTaskStatusPreview {
  ticketId: string; runId: string; projectRoot: string; sessionId: string;
  taskId: string; memberId: string; fromStatus: string;
  targetStatus: "queued" | "running" | "waiting"; expectedRevision: string;
  contextHash: string; freshContextDiffersFromAccepted: boolean | null;
}

export interface OwnerTask {
  id: string; owner: string; goal: string; scopes: string[];
  status: "queued" | "running" | "waiting" | "done"; contextRevision: string;
}
export interface OwnerPreview {
  previewId: string; projectRoot: string; sessionId: string; targetVersion: string;
  baseCommit: string; goal: string; ownerId: string; memberIds: string[];
  tasks: OwnerTask[]; mode: "create"; warnings: string[];
}
export interface OwnerStatus {
  state: string; projectRoot: string | null; sessionId: string | null;
  targetVersion: string | null; baseCommit: string | null; revision: string | null;
  goal: string | null; ownerId: string | null; memberIds: string[]; tasks: OwnerTask[];
  storePath: string | null; hubPath: string | null; endpoint: string | null;
  epoch: number; exportedMembers: string[]; retryRequired: boolean; createdPaths: string[];
}
export interface ProductTask { id: string; owner: string; goal: string; scopes: string[]; status: "queued" | "running" | "waiting" | "done"; contextRevision: string }
export interface ProductBoard {
  projectRoot: string; sessionId: string; baseCommit: string; targetVersion: string;
  revision: string; contextHash: string; context: { goal: string; decisions: string[]; interfaces: Record<string, string> };
  ownerId: string; memberIds: string[]; tasks: ProductTask[];
  overlaps: { tasks: string[]; shared: string[] }[]; waitingTaskIds: string[]; epoch: number; state: string;
}
export interface ProductProposals {
  sessionId: string; baseCommit: string; revision: string; contextHash: string; epoch: number;
  proposals: { proposalId: string; proposalRevision: string; sessionId: string; baseCommit: string; taskId: string; owner: string; contextRevision: string; contextHash: string; fileCount: number; staleContext: boolean }[];
}
export interface ProductChangeEvent {
  fromRevision: string; toRevision: string; metadataCheckpoint: boolean;
  goalChanged: boolean; decisionsChanged: boolean;
  interfaces: { added: string[]; removed: string[]; changed: string[] };
  taskChanges: { taskId: string; change: "added" | "removed" | "changed"; previousStatus: ProductTask["status"] | null; status: ProductTask["status"] | null; previousOwner: string | null; owner: string | null; fields: string[] }[];
  taskChangeCount: number; taskChangesTruncated: boolean;
  affectedTaskIds: string[]; affectedTaskCount: number; affectedTasksTruncated: boolean; contextChanged: boolean;
}
export interface ProductChanges { sessionId: string; baseCommit: string; epoch: number; headRevision: string; lastRevision: string; hasMore: boolean; events: ProductChangeEvent[] }
export interface OwnerShare {
  endpoint: string; sessionId: string; baseCommit: string; targetVersion: string;
  memberId: string; credential: string; storePath: string; hubPath: string;
  taskIds: string[]; epoch: number; scope: "loopback-only";
}

export interface DeliveryPreview {
  previewId: string; proposalId: string; runId: string; sessionId: string; taskId: string; owner: string;
  baseCommit: string; contextRevision: string; contextHash: string; expectedRevision: string;
  paths: string[]; outOfScopePaths: string[]; fileCount: number; artifactBytes: number;
  contentDigest: string; warnings: string[];
}
export interface DeliveryProposal {
  proposalId: string; proposalRevision: string; taskId: string; owner: string; sessionId: string;
  baseCommit: string; contextRevision: string; contextHash: string; fileCount: number;
  currentContextHashMatches: boolean;
}
export interface DeliveryCandidate {
  session_id: string; session_revision: string; binding_revision: string; context_hash: string;
  base_commit: string; proposal_ids: string[];
  proposals: { proposal_id: string; task_id: string; owner: string; context_revision: string; paths: string[] }[];
  candidate_dir: string; file_count: number; content_fingerprint: string;
  verification: { status: "not_run" | "pass" | "fail" | "timeout" | "error" | "invalidated"; plan_id?: string | null; checks?: unknown[]; changed_content?: boolean; fingerprint_complete?: boolean; fingerprint_before?: unknown; fingerprint_after?: unknown };
  conflicts: string[]; notes: string[];
}

/** F1 (canlı ajan etkinliği) — run_runtime.activity_projection.project_event
    ile birebir eşleşir (bkz. webhost/api/activity.py). */
export type ActivityRole = "planner" | "worker" | "verification" | "reviewer" | "fix" | "system";
export type ActivityKind = "tool" | "model" | "check" | "stage" | "note" | "usage";
export type ActivityStatus = "running" | "ok" | "error" | "info";

export interface ActivityItem {
  /** aynı mantıksal öğe (ör. bir araç çağrısı) için sabit — yerinde güncelleme */
  id: string;
  runId: string;
  seq: number;
  ts: string;
  role: ActivityRole;
  kind: ActivityKind;
  status: ActivityStatus;
  title: string;
  detail?: string;
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

  // session (proje-içi .imece/session.json)
  "session.get": { params: {}; result: SessionData };
  "session.save": { params: SessionData; result: {} };

  // app
  "app.info": { params: {}; result: { version: string; platform: string; chromium: string } };
  "app.openExternal": { params: { url: string }; result: {} };
  "app.pickFolder": { params: { title?: string }; result: { path: string | null } };
  "app.revealInOS": { params: { path: string }; result: {} };
  "app.clipboardWrite": { params: { text: string }; result: {} };
  /** Beta-2: web hataları dosya log'una — istem/anahtar içeriği GÖNDERİLMEZ */
  "app.log": {
    params: { level: "info" | "warn" | "error"; message: string; stack?: string };
    result: { logPath: string };
  };

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
    result: { providers: ProviderInfo[]; recommendedRouting: Routing };
  };
  "collab.preview": { params: { endpoint: string; credential: string; memberId: string; taskId: string }; result: CollaborationPreview };
  "collab.approve": { params: { previewId: string; resetCursor?: boolean }; result: { approvalHandle: string; preview: CollaborationPreview; resetCursor: boolean } };
  "collab.discard": { params: { previewId?: string; approvalHandle?: string }; result: {} };
  "collab.delivery.preview": { params: { runId: string; storePath: string; hubPath: string; paths: string[]; proposalId?: string }; result: DeliveryPreview };
  "collab.delivery.publish": { params: { runId: string; previewId: string; allowOutOfScope?: boolean }; result: { proposalId: string; sessionRevision: string; proposalRevision: string; contextRevision: string; contextHash: string; paths: string[]; outOfScopePaths: string[]; outOfScopeAuthorized: boolean } };
  "collab.delivery.discard": { params: { previewId: string }; result: {} };
  "collab.delivery.list": { params: { runId: string; storePath: string; hubPath: string }; result: { proposals: DeliveryProposal[] } };
  "collab.delivery.candidate": { params: { runId: string; storePath: string; hubPath: string; proposalIds: string[]; outputPath: string; verify?: boolean }; result: { candidate: DeliveryCandidate | null; conflicts: string[] } };
  "collab.status": { params: { runId?: string }; result: { collaboration: CollaborationStatus | null } };
  "collab.taskStatus.preview": { params: { runId: string; targetStatus: "queued" | "running" | "waiting" }; result: ParticipantTaskStatusPreview };
  "collab.taskStatus.confirm": { params: { runId: string; ticketId: string; confirm: true }; result: { revision: string; taskId: string; status: "queued" | "running" | "waiting" } };
  "collab.taskStatus.discard": { params: { runId: string; ticketId: string }; result: {} };
  "collab.owner.previewCreate": { params: { sessionId: string; targetVersion: string; goal: string; ownerId: string; memberIds: string[]; tasks: { id: string; owner: string; goal: string; scopes: string[]; status?: "queued" | "running" | "waiting" }[] }; result: OwnerPreview };
  "collab.owner.create": { params: { previewId: string }; result: OwnerStatus };
  "collab.owner.select": { params: { storePath: string; hubPath: string; ownerId: string; memberIds: string[] }; result: OwnerStatus };
  "collab.owner.start": { params: { port?: number }; result: OwnerStatus };
  "collab.owner.stop": { params: {}; result: OwnerStatus };
  "collab.owner.status": { params: {}; result: OwnerStatus };
  "collab.owner.snapshot": { params: {}; result: ProductBoard };
  "collab.owner.updateContext": { params: { confirm: true; expectedRevision: string; expectedEpoch: number; expectedSessionId: string; context: ProductBoard["context"] }; result: { revision: string; sessionId: string; epoch: number; action: string } };
  "collab.owner.updateTaskStatus": { params: { confirm: true; expectedRevision: string; expectedEpoch: number; expectedSessionId: string; taskId: string; status: ProductTask["status"] }; result: { revision: string; sessionId: string; epoch: number; action: string; taskId: string; status: ProductTask["status"] } };
  "collab.owner.createTask": { params: { confirm: true; expectedRevision: string; expectedEpoch: number; expectedSessionId: string; task: { id: string; owner: string; goal: string; scopes: string[] } }; result: { revision: string; sessionId: string; epoch: number; action: "createTask"; taskId: string; status: "queued"; owner: string; contextRevision: string } };
  "collab.owner.proposals": { params: { expectedSessionId: string }; result: ProductProposals };
  "collab.owner.changes": { params: { expectedSessionId: string; afterRevision: string; limit?: number }; result: ProductChanges };
  "collab.owner.shareOnce": { params: { memberId: string; confirmSecret: true }; result: OwnerShare };
  "collab.owner.localPreview": { params: { memberId: string; taskId: string }; result: { preview: CollaborationPreview; storePath: string; hubPath: string; endpoint: string; memberId: string; taskId: string; epoch: number } };
  /** mentions: F6 (@-mentions) — Composer'da @ ile seçilen proje-göreli,
      forward-slash yollar (dosya veya klasör); en fazla 10. Sunucu bunları
      Project._safe ile bağımsızca doğrular — mevcut olmayan/proje dışına
      çıkan bir yol sessizce düşürülür ve bir "info" olayıyla bildirilir. */
  "run.start": { params: { task: string; providerId: string; mentions?: string[]; collabApprovalHandle?: never } | { task: string; routing: Routing; mentions?: string[]; collabApprovalHandle?: string }; result: { runId: string } };
  "run.cancel": { params: { runId?: string }; result: {} };
  /** F2 (takip isteği / follow-up): sadece bekleyen bir öneri (WAITING_USER,
      canlı worktree'li bir pipeline koşusu) varken geçerlidir; klasik
      motorda veya aktif koşu yokken BridgeError. */
  "run.followUp": { params: { feedback: string; mentions?: string[] } | { feedback: string; mentions?: string[]; collabApprovalHandle?: string }; result: { runId: string } };
  "run.applyProposals": {
    params: { paths: string[] };
    result: {
      applied: string[];
      errors: { path: string; message: string }[];
      /** koşu sırasında kullanıcı tarafından değiştirilmiş/oluşturulmuş/silinmiş dosyalar —
       * hiçbir şey yazılmadı, checkpoint oluşturulmadı, öneriler bekliyor kaldı. */
      conflicts?: { path: string; reason: string }[];
      checkpointId: string | null;
    };
  };
  "checkpoint.list": { params: {}; result: { checkpoints: Checkpoint[] } };
  "checkpoint.restore": { params: { checkpointId: string }; result: { restored: string[] } };
  "run.rejectProposals": { params: {}; result: {} };
  "history.list": { params: {}; result: { items: HistoryItem[] } };
  "receipt.get": { params: { receiptId: string }; result: { receipt: Receipt } };
  "receipt.export": { params: { receiptId: string; directory: string }; result: { path: string } };

  // ---- terminal (P3, ConPTY) ----
  "terminal.create": {
    params: { cwd?: string; cols: number; rows: number };
    result: { termId: string; shell?: string };
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

  // ---- exec / F5 çalıştır (P8.1) ----
  /** rel verilirse dosya, verilmezse proje komutu; command ile geçersiz kılınır */
  "exec.run": {
    params: { rel?: string | null; command?: string };
    result: { execId: string; command: string };
  };
  "exec.stop": { params: {}; result: {} };
  "exec.getCommand": { params: {}; result: { command: string | null; source?: string | null } };
  "exec.preflight": {
    params: { rel?: string | null; command?: string };
    result: { command: string; source: "explicit" | "file" | "project_config" | "detected"; cwd: string; fingerprint: string; requiresConfirmation: boolean };
  };
  "exec.approveCommand": { params: { rel?: string | null; command?: string }; result: {} };
  "exec.setCommand": { params: { command: string }; result: {} };

  // ---- keys (API anahtarları — sağlayıcı kataloğundan) ----
  /** anahtarlar UI'a dönmez; yalnız ok + maske. kind=cli → PATH varlık kontrolü.
      decisionProviders: Jev (TypeSafe) karar sağlayıcısı — normal katalogdan
      AYRI bir arayüz (ProviderInfo'ya karışmaz, yönlendirmeye katılmaz). */
  "keys.status": {
    params: {};
    result: {
      providers: Record<string, ProviderInfo>;
      decisionProviders: Record<string, DecisionProviderInfo>;
      envPath: string;
    };
  };
  /** { sağlayıcıId: anahtar } — yalnız katalogda anahtar isteyen id'ler + karar
      sağlayıcısı "typesafe" kabul edilir; Jev anahtarı yazdırılabilir ve
      boşluksuz olmalıdır */
  "keys.set": { params: Record<string, string>; result: {} };
  /** ucuz canlı doğrulama; key verilirse kaydetmeden dener. Normal
      sağlayıcılar GET /models, Jev (typesafe) salt SDK modeller-listesi
      testidir — proje içeriği taşımaz.
      code: "" | "auth" | "network" | "http" | "no_key" | "sdk_missing"
            | "invalid_key" | "sdk" */
  "keys.test": {
    params: { provider: string; key?: string };
    result: { ok: boolean; code: string; detail: string };
  };

  // ---- providers (sağlayıcı kataloğu — v0.4) ----
  "providers.list": {
    params: {};
    result: { providers: ProviderInfo[]; defaultRouting: Routing };
  };
  "providers.setModel": { params: { provider: string; model: string }; result: {} };
  /** özel OpenAI-uyumlu uç; id katalogla çakışamaz */
  "providers.addCustom": {
    params: { id: string; label: string; baseUrl: string; model: string };
    result: { provider: ProviderInfo };
  };
  "providers.removeCustom": { params: { provider: string }; result: {} };

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

  // ---- debug (P8.2 — debugpy/DAP) ----
  "debug.start": {
    params: { rel: string; breakpoints: { path: string; lines: number[] }[] };
    result: { started: boolean };
  };
  /** oturum aktifken breakpoint güncelle; dönen lines = doğrulanan satırlar */
  "debug.setBreakpoints": {
    params: { path: string; lines: number[] };
    result: { lines: number[] };
  };
  "debug.continue": { params: {}; result: {} };
  "debug.next": { params: {}; result: {} };
  "debug.stepIn": { params: {}; result: {} };
  "debug.stepOut": { params: {}; result: {} };
  "debug.stack": { params: {}; result: { frames: DebugFrame[] } };
  "debug.scopes": { params: { frameId: number }; result: { scopes: DebugScope[] } };
  "debug.variables": { params: { ref: number }; result: { variables: DebugVariable[] } };
  "debug.evaluate": {
    params: { expr: string; frameId?: number };
    result: { result: string; ref: number };
  };
  "debug.stop": { params: {}; result: {} };
  "debug.status": { params: {}; result: { active: boolean; stopped: boolean } };

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

// ---- debug tipleri (P8.2) ----
export interface DebugFrame {
  id: number;
  name: string;
  path: string; // köke göre; kök dışıysa mutlak
  line: number;
}

export interface DebugScope {
  name: string;
  ref: number; // variablesReference
  expensive: boolean;
}

export interface DebugVariable {
  name: string;
  value: string;
  type: string;
  ref: number; // >0 → genişletilebilir (debug.variables ile çocuklar)
}

export interface SearchMatch {
  path: string;
  line: number;
  col: number;
  preview: string;
}

export type Role = "planner" | "coder" | "reviewer";
export type Routing = Record<Role, string>;

/** sağlayıcı kataloğu girdisi (keys.status + providers.list ortak şekli).
    Anahtar değeri hiçbir alanda taşınmaz. */
export interface ProviderInfo {
  id: string;
  label: string;
  /** cli = hesap girişi (ör. Claude Code, Codex, Gemini CLI'ın OAuth girişi);
      openai/anthropic = API anahtarıyla erişilen sağlayıcılar. */
  kind: "openai" | "anthropic" | "cli";
  custom: boolean;
  ok: boolean;
  docsUrl: string;
  /** kind=cli: bulunan yol veya "bulunamadı" açıklaması */
  detail?: string;
  /** kind=cli: CLI ikili dosyası PATH'te bulundu mu */
  cliAvailable?: boolean;
  /** kind=cli: `npx` PATH'te mi (ACP hesap girişi Node.js gerektirir) */
  npxAvailable?: boolean;
  /** yeni (pipeline) motor bu sağlayıcıyı sürebiliyor mu — değilse Composer
      "klasik motor" ipucu gösterir (bkz. engine_factory.role_supported) */
  engineSupported?: boolean;
  engineReason?: string;
  /** kind=openai|anthropic */
  model?: string;
  models?: string[];
  keyHint?: string;
  keyless?: boolean;
  masked?: string;
}

/** Jev karar katmanı sağlayıcısı — ProviderInfo kataloğundan BİLEŞİK DEĞİLDİR
    (keys.status decisionProviders alanında ayrı döner; planner/coder/reviewer
    yönlendirmesine katılmaz). `ok` yalnız anahtarın KAYITLI olduğunu belirtir:
    SDK kurulu mu (sdkAvailable) ve bağlantı canlı mı ayrı durumlardır —
    "anahtar var" asla "doğrulandı" anlamına gelmez. Anahtar değeri hiçbir
    alanda taşınmaz; yalnız son dört haneli `masked`. */
export interface DecisionProviderInfo {
  id: string;
  label: string;
  ok: boolean;
  masked?: string;
  keyHint?: string;
  docsUrl?: string;
  /** TypeSafe SDK kurulu mu (anahtar testi için gerekir) */
  sdkAvailable?: boolean;
}

export interface HistoryItem {
  ts: number;
  task: string;
  verdict: string;
  tokens: number;
  cost_usd: number;
  files: string[];
  receipt_id?: string | null;
  status?: string;
}
export interface Receipt {
  id: string;
  createdAt: number;
  finishedAt: number;
  status: string;
  task: string;
  routing: Routing;
  plan: { summary: string; files: string[] } | null;
  proposals: { path: string; is_new: boolean; diff: string }[];
  review: { verdict: string; note: string };
  metrics: { latency_s: number; tokens: number; cost_usd: number };
  applied: string[];
  rejected: string[];
  checkpointId: string | null;
  verification: { status: string; detail: string };
  error?: string;
}
export interface Checkpoint {
  id: string;
  ts: number;
  runId?: string | null;
  files: string[];
}

export interface Proposal {
  path: string;
  new: string;
  diff: string;
  is_new: boolean;
  /** yeni (pipeline) motor: dosya izole çalışma alanında silinmiş. */
  is_deleted?: boolean;
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
  /** F2: `engine`, bu koşunun GERÇEKTE hangi motorla yürütüldüğünü taşır
      ("pipeline" | "legacy") -- Composer'ın takip isteği modunu yalnızca
      pipeline motorunda açması için (bkz. state/run.ts).
      A5 (hata UX): status "failed" olduğunda `errorCode`/`errorTitle`/
      `errorDescription`, webhost/api/run.py'nin TEK Türkçe eşleme
      noktasından (_ERROR_MESSAGES/_error_details) gelir -- `error` ham
      metin olarak "Ayrıntılar" için saklanır. */
  "run.finished": {
    runId: string; status: "done" | "failed" | "cancelled"; error?: string;
    errorCode?: string; errorTitle?: string; errorDescription?: string;
    engine?: "agent" | "pipeline" | "legacy";
  };
  /** F1 (canlı ajan etkinliği) — run_runtime.activity_projection.project_event
      çıktısıyla birebir; `id` aynı öğenin (ör. bir araç çağrısı) sonraki
      durum güncellemeleri için sabit kalır (update-in-place). */
  "run.activity": ActivityItem;
  "terminal.data": { termId: string; data: string };
  "terminal.exit": { termId: string; code: number };
  "search.results": { searchId: string; matches: SearchMatch[] };
  "search.done": { searchId: string; total: number; limitHit: boolean };
  /** LSP sunucu bildirimleri (publishDiagnostics, $/imeceReady, $/imeceExited) */
  "lsp.event": { method: string; params: unknown };
  "exec.output": { execId: string; data: string };
  "exec.exited": { execId: string; code: number; durationS: number };
  /** Beta-2: yakalanmamış Python istisnası — UI hata toast'ı gösterir */
  "app.error": { message: string; logPath: string };
  /** debug (P8.2): durdu (top frame + yığın dahil — ekstra round-trip yok) */
  "debug.stopped": {
    reason: string;
    threadId: number;
    path: string;
    line: number;
    frames: DebugFrame[];
  };
  "debug.continued": {};
  "debug.output": { data: string };
  "debug.terminated": { code: number | null; durationS: number };
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
