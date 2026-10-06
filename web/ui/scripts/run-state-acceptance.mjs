import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";

const executablePath = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const reservation = createServer();
await new Promise((resolve, reject) => reservation.once("error", reject).listen(0, "127.0.0.1", resolve));
const { port } = reservation.address();
await new Promise((resolve) => reservation.close(resolve));

const uiRoot = fileURLToPath(new URL("../", import.meta.url));
const vite = spawn(process.execPath, [fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url)), "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: uiRoot, stdio: "ignore" });
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
  await page.waitForFunction(() => window.__imece?.run);
  const results = await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useActivity } = await import("/src/state/activity.ts");
    await window.__imece.openProject("C:/Projeler/demo-api");
    await new Promise((resolve) => setTimeout(resolve, 250));
    const bridge = window.__imece.bridge;
    const pending = [];
    const calls = [];
    bridge.call = (method, params) => {
      calls.push({ method, params });
      if (method === "run.start") return new Promise((resolve) => pending.push({ resolve }));
      if (method === "run.applyProposals") return new Promise((resolve) => { window.__applyResolve = resolve; });
      if (method === "run.rejectProposals") return new Promise((resolve) => { window.__rejectResolve = resolve; });
      if (method === "run.list") return Promise.resolve({ runs: [{ runId: "waiting", taskId: "t", task: "waiting task", status: "waiting_user", phase: "waiting_user", providerId: "deepseek", engine: "agent", changedPathCount: 1, errorCode: null }, { runId: "applied", taskId: "t2", task: "applied task", status: "succeeded", phase: "applied", providerId: "deepseek", engine: "agent", changedPathCount: 0, errorCode: null }] });
      if (method === "run.get") return Promise.resolve({ runId: params.runId, task: params.runId === "waiting" ? "waiting task" : "applied task", providerId: "deepseek", status: params.runId === "waiting" ? "waiting_user" : "succeeded", phase: params.runId === "waiting" ? "waiting_user" : "applied", engine: "agent", evidence: null, proposals: [], totals: { latency_s: null, tokens: null, cost_usd: null }, errorCode: null, checkpointId: null });
      return Promise.resolve(method === "checkpoint.list" ? { checkpoints: [] } : method === "run.cancel" ? {} : {});
    };
    const waitFor = async (test) => { for (let i = 0; i < 100; i++) { if (test()) return; await new Promise((r) => setTimeout(r, 10)); } const { useDelivery } = await import("/src/state/delivery.ts"); const { useCollaboration } = await import("/src/state/collaboration.ts"); const { useWorkspace } = await import("/src/state/workspace.ts"); throw new Error(`timed out waiting for store action: ${JSON.stringify({ pending: pending.length, state: { task: useRun.getState().task, status: useRun.getState().status, token: useRun.getState().draftToken, provider: useRun.getState().providers, root: useWorkspace.getState().root, delivery: useDelivery.getState().busy, collab: useCollaboration.getState().enabled } })}`); };
    const state = () => useRun.getState();
    useRun.setState({ providers: [{ id: "deepseek", label: "Deepseek", kind: "openai", custom: false, ok: true, docsUrl: "", engineSupported: true }] });
    state().setProviderId("deepseek");
    state().newDraft(); state().setTask("task A");
    const startA = state().start();
    await waitFor(() => pending.length === 1);
     bridge.emit("run.event", { runId: "run-A", ev: { type: "info", text: "early A" } });
     bridge.emit("run.activity", { id: "early", runId: "run-A", seq: 0, ts: "t", role: "system", kind: "note", status: "info", title: "early activity A" });
    bridge.emit("run.finished", { runId: "run-A", status: "failed", error: "failure A", errorTitle: "A title", errorDescription: "A detail" });
    state().newDraft(); state().setTask("task B");
    const startB = state().start();
    await waitFor(() => pending.length === 2);
    pending[0].resolve({ runId: "run-A" }); await startA;
    if (state().task !== "task B" || state().selectedRunId !== null) throw new Error("late A admission stole the B draft");
    pending[1].resolve({ runId: "run-B" }); await startB;
    if (state().selectedRunId !== "run-B") throw new Error("current B draft was not projected after admission");
     if (!state().runs["run-A"]?.flow.some((entry) => entry.text === "early A") || state().runs["run-A"]?.errorTitle !== "A title") throw new Error("buffered event/finished was not replayed");
     await waitFor(() => useActivity.getState().byRun["run-A"]?.some((item) => item.title === "early activity A"));
    state().selectRun("run-A"); state().setTask("edited A");
    state().selectRun("run-B"); state().setTask("edited B"); state().selectRun("run-A");
    if (state().task !== "edited A") throw new Error("run selection did not preserve per-run edits");
    bridge.emit("run.activity", { id: "same", runId: "run-A", seq: 1, ts: "t", role: "system", kind: "note", status: "info", title: "A" });
    bridge.emit("run.activity", { id: "same", runId: "run-B", seq: 1, ts: "t", role: "system", kind: "note", status: "info", title: "B" });
    useActivity.getState().select("run-B");
     if (useActivity.getState().items[0]?.title !== "B") throw new Error("activity ID collision crossed run boundary");
     for (let i = 0; i < 510; i++) bridge.emit("run.activity", { id: `bounded-${i}`, runId: "run-B", seq: i + 2, ts: "t", role: "system", kind: "note", status: "info", title: `B-${i}` });
     if (useActivity.getState().byRun["run-B"].length !== 500 || useActivity.getState().byRun["run-A"].length !== 2) throw new Error("activity bounds affected another run or exceeded 500");
    state().selectRun("run-A");
    const a = state().runs["run-A"];
    useRun.setState((s) => ({ runs: { ...s.runs, "run-A": { ...a, status: "running", runStage: "working" } }, status: "running" }));
    await state().cancel();
    if (calls.at(-1)?.params?.runId !== "run-A") throw new Error("cancel omitted captured runId");
    const ready = { ...state().runs["run-A"], status: "done", runStage: "ready", diffs: [{ path: "x", isNew: false, diff: "", checked: true }], proposals: [{ path: "x", new: "x", diff: "", is_new: false }] };
    useRun.setState((s) => ({ runs: { ...s.runs, "run-A": ready }, ...({}) }));
    state().selectRun("run-A");
    const applying = state().apply();
    await waitFor(() => typeof window.__applyResolve === "function");
    state().selectRun("run-B");
    window.__applyResolve({ applied: ["x"], errors: [], conflicts: [], checkpointId: "cp-A" });
    await applying;
    if (state().runs["run-A"].checkpointId !== "cp-A" || state().selectedRunId !== "run-B") throw new Error("apply completion escaped origin run");
    state().selectRun("run-A");
    const rejecting = state().reject();
    await waitFor(() => typeof window.__rejectResolve === "function");
    state().selectRun("run-B"); window.__rejectResolve({}); await rejecting;
    if (state().runs["run-A"].runStage !== "draft" || state().selectedRunId !== "run-B") throw new Error("reject completion escaped origin run");
    state().selectRun("run-A");
    useRun.setState((s) => ({ runs: { ...s.runs, "run-A": { ...s.runs["run-A"], status: "done", runStage: "ready", engine: "agent", followUpDraft: "" } } }));
    state().setFollowUpDraft("continue A");
    await state().followUp();
    if (calls.at(-1)?.method !== "run.followUp" || calls.at(-1)?.params?.runId !== "run-A") throw new Error("follow-up omitted captured runId");
    useRun.setState((s) => ({ runs: { ...s.runs, "run-A": { ...s.runs["run-A"], root: "wrong-root" } } }));
    state().selectRun("run-A");
    const mutationCount = calls.filter((c) => c.method === "run.applyProposals").length;
    await state().apply();
    if (calls.filter((c) => c.method === "run.applyProposals").length !== mutationCount) throw new Error("wrong-root apply was not refused");
    useRun.setState({ runs: {} });
    state().newDraft();
    await state().refreshRuns();
     if (state().runs.waiting?.runStage !== "ready" || state().runs.waiting?.status !== "done" || state().runs.applied?.runStage !== "applied") throw new Error("refresh recovery mapped backend states incorrectly");
    const history = {};
    for (let i = 0; i < 36; i++) history[`terminal-${i}`] = { ...state().runs.applied, runId: `terminal-${i}`, status: "done", runStage: "noChanges", root: "C:/Projeler/demo-api" };
    history.preservedReady = { ...state().runs.waiting, runId: "preservedReady", status: "done", runStage: "ready" };
    useRun.setState({ runs: history });
    bridge.emit("run.finished", { runId: "terminal-35", status: "done" });
     if (Object.values(state().runs).filter((run) => run.runStage !== "ready").length > 32 || !state().runs.preservedReady) throw new Error("history cap evicted owned ready proof or exceeded clean-history limit");
     // A late admission belongs to its captured project, never the new root's draft.
     const { useWorkspace } = await import("/src/state/workspace.ts");
     const originalRoot = useWorkspace.getState().root;
     const controlledCall = bridge.call;
     let rootTest = true;
     bridge.call = (method, params) => rootTest && method === "run.list" ? Promise.resolve({ runs: [] }) : controlledCall(method, params);
     state().newDraft(); state().setTask("root-bound C");
     const startC = state().start();
     await waitFor(() => pending.length === 3);
     useWorkspace.setState({ root: "C:/Projeler/another-project" });
     state().setTask("new root draft");
     pending[2].resolve({ runId: "run-C" }); await startC;
     if (state().selectedRunId !== null || state().task !== "new root draft" || state().runs["run-C"].root !== originalRoot) throw new Error("late admission acquired wrong-root ownership");
     bridge.emit("run.event", { runId: "run-C", ev: { type: "diff", path: "x", diff: "C change", is_new: false } });
     bridge.emit("run.event", { runId: "run-C", ev: { type: "proposal", proposals: [{ path: "x", new: "C", diff: "C change", is_new: false }] } });
     bridge.emit("run.finished", { runId: "run-C", status: "done", engine: "agent" });
     state().selectRun("run-C");
     const beforeWrongRoot = calls.filter((call) => call.method === "run.applyProposals").length;
     await state().apply();
     if (!state().runRootStale || calls.filter((call) => call.method === "run.applyProposals").length !== beforeWrongRoot) throw new Error("old-root evidence conferred current-project apply authority");
     useWorkspace.setState({ root: originalRoot });
     state().selectRun("run-C");
     // An unconfirmed transport outcome clears only C's proof and cannot be retried.
     bridge.call = (method, params) => {
       if (method === "run.applyProposals") { calls.push({ method, params }); return Promise.reject(new Error("controlled uncertain apply")); }
       return controlledCall(method, params);
     };
     await state().apply();
     if (!state().runs["run-C"].uncertain || state().runs["run-C"].proposals.length || state().runs["run-C"].agentEvidence) throw new Error("uncertain apply retained proposal authority");
     const beforeRetry = calls.filter((call) => call.method === "run.applyProposals").length;
     await state().apply();
     if (calls.filter((call) => call.method === "run.applyProposals").length !== beforeRetry) throw new Error("uncertain apply was blindly retried");
     await new Promise((resolve) => setTimeout(resolve, 250));
     // An older detail response must not overwrite an event received during refresh.
     rootTest = false;
     let resolveDetail;
     bridge.call = (method, params) => method === "run.get" && params.runId === "waiting"
       ? new Promise((resolve) => { resolveDetail = resolve; }) : controlledCall(method, params);
     const refreshing = state().refreshRuns();
     await waitFor(() => !!resolveDetail);
     bridge.emit("run.event", { runId: "waiting", ev: { type: "info", text: "newer event during detail fetch" } });
     resolveDetail(await controlledCall("run.get", { runId: "waiting" }));
     await refreshing;
     if (!state().runs.waiting.flow.some((entry) => entry.text === "newer event during detail fetch")) throw new Error("stale detail response overwrote newer evidence state");
     return { calls: calls.filter((c) => ["run.cancel", "run.applyProposals", "run.rejectProposals"].includes(c.method)) };
  });
  console.log(JSON.stringify({ ok: true, ...results }));
} finally {
  await browser?.close();
  vite.kill("SIGTERM");
}
