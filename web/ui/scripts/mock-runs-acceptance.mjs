import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
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
  const context = await browser.newContext();
  await context.route("**/*", (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  await page.goto(origin);
  await page.waitForFunction(() => window.__imece?.bridge);
  const result = await page.evaluate(async () => {
    const bridge = window.__imece.bridge;
    const call = (method, params = {}) => bridge.call(method, params);
    const wait = async (test) => {
      for (let i = 0; i < 150; i++) { if (await test()) return; await new Promise((resolve) => setTimeout(resolve, 20)); }
      throw new Error("Timed out waiting for mock run state");
    };
    await call("project.open", { path: "C:/Projeler/demo-api" });
    const a = (await call("run.start", { task: "cancel A", providerId: "deepseek" })).runId;
    const b = (await call("run.start", { task: "continue B", providerId: "openai" })).runId;
    if (a === b) throw new Error("Admitted native-agent IDs were not unique");
    let capacityRefused = false;
    try { await call("run.start", { task: "third", providerId: "claude" }); } catch { capacityRefused = true; }
    if (!capacityRefused) throw new Error("Third active/waiting agent was not refused");
    await call("run.cancel", { runId: a });
    await wait(() => bridge.call("run.get", { runId: b }).then((run) => run.status === "waiting_user"));
    const bBefore = await call("run.get", { runId: b });
    const oldExecution = bBefore.evidence?.execution_id;
    await call("run.followUp", { runId: b, feedback: "continue B" });
    await wait(() => bridge.call("run.get", { runId: b }).then((run) => run.status === "waiting_user" && run.evidence?.execution_id !== oldExecution));
    const continued = await call("run.get", { runId: b });
    const c = (await call("run.start", { task: "overlapping C", providerId: "gemini" })).runId;
    await wait(() => bridge.call("run.get", { runId: c }).then((run) => run.status === "waiting_user"));
    const paths = continued.proposals.map((proposal) => proposal.path);
    const applied = await call("run.applyProposals", { runId: b, paths });
    if (applied.applied.length !== paths.length || !applied.checkpointId) throw new Error("Explicit agent proposal apply/checkpoint failed");
    const conflict = await call("run.applyProposals", { runId: c, paths });
    if (!conflict.conflicts?.length || conflict.applied.length) throw new Error("Compare-at-apply did not refuse stale overlapping proposal");
    const stored = await call("run.get", { runId: b });
    const checkpoints = await call("checkpoint.list");
    if (stored.phase !== "applied" || stored.checkpointId !== applied.checkpointId || !checkpoints.checkpoints.some((item) => item.id === applied.checkpointId)) throw new Error("Run/checkpoint store is inconsistent");
    await call("checkpoint.restore", { checkpointId: applied.checkpointId });
    await call("project.open", { path: "C:/Projeler/other-project" });
    let wrongRootRefused = false;
    try { await call("run.get", { runId: b }); } catch { wrongRootRefused = true; }
    if (!wrongRootRefused) throw new Error("run.get did not refuse old-root run");
    await call("project.open", { path: "C:/Projeler/demo-api" });
    const stillAlive = await call("run.get", { runId: a });
    if (stillAlive.status !== "cancelled") throw new Error("Cancel A was overwritten by B activity");
    return { runIds: [a, b, c], continuationExecutionId: continued.evidence.execution_id, checkpointId: applied.checkpointId };
  });
  console.log(JSON.stringify({ ok: true, ...result }));
} finally {
  await browser?.close();
  vite.kill("SIGTERM");
}
