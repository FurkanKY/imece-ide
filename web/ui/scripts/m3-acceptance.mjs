import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const cwd = fileURLToPath(new URL("..", import.meta.url));
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
    if (vite.exitCode !== null) throw new Error(`Vite exited: ${vite.exitCode}`);
    try { ready = (await fetch(origin)).ok; } catch { await delay(100); }
  }
  if (!ready) throw new Error("Vite not ready");
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || "/usr/bin/google-chrome" });
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 } });
  await context.route("**/*", (route) => route.request().url().startsWith(origin) ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/?scenario=empty`);
  await page.waitForFunction(() => window.__imece?.bridge);
  await page.evaluate(() => window.__imece.openProject("C:/M3/project"));
  await page.evaluate(async () => {
    const { useRun } = await import("/src/state/run.ts");
    const { useCandidate } = await import("/src/state/candidate.ts");
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const bridge = window.__imece.bridge;
    const fixture = { calls: [], candidates: [], delayPrepare: false, resolvePrepare: null, resolveRestart: null,
      files: { "a.txt": "before A", "b.txt": "before B" }, delayedRead: null, delayedList: false, delayedScm: false };
    window.__m3Fixture = fixture;
    const items = [
      { runId: "history-R", taskId: "task-R", task: "Continue original", providerId: "p", status: "cancelled", phase: "done", readOnly: true, continuationAvailable: true, engine: "agent" },
      ...["A", "B"].map((id) => ({ runId: id, taskId: `task-${id}`, task: `Result ${id}`, providerId: "p", status: "waiting_user", phase: "ready", engine: "agent" })),
    ];
    const receipt = () => ({ candidateId: "candidate-AB", projectRoot: "C:/M3/project", candidateDir: "C:/M3/candidates/AB", baseCommit: "a".repeat(40), selected: ["A", "B"].map((runId) => ({ runId })), changedPaths: ["a.txt", "b.txt"], verification: { status: "pass", fingerprint_complete: true }, state: "prepared", checkpointId: null });
    fixture.receipt = receipt;
    bridge.call = (method, params) => {
      fixture.calls.push({ method, params, root: useWorkspace.getState().root });
      if (method === "run.list") return Promise.resolve({ runs: useWorkspace.getState().root === "C:/M3/project" ? items : [], historyUnavailable: false });
      if (method === "run.get") {
        const item = items.find((item) => item.runId === params.runId);
        return Promise.resolve({ ...item, proposals: item.readOnly ? [] : [{ path: `${item.runId}.txt`, new: "result\n", diff: "diff", is_new: true }], evidence: null, totals: null, checkpointId: null });
      }
      if (method === "run.restart") return new Promise((resolve) => { fixture.resolveRestart = resolve; });
      if (method === "candidate.list") return Promise.resolve({ candidates: useWorkspace.getState().root === "C:/M3/project" ? fixture.candidates : [] });
      if (method === "candidate.prepare") {
        if (JSON.stringify(params.runIds) !== JSON.stringify(["A", "B"]) || params.verify !== true) throw new Error("Candidate selection/verification was implicit");
        const candidate = receipt();
        if (fixture.delayPrepare) return new Promise((resolve) => { fixture.resolvePrepare = resolve; });
        fixture.candidates = [candidate];
        return Promise.resolve({ candidate });
      }
      if (method === "fs.readFile") {
        if (fixture.delayedRead === params.rel) return new Promise((resolve) => { fixture.resolveRead = () => resolve({ content: fixture.files[params.rel] }); });
        if (!(params.rel in fixture.files)) return Promise.reject(Object.assign(new Error("missing"), { code: "not_found" }));
        return Promise.resolve({ content: fixture.files[params.rel] });
      }
      if (method === "fs.listDir") {
        if (fixture.delayedList) return new Promise((resolve) => { fixture.resolveList = () => resolve({ entries: [{ rel: "stale-from-A", name: "stale-from-A", isDir: false, ext: "" }] }); });
        return Promise.resolve({ entries: [] });
      }
      if (method === "scm.status") {
        if (fixture.delayedScm) return new Promise((resolve) => { fixture.resolveScm = () => resolve({ isRepo: true, branch: "stale-A", ahead: 0, behind: 0, staged: [], unstaged: [], busy: false, message: "" }); });
        return Promise.resolve({ isRepo: true, branch: "main", ahead: 0, behind: 0, staged: [], unstaged: [], busy: false, message: "" });
      }
      if (method === "candidate.apply") {
        fixture.files = { "a.txt": "candidate A", "b.txt": "candidate B" };
        fixture.candidates[0] = { ...fixture.candidates[0], state: "applied", checkpointId: "checkpoint-AB" };
        return Promise.resolve({ applied: ["a.txt", "b.txt"], checkpointId: "checkpoint-AB" });
      }
      if (method === "candidate.rollback") {
        fixture.files = { "a.txt": "before A", "b.txt": "before B" };
        fixture.candidates[0] = { ...fixture.candidates[0], state: "rolled_back" };
        return Promise.resolve({ restored: ["a.txt", "b.txt"] });
      }
      return Promise.resolve(method === "checkpoint.list" ? { checkpoints: [] } : {});
    };
    const { useEditor } = await import("/src/state/editor.ts");
    useEditor.setState({ tabs: ["a.txt", "b.txt"].map((rel) => ({ rel, name: rel, content: fixture.files[rel], draft: fixture.files[rel], dirty: false, tooLarge: false })) });
    await useRun.getState().refreshRuns();
    await useCandidate.getState().refresh();
    useRun.getState().selectRun("history-R");
  });
  await page.getByRole("button", { name: /Continue original/ }).click();
  await page.getByRole("button", { name: "Çalışma alanından devam et" }).click();
  await page.waitForFunction(() => window.__m3Fixture.resolveRestart);
  await page.getByRole("button", { name: "Çalışma alanından devam et" }).click();
  await page.evaluate(() => {
    window.__imece.bridge.emit("run.event", { runId: "history-R", ev: { type: "info", text: "Fresh resumed execution" } });
    window.__imece.bridge.emit("run.finished", { runId: "history-R", status: "failed", error: "fresh failure" });
    window.__m3Fixture.resolveRestart({ runId: "history-R" });
  });
  await page.waitForFunction(() => window.__imece.run().runs["history-R"]?.readOnly === false);
  const restarted = await page.evaluate(() => ({ run: window.__imece.run().runs["history-R"], calls: window.__m3Fixture.calls.filter((c) => c.method === "run.restart") }));
  if (restarted.calls.length !== 1 || restarted.run.status !== "failed" || !restarted.run.flow.some((item) => item.text === "Fresh resumed execution") || restarted.run.proposals.length) throw new Error("Restart admitted twice, lost early events, or revived old authority");

  await page.getByText(/^Sonuçları birleştir · yerel/).click();
  await page.getByRole("checkbox", { name: "Birleştirmek için seç: Result A" }).check();
  await page.getByRole("checkbox", { name: "Birleştirmek için seç: Result B" }).check();
  await page.getByRole("button", { name: "Birleştir ve doğrula" }).click();
  const row = page.locator('[data-candidate-id="candidate-AB"]');
  await row.getByText(/Doğrulama: pass/).waitFor();
  const beforeApply = await page.evaluate(() => window.__m3Fixture.calls.filter((c) => c.method === "candidate.apply").length);
  if (beforeApply) throw new Error("Candidate was automatically applied");
  await row.getByRole("button", { name: "Doğrulanmış adayı uygula" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Uygula", exact: true }).click();
  await row.getByRole("button", { name: "Birleşik adayı geri al" }).waitFor();
  let tabs = await page.evaluate(async () => (await import("/src/state/editor.ts")).useEditor.getState().tabs);
  if (tabs.find((tab) => tab.rel === "a.txt")?.content !== "candidate A" || tabs.find((tab) => tab.rel === "b.txt")?.content !== "candidate B") throw new Error(`Clean editor tabs stale after candidate apply: ${JSON.stringify(tabs)}`);
  await row.getByRole("button", { name: "Birleşik adayı geri al" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Geri Al", exact: true }).click();
  await row.getByText(/Geri alındı/).waitFor();
  tabs = await page.evaluate(async () => (await import("/src/state/editor.ts")).useEditor.getState().tabs);
  if (tabs.find((tab) => tab.rel === "a.txt")?.content !== "before A" || tabs.find((tab) => tab.rel === "b.txt")?.content !== "before B") throw new Error(`Clean editor tabs stale after candidate rollback: ${JSON.stringify(tabs)}`);
  // A user edit made while the post-apply disk refresh is pending must not be overwritten.
  await page.evaluate(async () => {
    const { useCandidate } = await import("/src/state/candidate.ts");
    window.__m3Fixture.delayedRead = "b.txt";
    window.__m3Fixture.files["a.txt"] = "candidate A"; window.__m3Fixture.files["b.txt"] = "candidate B";
    window.__m3Fixture.candidates[0] = { ...window.__m3Fixture.candidates[0], state: "prepared" };
    useCandidate.setState((state) => ({ candidates: state.candidates.map((candidate) => ({ ...candidate, state: "prepared" })) }));
    window.__m3Fixture.pendingApply = useCandidate.getState().apply("candidate-AB");
  });
  await page.getByRole("dialog").getByRole("button", { name: "Uygula", exact: true }).click();
  await page.waitForFunction(() => window.__m3Fixture.resolveRead);
  await page.evaluate(async () => {
    const { useEditor } = await import("/src/state/editor.ts");
    useEditor.getState().setDraft("b.txt", "user's concurrent edit");
    window.__m3Fixture.resolveRead();
    window.__m3Fixture.delayedRead = null;
    await window.__m3Fixture.pendingApply;
  });
  tabs = await page.evaluate(async () => (await import("/src/state/editor.ts")).useEditor.getState().tabs);
  if (tabs.find((tab) => tab.rel === "b.txt")?.draft !== "user's concurrent edit" || !tabs.find((tab) => tab.rel === "b.txt")?.dirty) throw new Error("Concurrent editor draft was overwritten by refresh");
  await page.evaluate(async () => {
    const { useEditor } = await import("/src/state/editor.ts");
    useEditor.setState((state) => ({ tabs: state.tabs.map((tab) => tab.rel === "b.txt" ? { ...tab, content: "candidate B", draft: "candidate B", dirty: false } : tab) }));
  });
  // A same-content close/reopen replaces the immutable tab object; the earlier
  // disk read must not overwrite that newer editor lifecycle.
  await page.evaluate(async () => {
    const { useCandidate } = await import("/src/state/candidate.ts");
    window.__m3Fixture.delayedRead = "a.txt";
    window.__m3Fixture.pendingRollbackIdentity = useCandidate.getState().rollback("candidate-AB");
  });
  await page.waitForFunction(async () => (await import("/src/components/dialogs/dialogs.ts")).useDialogs.getState().current?.okLabel === "Geri Al");
  await page.evaluate(async () => (await import("/src/components/dialogs/dialogs.ts")).useDialogs.getState().current.resolve(true));
  await page.waitForFunction(() => window.__m3Fixture.resolveRead);
  await page.evaluate(async () => {
    const { useEditor } = await import("/src/state/editor.ts");
    useEditor.setState((state) => ({ tabs: state.tabs.map((tab) => tab.rel === "a.txt"
      ? { ...tab, lifecycle: "reopened-same-content" } : tab) }));
    window.__m3Fixture.resolveRead(); window.__m3Fixture.delayedRead = null;
    await window.__m3Fixture.pendingRollbackIdentity;
  });
  tabs = await page.evaluate(async () => (await import("/src/state/editor.ts")).useEditor.getState().tabs);
  if (tabs.find((tab) => tab.rel === "a.txt")?.content !== "candidate A"
      || tabs.find((tab) => tab.rel === "a.txt")?.lifecycle !== "reopened-same-content") {
    throw new Error(`Stale candidate refresh overwrote a reopened same-content tab: ${JSON.stringify(tabs)}`);
  }
  // Epoch fences must catch A -> B -> A, not merely compare the final path.
  await page.evaluate(async () => {
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const { useScm } = await import("/src/state/scm.ts");
    useWorkspace.setState({ root: "C:/M3/project", children: { "": [{ rel: "sentinel", name: "sentinel", isDir: false, ext: "" }] } });
    window.__m3Fixture.delayedList = true;
    window.__m3Fixture.pendingListEpoch = useWorkspace.getState().loadDir("");
    window.__m3Fixture.delayedScm = true;
    window.__m3Fixture.pendingScmEpoch = useScm.getState().refresh("C:/M3/project");
  });
  await page.waitForFunction(() => window.__m3Fixture.resolveList && window.__m3Fixture.resolveScm);
  await page.evaluate(async () => {
    const { useWorkspace } = await import("/src/state/workspace.ts");
    useWorkspace.setState({ root: "C:/M3/other" });
    useWorkspace.setState({ root: "C:/M3/project" });
    window.__m3Fixture.resolveList(); window.__m3Fixture.resolveScm();
    window.__m3Fixture.delayedList = false; window.__m3Fixture.delayedScm = false;
    await Promise.all([window.__m3Fixture.pendingListEpoch, window.__m3Fixture.pendingScmEpoch]);
  });
  const epochFenced = await page.evaluate(async () => ({
    children: (await import("/src/state/workspace.ts")).useWorkspace.getState().children[""],
    branch: (await import("/src/state/scm.ts")).useScm.getState().branch,
  }));
  if (epochFenced.children[0]?.rel !== "sentinel" || epochFenced.branch === "stale-A") throw new Error(`A→B→A stale root response crossed epoch fence: ${JSON.stringify(epochFenced)}`);
  // A delayed filesystem/SCM reply from the old root must not alter the new project.
  await page.evaluate(async () => {
    const { useCandidate } = await import("/src/state/candidate.ts");
    const { useScm } = await import("/src/state/scm.ts");
    useCandidate.setState((state) => ({ candidates: state.candidates.map((candidate) => ({ ...candidate, state: "applied" })) }));
    window.__m3Fixture.delayedRead = "a.txt";
    window.__m3Fixture.pendingRollback = useCandidate.getState().rollback("candidate-AB");
    useScm.setState({ unstaged: [{ path: "sentinel.txt", status: "M" }] });
  });
  await page.waitForFunction(async () => (await import("/src/components/dialogs/dialogs.ts")).useDialogs.getState().current?.okLabel === "Geri Al");
  await page.evaluate(async () => (await import("/src/components/dialogs/dialogs.ts")).useDialogs.getState().current.resolve(true));
  await page.waitForFunction(() => window.__m3Fixture.resolveRead);
  await page.evaluate(async () => {
    const { useWorkspace } = await import("/src/state/workspace.ts");
    useWorkspace.setState({ root: "C:/M3/other" });
    (await import("/src/state/scm.ts")).useScm.setState({ unstaged: [{ path: "sentinel.txt", status: "M" }] });
    window.__m3Fixture.resolveRead(); window.__m3Fixture.delayedRead = null;
    await window.__m3Fixture.pendingRollback;
  });
  const fenced = await page.evaluate(async () => ({
    tabs: (await import("/src/state/editor.ts")).useEditor.getState().tabs,
    scm: (await import("/src/state/scm.ts")).useScm.getState().unstaged,
  }));
  if (!fenced.tabs.some((tab) => tab.rel === "a.txt" && tab.content === "candidate A") || fenced.scm[0]?.path !== "sentinel.txt") throw new Error(`Late candidate refresh crossed root fence: ${JSON.stringify(fenced)}`);
  await page.setViewportSize({ width: 320, height: 900 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error("M3 UI overflows at 320px");

  // A late preparation result must not replace another project's candidate list.
  await page.evaluate(async () => (await import("/src/state/workspace.ts")).useWorkspace.setState({ root: "C:/M3/project" }));
  await page.evaluate(async () => {
    const { useCandidate } = await import("/src/state/candidate.ts");
    window.__m3Fixture.delayPrepare = true;
    useCandidate.getState().toggle("A"); useCandidate.getState().toggle("B");
    window.__m3Fixture.pendingPrepare = useCandidate.getState().prepare();
  });
  await page.waitForFunction(() => window.__m3Fixture.resolvePrepare);
  await page.evaluate(async () => {
    const { useWorkspace } = await import("/src/state/workspace.ts");
    const { useCandidate } = await import("/src/state/candidate.ts");
    useWorkspace.setState({ root: "C:/M3/other" });
    window.__m3Fixture.resolvePrepare({ candidate: window.__m3Fixture.receipt() });
    await window.__m3Fixture.pendingPrepare;
    if (useCandidate.getState().candidates.length || useCandidate.getState().busy) throw new Error("Late candidate crossed root fence");
  });
  if (errors.length) throw new Error(JSON.stringify(errors));
  console.log(JSON.stringify({ ok: true, checks: ["explicit same-ID restart", "restart double-click/early-event fence", "explicit result selection and isolated verification", "confirmed apply refreshes clean tabs", "confirmed rollback refreshes clean tabs", "concurrent dirty edit survives delayed refresh", "late filesystem/SCM refresh respects root fence", "confirmed candidate apply and rollback", "320px layout", "late candidate root fence"], errors }));
} finally {
  await browser?.close();
  if (vite.exitCode === null) {
    const exited = once(vite, "exit"); vite.kill("SIGTERM");
    await Promise.race([exited, delay(3000)]);
  }
}
