import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const cwd = fileURLToPath(new URL("..", import.meta.url));
const executablePath = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const screenshotDir = process.env.M2_SCREENSHOT_DIR || join(tmpdir(), "opencode");
await mkdir(screenshotDir, { recursive: true });
const reservation = createServer();
await new Promise((resolve, reject) => reservation.once("error", reject).listen(0, "127.0.0.1", resolve));
const { port } = reservation.address();
await new Promise((resolve) => reservation.close(resolve));
const vite = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd, stdio: "ignore" });
let browser;
const checks = [];
const consoleIssues = [];
const pageErrors = [];
const knownMonacoCancellation = [];
const pass = (name) => checks.push(name);
const waitForRun = (page, id, predicate, message, timeout = 7000) => page.waitForFunction(
  ([runId, source]) => {
    const run = window.__imece.run().runs[runId];
    return run && (source === "running" ? run.status === "running" : source === "cancelled" ? run.runStage === "cancelled" : source === "ready" ? run.runStage === "ready" : source === "applied" ? run.runStage === "applied" : source === "restored" ? run.runStage === "restored" : source === "rejected" ? run.runStage === "draft" && !run.proposals.length && !run.agentEvidence : false);
  }, [id, predicate], { timeout }).catch((error) => { throw new Error(`${message}: ${error.message}`); });

try {
  const origin = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let i = 0; i < 100 && !ready; i++) {
    if (vite.exitCode !== null) throw new Error(`Vite exited with ${vite.exitCode}`);
    try { ready = (await fetch(origin)).ok; } catch { await delay(100); }
  }
  if (!ready) throw new Error("Vite did not become ready");
  browser = await chromium.launch({ headless: true, executablePath });
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
  await context.route("**/*", (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() !== "warning" && message.type() !== "error") return;
    const line = `${message.type()}: ${message.text()}`;
    if (/Monaco.*Canceled|Canceled.*Monaco|Canceled:.*editor/i.test(line)) knownMonacoCancellation.push(line);
    else consoleIssues.push(line);
  });
  page.on("pageerror", (error) => pageErrors.push(error.stack || error.message));
  await page.goto(`${origin}/?scenario=m2-acceptance`);
  await page.waitForFunction(() => window.__imece?.bridge);
  await page.evaluate(() => window.__imece.openProject("C:/Projeler/demo-api"));
  await page.getByRole("heading", { name: "Görevler" }).first().waitFor();
  if (!(await page.getByRole("navigation", { name: "Çalışma alanı görünümü" }).getByText("Görevler").count())) throw new Error("Default task-first navigation is missing");
  pass("default task-first workspace");

  const taskInput = () => page.getByRole("textbox", { name: "Görev" });
  const startButton = () => page.getByRole("button", { name: "Çalıştır", exact: true }).last();
  const startTask = async (task) => {
    await taskInput().fill(task);
    await startButton().click();
    await page.waitForFunction((text) => Object.values(window.__imece.run().runs).some((run) => run.task === text && run.status === "running"), task, { timeout: 3000 });
    return page.evaluate((text) => Object.values(window.__imece.run().runs).find((run) => run.task === text)?.runId, task);
  };
  const a = await startTask("A: cancel while B continues");
  await page.getByRole("button", { name: "Yeni görev", exact: true }).click();
  const b = await startTask("B: follow up and review");
  if (!a || !b || a === b) throw new Error("A/B controls did not produce distinct run IDs");
  await waitForRun(page, a, "running", "A was not running after creation");
  await waitForRun(page, b, "running", "B was not running after creation");
  if ((await page.getByText("2/2 etkin veya bekleyen").count()) !== 1) throw new Error("Task list did not show both active capacity slots");
  pass("A and B started from UI controls while both are running");

  await page.getByRole("button", { name: "Yeni görev", exact: true }).click();
  await taskInput().fill("third: must be refused");
  await startButton().click();
  await page.getByText(/en fazla iki ajan koşusu/i).waitFor({ timeout: 3000 });
  const noThird = await page.evaluate(() => Object.values(window.__imece.run().runs).filter((run) => run.task === "third: must be refused").length === 0);
  if (!noThird) throw new Error("Capacity-refused third request was incorrectly recorded as admitted");
  pass("third request surfaces run_capacity and is not admitted");

  await page.getByRole("button", { name: /A: cancel while B continues/ }).click();
  await page.getByRole("button", { name: "Koşuyu durdur" }).click();
  await page.waitForFunction((runId) => window.__imece.run().runs[runId]?.status === "cancelled", a, { timeout: 4000 }).catch(async (error) => {
    const current = await page.evaluate((runId) => window.__imece.run().runs[runId], a);
    throw new Error(`A cancel did not settle (${JSON.stringify(current)}): ${error.message}`);
  });
  await waitForRun(page, b, "running", "Cancelling A interrupted B");
  await page.getByRole("button", { name: /B: follow up and review/ }).click();
  await page.getByRole("tab", { name: "Etkinlik" }).click();
  await page.getByText("MOCK worker simülasyonu").waitFor({ timeout: 3000 });
  const bActivity = await page.evaluate(async (runId) => {
    const { useActivity } = await import("/src/state/activity.ts");
    return useActivity.getState().items.some((item) => item.runId === runId && item.title.includes("MOCK worker"));
  }, b);
  if (!bActivity) throw new Error("B activity was not independent of cancelled A activity");
  pass("A cancellation leaves B running with independent activity");

  await waitForRun(page, b, "ready", "B did not reach proposal review", 15000);
  const firstEvidence = await page.evaluate((runId) => window.__imece.run().runs[runId]?.agentEvidence?.execution_id, b);
  if (!firstEvidence) throw new Error("B has no first-execution evidence");
  await page.getByRole("tab", { name: "Çalışma" }).click();
  const followup = page.getByRole("textbox", { name: "Takip isteği" });
  await followup.fill("Trim surrounding whitespace before parsing");
  await page.getByRole("button", { name: "Takip isteği gönder" }).click();
  await waitForRun(page, b, "running", "B follow-up did not continue the original run");
  const preservedId = await page.evaluate((runId) => window.__imece.run().selectedRunId === runId, b);
  if (!preservedId) throw new Error("B follow-up changed run identity");
  await waitForRun(page, b, "ready", "B follow-up did not produce a new ready result", 15000);
  const secondEvidence = await page.evaluate((runId) => window.__imece.run().runs[runId]?.agentEvidence?.execution_id, b);
  if (!secondEvidence || secondEvidence === firstEvidence) throw new Error("B follow-up did not replace evidence with a new execution ID");
  pass("B follow-up stays on the same ID and updates execution evidence");

  await page.getByRole("tab", { name: "Etkinlik" }).click();
  await page.getByRole("tab", { name: "Sonuç" }).click();
  await page.getByText(/MOCK senaryosu/).waitFor();
   await page.screenshot({ path: join(screenshotDir, "imece-m2-tasks-desktop.png"), fullPage: false });
  const proposal = page.getByRole("button", { name: /src\/utils\.ts/ });
  await proposal.click();
  await page.getByRole("navigation", { name: "Çalışma alanı görünümü" }).getByText("Araçlar").waitFor();
  await page.getByRole("button", { name: "Görevler" }).click();
  const retained = await page.evaluate(([runId, aId]) => {
    const state = window.__imece.run();
    return state.selectedRunId === runId && !!state.runs[runId] && state.runs[aId]?.runStage === "cancelled";
  }, [b, a]);
  if (!retained) throw new Error("Opening diff/tools and returning lost selected/background task data");
  pass("selected diff opens tools and return preserves A/B records");

  await page.setViewportSize({ width: 320, height: 720 });
  await page.getByRole("tab", { name: "Sonuç" }).click();
  const mobileEvidence = page.getByText(/MOCK senaryosu/);
  await mobileEvidence.scrollIntoViewIfNeeded();
  if (!(await mobileEvidence.isVisible())) throw new Error("Mobile result evidence is not reachable");
  const mobileApply = page.getByRole("button", { name: /Uygula \(1\)/ });
  await mobileApply.scrollIntoViewIfNeeded();
  if (!(await mobileApply.isVisible()) || !(await mobileApply.isEnabled())) throw new Error("Mobile apply action is not reachable/enabled");
   await page.screenshot({ path: join(screenshotDir, "imece-m2-tasks-320px.png"), fullPage: false });
  const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  if (dimensions.scroll > dimensions.client) throw new Error(`320px viewport overflows horizontally: ${JSON.stringify(dimensions)}`);
  await mobileApply.click();
  await waitForRun(page, b, "applied", "Applying B did not settle", 5000);
  const checkpoint = await page.evaluate(async (runId) => {
    const state = window.__imece.run().runs[runId];
    const { checkpoints } = await window.__imece.bridge.call("checkpoint.list", {});
    return state?.task === "B: follow up and review" && state.checkpointId && checkpoints.some((item) => item.id === state.checkpointId && item.runId === runId);
  }, b);
  if (!checkpoint) throw new Error("Applied checkpoint was not sourced from B while preserving its origin task");
  pass("320px exposes evidence and apply; B apply records its sourced checkpoint");

  await page.getByRole("button", { name: "Geri al", exact: true }).click();
  await page.getByRole("dialog", { name: "Checkpoint'e geri dön" }).waitFor();
  await page.getByRole("dialog").getByRole("button", { name: "Geri Al" }).click();
  await waitForRun(page, b, "restored", "B restore did not settle", 5000);
  pass("checkpoint restore uses real confirmation dialog");

  await page.getByRole("button", { name: "Yeni görev", exact: true }).click();
  const c = await startTask("C: targeted reject");
  if (!c || c === b) throw new Error("C was not a distinct task");
  await waitForRun(page, c, "ready", "C did not reach review", 15000);
  await page.getByRole("tab", { name: "Sonuç" }).click();
  await page.getByRole("button", { name: "Vazgeç", exact: true }).click();
  await waitForRun(page, c, "rejected", "C proposals/evidence were not rejected", 4000);
  const cCleared = await page.evaluate((runId) => {
    const run = window.__imece.run().runs[runId];
    return run?.proposals.length === 0 && run.diffs.length === 0 && run.agentEvidence === null;
  }, c);
  if (!cCleared) throw new Error("Rejecting C left stale proposal/evidence data");
  await page.getByRole("button", { name: "Yenile" }).click();
  await page.waitForFunction((id) => window.__imece.run().runs[id]?.uncertain === false && window.__imece.run().runs[id]?.proposals.length === 0 && window.__imece.run().runs[id]?.agentEvidence === null, c, { timeout: 4000 });
  pass("C-only reject clears evidence and remains cleared after manual refresh");

  const tools = page.getByRole("button", { name: "Araçlar" });
  await tools.click();
  if (!(await page.getByRole("button", { name: "Gezgin" }).count())) throw new Error("Tools view did not expose IDE navigation");
  await page.getByRole("button", { name: "Görevler" }).click();
  pass("tools/tasks navigation remains reachable");
  if (pageErrors.length || consoleIssues.length) throw new Error(`Unexpected browser errors: ${JSON.stringify({ pageErrors, consoleIssues })}`);
  console.log(JSON.stringify({ ok: true, checks, count: checks.length, consoleWarnings: consoleIssues, pageErrors, knownMonacoCancellation }));
} finally {
  await browser?.close();
  if (vite.exitCode === null) {
    const exited = once(vite, "exit");
    vite.kill("SIGTERM");
    await Promise.race([exited, delay(3000)]);
  }
}
