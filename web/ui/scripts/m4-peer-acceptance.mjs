import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const cwd = fileURLToPath(new URL("..", import.meta.url));
const executablePath = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const reservation = createServer();
await new Promise((resolve, reject) => reservation.once("error", reject).listen(0, "127.0.0.1", resolve));
const { port } = reservation.address();
await new Promise((resolve) => reservation.close(resolve));
const vite = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd, stdio: "ignore" });
let browser;
try {
  const origin = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let i = 0; i < 100 && !ready; i++) {
    if (vite.exitCode !== null) throw new Error(`Vite exited ${vite.exitCode}`);
    try { ready = (await fetch(origin)).ok; } catch { await delay(100); }
  }
  if (!ready) throw new Error("Vite did not become ready");
  browser = await chromium.launch({ headless: true, executablePath });
  const context = await browser.newContext({ viewport: { width: 320, height: 800 } });
  await context.route("**/*", (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/?scenario=project`);
  await page.waitForFunction(() => window.__imece?.bridge);
  await page.evaluate(() => window.__imece.ui().toggleAiPanel());
  await page.getByRole("tab", { name: "Oturum" }).click();
  await page.getByText("Katılımcı oturumuna katıl").click();
  const invite = { memberId: "peer-1", code: "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-ABC", expiresInSeconds: 120, controlEndpoint: "https://192.168.1.10:41001", proposalEndpoint: "https://192.168.1.10:41002", certificateSha256: "a".repeat(64), sessionId: "peer-session", epoch: 3 };
  await page.getByLabel("Davet paketi").fill(JSON.stringify(invite));
  await page.getByLabel("Ayrı kanaldan doğrulanan PIN").fill("a".repeat(64));
  await page.getByText(`Davet parmak izi SHA-256: ${"a".repeat(64)}`).waitFor();
  const pair = page.getByRole("button", { name: "Eşleştir" });
  if (await pair.isEnabled()) throw new Error("pair allowed before out-of-band certificate confirmation");
  await page.getByLabel("Sertifika SHA-256 parmak izini ayrı güvenlik kanalından doğruladım.").check();
  const invalidInvite = { ...invite, controlEndpoint: "https://192.0.2.1:41001" };
  await page.getByLabel("Davet paketi").fill(JSON.stringify(invalidInvite));
  await page.getByLabel("Sertifika SHA-256 parmak izini ayrı güvenlik kanalından doğruladım.").check();
  await pair.click();
  await page.getByText(/Eşleştirmenin sonucu belirsiz olabilir/).waitFor();
  if (await page.getByText(/Durum: active · peer-1/).count()) throw new Error("mock accepted mutated public-IP invite DTO");
  await page.getByLabel("Davet paketi").fill(JSON.stringify(invite));
  await page.getByLabel("Ayrı kanaldan doğrulanan PIN").fill("a".repeat(64));
  await page.getByLabel("Sertifika SHA-256 parmak izini ayrı güvenlik kanalından doğruladım.").check();
  await pair.click();
  await page.getByText(/Durum: active · peer-1/).waitFor();
  await page.getByText("Mock shared goal").waitFor();
  await page.locator("p").filter({ hasText: "Mock task" }).waitFor();
  await page.getByRole("tab", { name: "Çalışma" }).click();
  await page.getByRole("tab", { name: "Oturum" }).click();
  await page.getByText("Katılımcı oturumuna katıl").click();
  await page.getByText(/Durum: active · peer-1/).waitFor();
  await page.getByRole("button", { name: "Durumu yenile" }).click();
  await page.getByText("Katılımcı metadata yenilendi.").waitFor();
  // Explicit publication, no hidden sends, and same-ID ambiguity across tab remounts.
  await page.evaluate(() => {
    const bridge = window.__imece.bridge, original = bridge.call.bind(bridge);
    window.__shareCalls = [];
    const preview = { ticketId: "c".repeat(32), proposalId: "proposal-fixed", taskId: "mock-task", owner: "peer-1", sessionId: "peer-session", contextRevision: "1".repeat(40), contextHash: "2".repeat(64), paths: ["a.txt"], outOfScopePaths: [], fileCount: 1, artifactSha256: "3".repeat(64), sourceRunId: null, state: "preview" };
    bridge.call = (method, params) => {
      if (!method.startsWith("collab.peer.") || !method.endsWith("Proposal")) return original(method, params);
      window.__shareCalls.push({ method, params });
      if (method === "collab.peer.previewProposal") return Promise.resolve(preview);
      if (method === "collab.peer.publishProposal") return Promise.reject({ code: "peer_publish_uncertain" });
      if (method === "collab.peer.reconcileProposal") return Promise.resolve({ ...preview, state: "published" });
      if (method === "collab.peer.discardProposal") return Promise.resolve({});
      if (method === "collab.peer.fetchProposal") return Promise.resolve(preview);
      throw new Error("Unknown proposal route");
    };
  });
  await page.getByLabel("Size atanmış ortak görev").selectOption("mock-task");
  await page.getByLabel(/Açıkça seçilen göreli dosyalar/).fill("a.txt");
  await page.getByRole("button", { name: "Seçili öneriyi önizle" }).click();
  const publish = page.getByRole("button", { name: "Öneriyi açıkça paylaş" });
  await publish.waitFor();
  if (await publish.isEnabled()) throw new Error("share consent was bypassed");
  if (await page.evaluate(() => window.__shareCalls.some((c) => c.method === "collab.peer.publishProposal"))) throw new Error("preview automatically published code");
  await page.getByLabel("Bu dosyaların içeriğini seçtiğim ekip oturumuyla olduğu gibi paylaşmayı onaylıyorum.").check();
  await publish.click();
  await page.getByRole("button", { name: "Aynı öneri kimliğini uzlaştır" }).waitFor();
  await page.getByRole("tab", { name: "Çalışma" }).click();
  await page.getByRole("tab", { name: "Oturum" }).click();
  await page.getByText("Katılımcı oturumuna katıl").click();
  await page.getByRole("button", { name: "Aynı öneri kimliğini uzlaştır" }).click();
  await page.getByRole("button", { name: "Yeni açık dosya seçimine dön" }).waitFor();
  const shareCalls = await page.evaluate(() => window.__shareCalls);
  if (shareCalls.filter((c) => c.method === "collab.peer.publishProposal").length !== 1 || shareCalls.find((c) => c.method === "collab.peer.reconcileProposal").params.ticketId !== "c".repeat(32)) throw new Error("ambiguous publication was blindly retried or its identity was lost");
  await page.getByRole("button", { name: "Yeni açık dosya seçimine dön" }).click();
  const width = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }));
  if (width.content > width.viewport) throw new Error("participant delivery overflow at 320px");
  await page.getByRole("button", { name: "Bağlantıyı kes" }).click();
  await page.getByText("Katılımcı bağlantısı kesildi.").waitFor();
  // Hold a pairing response across an A→B→A workspace generation change.
  await page.getByLabel("Davet paketi").fill(JSON.stringify(invite));
  await page.getByLabel("Ayrı kanaldan doğrulanan PIN").fill("a".repeat(64));
  await page.getByLabel("Sertifika SHA-256 parmak izini ayrı güvenlik kanalından doğruladım.").check();
  await page.evaluate(() => {
    const bridge = window.__imece.bridge, original = bridge.call.bind(bridge);
    window.__peerOriginalCall = original;
    window.__peerDisconnects = [];
    bridge.call = (method, params) => method === "collab.peer.join"
      ? new Promise((resolve) => { window.__peerPendingPair = { resolve, params }; })
      : method === "collab.peer.disconnect"
        ? (window.__peerDisconnects.push(params.peerHandle), original(method, params))
        : original(method, params);
  });
  await pair.click();
  await page.waitForFunction(() => !!window.__peerPendingPair);
  if (await pair.isEnabled()) throw new Error("pair button remained enabled during admission");
  if (await page.getByLabel("Davet paketi").inputValue() || await page.getByLabel("Ayrı kanaldan doğrulanan PIN").inputValue()) throw new Error("invite/PIN remained in the UI after admission");
  await page.evaluate(async () => {
    await window.__imece.openProject("C:/M4/other");
    await window.__imece.openProject("C:/Projeler/demo-api");
    const pending = window.__peerPendingPair;
    pending.resolve({ peerHandle: "stale-peer-handle", state: "active", memberId: "peer-1", sessionId: "peer-session", projectRoot: "C:/Projeler/demo-api", epoch: 3, context: { goal: "stale" }, tasks: [] });
  });
  await page.waitForFunction(() => window.__peerDisconnects.length === 1);
  await page.evaluate(() => { window.__imece.bridge.call = window.__peerOriginalCall; });
  await page.waitForTimeout(100);
  if (await page.getByText(/Durum: active · peer-1/).count()) throw new Error("stale pairing crossed the project generation fence");
  const cleaned = await page.evaluate(() => window.__peerDisconnects);
  if (JSON.stringify(cleaned) !== JSON.stringify(["stale-peer-handle"])) throw new Error(`stale join cleanup did not target its exact handle: ${JSON.stringify(cleaned)}`);
  const safety = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  if (/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-ABC|246810/.test(safety)) throw new Error("invite or PIN persisted to browser storage");
  if (errors.length) throw new Error(`Browser errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({ ok: true, checks: ["invite paste", "out-of-band pin gate", "busy admission", "invite/PIN cleared at admission", "pair", "unmount/remount preserves participant session", "refresh", "explicit file selection and sharing consent", "no automatic publication", "same-ID reconciliation survives remount", "320px delivery layout", "disconnect", "exact-handle stale cleanup", "no persisted secrets"], errors }));
} finally {
  if (browser) await browser.close();
  vite.kill("SIGTERM");
}
