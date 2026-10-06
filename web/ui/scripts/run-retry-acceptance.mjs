import { spawn } from "node:child_process";
import { once } from "node:events";
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
    if (vite.exitCode !== null) throw new Error(`Vite exited with ${vite.exitCode}`);
    try { ready = (await fetch(origin)).ok; } catch { await delay(100); }
  }
  if (!ready) throw new Error("Vite did not become ready");
  browser = await chromium.launch({ headless: true, executablePath });
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 } });
  await context.route("**/*", (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/?scenario=empty`);
  await page.waitForFunction(() => window.__imece?.bridge);
  await page.evaluate(() => window.__imece.openProject("C:/Retry/project"));
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const { useActivity } = await import("/src/state/activity.ts");
    const bridge = window.__imece.bridge;
    const fixture = { calls: [], roots: [], projectRoot: "C:/Retry/project", retryReject: false, source: { runId: "history-retry", taskId: "task-retry", task: "Canonical retry task", status: "failed", phase: "error", providerId: "provider-x", engine: "agent", changedPathCount: null, errorCode: null, readOnly: true, retryAvailable: true },
      resolveStart: null, rejectStart: null, admissionGate: null };
    window.__retryFixture = fixture;
    const detail = (runId) => ({ runId, taskId: "task-retry", task: fixture.source.task, status: "failed", phase: "error", providerId: "provider-x", engine: "agent", readOnly: true, retryAvailable: true, evidence: null, proposals: [], totals: null, errorCode: null, checkpointId: null });
    bridge.call = (method, params) => {
      fixture.calls.push({ method, params, root: useWorkspace.getState().root });
      if (method === "project.open") { fixture.projectRoot = params.path; return Promise.resolve({ root: params.path, name: params.path.split("/").at(-1) }); }
      if (method === "fs.listDir") return Promise.resolve({ entries: [] });
      if (method === "run.list") return Promise.resolve({ runs: fixture.projectRoot === "C:/Retry/project" ? [fixture.source] : [], historyUnavailable: false });
      if (method === "run.get") return Promise.resolve(detail(params.runId));
      if (method === "run.start") {
        if (JSON.stringify(params) !== JSON.stringify({ retryOfRunId: "history-retry" })) throw new Error(`noncanonical retry payload: ${JSON.stringify(params)}`);
        return new Promise((resolve, reject) => { fixture.resolveStart = resolve; fixture.rejectStart = reject; });
      }
      if (method === "window.ready") return Promise.resolve({});
      return Promise.resolve(method === "checkpoint.list" ? { checkpoints: [] } : {});
    };
    await useRun.getState().refreshRuns();
    useRun.getState().selectRun("history-retry");
    useActivity.getState().install();
  });
  await page.getByRole("button", { name: /Canonical retry task/ }).click();
  await page.getByRole("status").filter({ hasText: "Önceki oturumun kaydı" }).waitFor();
  const retryButton = page.getByRole("button", { name: "Yeniden çalıştır" });
  await retryButton.click();
  await page.waitForFunction(() => window.__retryFixture.calls.filter((call) => call.method === "run.start").length === 1);
  // A second actual button click while RPC admission is pending must not issue a duplicate.
  await retryButton.click();
  await page.waitForTimeout(50);
  let counts = await page.evaluate(() => window.__retryFixture.calls.filter((call) => call.method === "run.start").length);
  if (counts !== 1) throw new Error(`double-click issued ${counts} admissions`);

  // Events arriving before RPC resolution are buffered and replayed in order.
  await page.evaluate(() => {
    window.__imece.bridge.emit("run.event", { runId: "retry-new", ev: { type: "info", text: "early output" } });
    window.__imece.bridge.emit("run.activity", { id: "act-early", runId: "retry-new", seq: 1, ts: new Date().toISOString(), role: "worker", kind: "tool", status: "ok", title: "early activity" });
    window.__imece.bridge.emit("run.finished", { runId: "retry-new", status: "failed", error: "early failure" });
  });
  await page.evaluate(() => window.__retryFixture.resolveStart({ runId: "retry-new" }));
  await page.waitForFunction(() => window.__imece.run().runs["retry-new"]?.status === "failed");
  let result = await page.evaluate(async () => {
    const { useActivity } = await import("/src/state/activity.ts");
    const run = window.__imece.run();
    return { selected: run.selectedRunId, status: run.runs["retry-new"]?.status, flow: run.runs["retry-new"]?.flow.map((item) => item.text), activities: useActivity.getState().byRun["retry-new"]?.map((item) => item.id), calls: window.__retryFixture.calls };
  });
  if (result.selected !== "retry-new" || result.status !== "failed" || !result.flow.includes("early output") || !result.activities.includes("act-early")) throw new Error(`early admission events were lost: ${JSON.stringify(result)}`);
  if (result.flow.findIndex((text) => text.includes("early output")) > result.flow.findIndex((text) => text.includes("early failure"))) throw new Error("early events replayed out of order");

  // A changed selection stays selected when a background retry is admitted.
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const fixture = window.__retryFixture;
    useRun.getState().selectRun("history-retry");
    fixture.resolveStart = null;
    const retry = useRun.getState().retryHistory("history-retry");
    for (let i = 0; i < 100 && !fixture.resolveStart; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    if (!fixture.resolveStart) throw new Error("background retry admission did not start");
    useRun.getState().newDraft();
    useRun.getState().setTask("Keep selected draft");
    fixture.resolveStart({ runId: "retry-background" });
    await retry;
    const state = useRun.getState();
    if (state.selectedRunId !== null || state.task !== "Keep selected draft" || !state.runs["retry-background"])
      throw new Error("retry admission overwrote changed selection");
  });

  // Switching project while admission is pending fences the late response.
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const fixture = window.__retryFixture;
    useWorkspace.setState({ root: "C:/Retry/project" });
    useRun.getState().selectRun("history-retry");
    fixture.resolveStart = null;
    const retry = useRun.getState().retryHistory("history-retry");
    for (let i = 0; i < 100 && !fixture.resolveStart; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    if (!fixture.resolveStart) throw new Error("root-switch retry admission did not start");
    fixture.pendingRootResolve = fixture.resolveStart;
    fixture.pendingRetry = retry;
  });
  await page.waitForFunction(() => window.__retryFixture.calls.filter((call) => call.method === "run.start").length === 3);
  await page.evaluate(() => window.__imece.openProject("C:/Retry/other"));
  await page.evaluate(() => window.__retryFixture.pendingRootResolve({ runId: "retry-late-root" }));
  await page.waitForTimeout(100);
  result = await page.evaluate(async () => { const { useWorkspace } = await import("/src/state/workspace.ts"); return { root: useWorkspace.getState().root, projectRoot: window.__retryFixture.projectRoot, late: window.__imece.run().runs["retry-late-root"] ?? null }; });
  if (result.projectRoot !== "C:/Retry/other" || result.late) throw new Error(`late root response crossed fence: ${JSON.stringify(result)}`);

  // Ambiguous transport failure fences further retry and ordinary draft start for that root.
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const fixture = window.__retryFixture;
    await useWorkspace.getState().openProject("C:/Retry/project");
    await useRun.getState().refreshRuns();
    useRun.getState().selectRun("history-retry");
    fixture.rejectStart = null;
    const retry = useRun.getState().retryHistory("history-retry");
    for (let i = 0; i < 100 && !fixture.rejectStart; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    if (!fixture.rejectStart) throw new Error("uncertain retry admission did not start");
    fixture.rejectStart(new Error("transport lost"));
    await retry;
  });
  await page.waitForTimeout(100);
  const before = await page.evaluate(() => window.__retryFixture.calls.filter((call) => call.method === "run.start").length);
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    await useRun.getState().retryHistory("history-retry");
    useRun.getState().newDraft();
    useRun.getState().setTask("Must not start after uncertain admission");
    useRun.getState().setProviderId("provider-x");
    await useRun.getState().start();
  });
  await page.waitForTimeout(30);
  const after = await page.evaluate(() => ({ calls: window.__retryFixture.calls.filter((call) => call.method === "run.start").length, uncertain: window.__imece.run().draftUncertain }));
  if (after.calls !== before || !after.uncertain) throw new Error(`uncertain retry was not fenced: ${JSON.stringify(after)}`);
  if (errors.length) throw new Error(`Browser errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({ ok: true, checks: ["explicit canonical retry RPC", "actual double-click admission fence", "ordered early event/activity/finished replay", "selection switch fence", "late root response fence", "uncertain transport fences repeated retry"] }));
} finally {
  await browser?.close();
  if (vite.exitCode === null) {
    const exited = once(vite, "exit");
    vite.kill("SIGTERM");
    await Promise.race([exited, delay(3000)]);
  }
}
