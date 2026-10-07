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
  await page.goto(`${origin}/?scenario=empty`);
  await page.waitForFunction(() => window.__imece?.bridge);
  await page.evaluate(() => window.__imece.openProject("C:/M4/owner"));
  const fixture = await page.evaluate(async () => {
    const { useOwner } = await import("/src/state/owner.ts");
    const bridge = window.__imece.bridge;
    const originalCall = bridge.call.bind(bridge);
    const root = "C:/M4/owner";
    const status = { state: "configured", projectRoot: root, sessionId: "m4-session", targetVersion: "v1", baseCommit: "a".repeat(40), revision: "b".repeat(40), goal: "Coordinate safely", ownerId: "owner", memberIds: ["owner", "member-1"], tasks: [], storePath: "", hubPath: "", endpoint: null, epoch: 1, exportedMembers: [], retryRequired: false, createdPaths: [], transportMode: "loopback" };
    const data = { calls: [], status, resolveInvite: null, resolveStatus: null, deferStatus: false, clipboard: [] };
    window.__m4OwnerFixture = data;
    useOwner.setState({ status, draftRoot: root, busy: false, error: null });
    bridge.call = (method, params) => {
      data.calls.push({ method, params, root: window.__imece.bridge === bridge ? document.querySelector("[aria-label='Sahip oturumu']")?.textContent : "" });
      if (method === "collab.owner.status") {
        if (data.deferStatus) return new Promise((resolve) => { data.resolveStatus = resolve; });
        return Promise.resolve(data.status);
      }
      if (method === "collab.owner.startLAN") {
        data.status = { ...data.status, state: "running", transportMode: "lan", controlEndpoint: "https://192.168.1.10:41001", proposalEndpoint: "https://192.168.1.10:41002", certificateSha256: "c".repeat(64), epoch: 2 };
        return Promise.resolve(data.status);
      }
      if (method === "collab.owner.issueInvite") return new Promise((resolve) => { data.resolveInvite = resolve; });
      if (method === "app.clipboardWrite") { data.clipboard.push(params.text); return Promise.resolve({}); }
      if (method === "collab.owner.stop") { data.status = { ...data.status, state: "stopped", transportMode: "loopback" }; return Promise.resolve(data.status); }
      if (method === "collab.owner.cancelInvite") return Promise.resolve({});
      return originalCall(method, params);
    };
    return { root };
  });
  await page.getByRole("tab", { name: "Oturum" }).click();
  await page.waitForSelector("[aria-label='Sahip oturumu']");
  const auto = await page.evaluate(() => window.__m4OwnerFixture.calls.filter((call) => call.method === "collab.owner.startLAN").length);
  if (auto !== 0) throw new Error("LAN listener started without an explicit owner action");
  await page.locator("details summary").getByText("LAN TLS sunucusunu açıkça yapılandır").click();
  await page.getByLabel("Özel IPv4 adresi").fill("192.168.1.10");
  await page.getByLabel("TLS sertifika PEM yolu").fill("C:/certs/owner.pem");
  await page.getByLabel("TLS özel anahtar PEM yolu").fill("C:/certs/owner-key.pem");
  // An older status response must not overwrite the later mutation receipt.
  await page.evaluate(async () => {
    const { useOwner } = await import("/src/state/owner.ts");
    window.__m4OwnerFixture.deferStatus = true;
    void useOwner.getState().refresh();
  });
  await page.waitForFunction(() => !!window.__m4OwnerFixture.resolveStatus);
  await page.getByRole("button", { name: "İki TLS listener’ı başlat" }).click();
  await page.getByText("Kontrol TLS:").waitFor();
  await page.evaluate(() => {
    const fixture = window.__m4OwnerFixture;
    fixture.deferStatus = false;
    fixture.resolveStatus({ ...fixture.status, state: "configured", transportMode: "loopback", epoch: 1 });
  });
  await page.waitForTimeout(50);
  await page.getByText("Kontrol TLS:").waitFor();
  let state = await page.evaluate(() => ({ calls: window.__m4OwnerFixture.calls, status: window.__imece.bridge }));
  if (state.calls.filter((call) => call.method === "collab.owner.startLAN").length !== 1) throw new Error("explicit TLS start was not sent exactly once");
  await page.getByLabel("Davet edilecek yapılandırılmış üye").selectOption("member-1");
  await page.getByRole("button", { name: "Tek kullanımlık davet üret" }).click();
  await page.waitForFunction(() => !!window.__m4OwnerFixture.resolveInvite);
  await page.evaluate(() => window.__m4OwnerFixture.resolveInvite({ memberId: "member-1", code: "SECRET_ONE_USE_CODE", expiresInSeconds: 300, controlEndpoint: "https://192.168.1.10:41001", proposalEndpoint: "https://192.168.1.10:41002", certificateSha256: "c".repeat(64), sessionId: "m4-session", epoch: 2 }));
  await page.getByText(/Davet kodu 300 saniye/).waitFor();
  if (await page.locator("input").evaluateAll((inputs) => inputs.some((input) => input.value.includes("SECRET_ONE_USE_CODE")))) throw new Error("invite code was revealed before confirmation");
  await page.getByLabel("Davet bilgisini açıkça gösterip paylaşmayı onaylıyorum.").check();
  await page.waitForFunction(() => [...document.querySelectorAll("input")].some((input) => input.value.includes("SECRET_ONE_USE_CODE")));
  await page.getByRole("button", { name: "Davet paketini panoya kopyala" }).click();
  await page.getByRole("status").filter({ hasText: "Davet panoya kopyalandı" }).waitFor();
  state = await page.evaluate(() => ({ clipboard: window.__m4OwnerFixture.clipboard, text: document.body.innerText }));
  if (state.clipboard.length !== 1 || !state.clipboard[0].includes("SECRET_ONE_USE_CODE")) throw new Error("explicit invite copy did not carry the one-use bundle");
  if (!state.text.includes("192.168.1.10:41002") || !state.text.includes("c".repeat(64))) throw new Error("TLS endpoint or certificate pin is not shown");
  // A late invite response must not expose a code after switching projects.
  await page.getByLabel("Davet edilecek yapılandırılmış üye").selectOption("member-1");
  await page.getByRole("button", { name: "Tek kullanımlık davet üret" }).click();
  await page.waitForFunction(() => !!window.__m4OwnerFixture.resolveInvite);
  await page.evaluate(async () => {
    await window.__imece.openProject("C:/M4/other");
    await window.__imece.openProject("C:/M4/owner");
  });
  await page.evaluate(() => window.__m4OwnerFixture.resolveInvite({ memberId: "member-1", code: "LATE_SECRET", expiresInSeconds: 300, controlEndpoint: "https://192.168.1.10:41001", proposalEndpoint: "https://192.168.1.10:41002", certificateSha256: "c".repeat(64), sessionId: "m4-session", epoch: 2 }));
  await page.waitForTimeout(100);
  if (await page.locator("input").evaluateAll((inputs) => inputs.some((input) => input.value.includes("LATE_SECRET")))) throw new Error("late invite crossed the A→B→A project fence");
  const safety = await page.evaluate(() => ({
    calls: window.__m4OwnerFixture.calls,
    persisted: JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }),
  }));
  if (!safety.calls.some((call) => call.method === "collab.owner.cancelInvite" && call.params.code === "LATE_SECRET" && call.params.expectedEpoch === 2)) throw new Error("stale invite did not cancel its own nonce");
  if (safety.calls.some((call) => call.method === "collab.owner.revokeMember")) throw new Error("stale invite indiscriminately revoked a member");
  if (/SECRET_ONE_USE_CODE|LATE_SECRET/.test(safety.persisted)) throw new Error("invite secret was persisted in browser storage");
  if (await page.evaluate(() => document.documentElement.scrollWidth > 320)) throw new Error("owner LAN controls overflow at 320px");
  if (errors.length) throw new Error(`Browser errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({ ok: true, checks: ["LAN remains opt-in", "explicit TLS owner start", "certificate pin and separate endpoints visible", "invite remains gated until explicit reveal", "explicit clipboard copy", "old snapshot cannot overwrite LAN mutation", "A→B→A response fenced", "stale nonce cleanup never revokes replacement member", "no persisted invitation secrets", "320px layout"], errors }));
} finally {
  if (browser) await browser.close();
  vite.kill("SIGTERM");
}
