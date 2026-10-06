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
  await page.evaluate(() => window.__imece.openProject("C:/History/project"));
  const results = await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const bridge = window.__imece.bridge;
    const calls = [];
    let detailResolver;
    const evidence = {
      unknown: false, truncated: false, reason: "single_agent_proposal", execution_id: "old-execution",
      agent_message: "Historical result message", attempt_receipt: { model_turns: 1, tool_calls: 2 },
      changed_paths: ["src/old.ts"], diff_sha256: "a".repeat(64),
      verification: { outcome: "pass", fingerprint_complete: true, changed_content: false,
        verification_id: "old-verification", plan_id: "old-plan", checks: [{ check_id: "old-check", status: "pass" }] },
    };
    const listed = [
      { runId: "history-running", taskId: "task-old", task: "Original prior task", status: "running", phase: "executing", providerId: "agent-provider", engine: "agent", changedPathCount: null, errorCode: null, readOnly: true },
    ];
    const details = (runId) => ({ runId, taskId: "task-old", task: "Original prior task", providerId: "agent-provider",
      status: "running", phase: "executing", engine: "agent", readOnly: true, evidence,
      proposals: [{ path: "must-not-appear.ts", new: "unsafe", diff: "unsafe", is_new: true }],
      totals: { latency_s: null, tokens: null, cost_usd: null }, errorCode: null, checkpointId: "unsafe-checkpoint" });
    const realCall = bridge.call.bind(bridge);
    bridge.call = (method, params) => {
      calls.push({ method, params });
      if (method === "run.list") return Promise.resolve({ runs: useWorkspace.getState().root === "C:/History/project" ? listed : [], historyUnavailable: false });
      if (method === "run.get") return params.runId === "history-late"
        ? new Promise((resolve) => { detailResolver = resolve; }) : Promise.resolve(details(params.runId));
      return Promise.resolve(method === "checkpoint.list" ? { checkpoints: [] } : {});
    };
    await useRun.getState().refreshRuns();
    const state = useRun.getState();
    state.selectRun("history-running");
    const selected = useRun.getState();
    if (selected.status !== "done" || selected.runStage !== "history" || selected.runs["history-running"].uncertain) throw new Error(`history was represented as live/uncertain instead of historical terminal UI state: ${JSON.stringify({ status: selected.status, stage: selected.runStage, run: selected.runs["history-running"] })}`);
    if (Object.values(selected.runs).filter((run) => !run.readOnly && (run.status === "running" || run.runStage === "ready" || run.pending || run.uncertain)).length !== 0) throw new Error("history consumed live capacity");
    await state.cancel(); await state.followUp(); await state.apply(); await state.reject(); await state.restoreCheckpoint("unsafe-checkpoint");
    if (calls.some(({ method }) => ["run.cancel", "run.followUp", "run.applyProposals", "run.rejectProposals", "checkpoint.restore"].includes(method))) throw new Error("read-only store action emitted a mutation RPC");

    // Root/revision fences: a detail response from the former root cannot be installed.
    listed.push({ ...listed[0], runId: "history-late", taskId: "task-late", task: "Late old-root record" });
    const refresh = state.refreshRuns();
    for (let i = 0; i < 100 && !detailResolver; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    if (!detailResolver) throw new Error("late detail request did not start");
    useWorkspace.setState({ root: "C:/History/another-project" });
    detailResolver(details("history-late"));
    await refresh;
    const late = useRun.getState().runs["history-late"];
    if (late && (late.root !== "C:/History/project" || late.agentEvidence || late.proposals.length || late.checkpointId)) throw new Error("late detail from previous root crossed root fence");
    if (useWorkspace.getState().root !== "C:/History/another-project") throw new Error("test root did not switch during delayed detail");
    listed.pop();
    useWorkspace.setState({ root: "C:/History/project" });
    await useRun.getState().refreshRuns();
    await realCall("window.ready", {});
    return { calls, task: useRun.getState().runs["history-running"].task, provider: useRun.getState().runs["history-running"].providerId };
  });
  await page.getByRole("button", { name: /Original prior task/ }).click();
  await page.getByText("0/2 etkin veya bekleyen").waitFor();
   await page.getByRole("button", { name: /Original prior task/ }).getByText("Önceki oturum · running").waitFor();
  await page.getByRole("status").filter({ hasText: "Önceki oturumun kaydı; bu oturumda uygulanamaz/devam ettirilemez." }).waitFor();
  await page.getByRole("tab", { name: "Sonuç" }).click();
  await page.getByText("Historical result message").waitFor();
  await page.getByText(/güncel çalışma alanının doğrulaması/).waitFor();
  if (await page.getByText("must-not-appear.ts").count()) throw new Error("wire-supplied historical proposal was rendered");
  if (await page.getByRole("button", { name: "Koşuyu durdur" }).count()) throw new Error("historical running status rendered a stop control");
  await page.getByRole("button", { name: "Yeni görev taslağına taşı" }).click();
  await page.getByRole("textbox", { name: "Görev" }).waitFor();
  const stateAfterCopy = await page.evaluate(() => ({ task: window.__imece.run().task, selected: window.__imece.run().selectedRunId }));
  if (stateAfterCopy.task !== "Original prior task" || stateAfterCopy.selected !== null) throw new Error("copy-to-draft did not create a fresh task draft");
  if (results.calls.some(({ method }) => method === "run.start")) throw new Error("copy-to-draft automatically started a task");
  if (errors.length) throw new Error(`Browser errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({ ok: true, checks: ["controlled history RPC", "historical evidence display", "non-live capacity/status", "mutation RPC guards", "fresh draft copy without auto-start", "late root detail fence"], errors }));
} finally {
  await browser?.close();
  if (vite.exitCode === null) {
    const exited = once(vite, "exit");
    vite.kill("SIGTERM");
    await Promise.race([exited, delay(3000)]);
  }
}
