/* Üretilmiş Qt paketini gerçek QWebChannel + CDP üzerinden sınar. */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";
import { mkdtemp, mkdir, rm, writeFile, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { checkFrozenSupervisor } from "./supervisor-smoke.mjs";
import { boundedText, linuxSmokeEnvironment } from "./smoke-runtime.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
if (!["win32", "linux"].includes(process.platform)) throw new Error("Unsupported smoke platform");
const windows = process.platform === "win32";
const bundle = path.resolve(process.env.IMECE_PACKAGE_BUNDLE || path.join(ROOT, "dist", "ImeceIDE"));
const exe = path.join(bundle, windows ? "ImeceIDE.exe" : "ImeceIDE");
const sha256 = async (file) => createHash("sha256").update(await readFile(file)).digest("hex");
const bundleDigests = { manifestSha256: await sha256(path.join(bundle, "package-manifest.json")), executableSha256: await sha256(exe) };
const reservation = createServer();
await new Promise((resolve, reject) => reservation.once("error", reject).listen(0, "127.0.0.1", resolve));
const { port } = reservation.address();
await new Promise((resolve) => reservation.close(resolve));
const scratch = await mkdtemp(path.join(tmpdir(), "imece-package-smoke-"));
for (const name of ["home", "local", "roaming", "temp", "runtime", "path"]) await mkdir(path.join(scratch, name), { mode: 0o700 });
// No inherited provider credentials, development executables or real user stores.
const environment = windows ? {
  SystemRoot: process.env.SystemRoot, WINDIR: process.env.SystemRoot,
  COMSPEC: path.join(process.env.SystemRoot, "System32", "cmd.exe"),
  PATH: [process.env.SystemRoot, path.join(process.env.SystemRoot, "System32"), path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0")].join(";"),
  USERPROFILE: path.join(scratch, "home"), HOME: path.join(scratch, "home"),
  LOCALAPPDATA: path.join(scratch, "local"), APPDATA: path.join(scratch, "roaming"),
  TEMP: path.join(scratch, "temp"), TMP: path.join(scratch, "temp"),
} : linuxSmokeEnvironment(scratch);
let app, launchError, appClose, stderr = "";
if (process.platform === "linux") environment.QT_QPA_PLATFORM = environment.QT_QPA_PLATFORM || "offscreen";
const platform = windows ? "windows" : environment.QT_QPA_PLATFORM;
const nativeDisplay = windows || platform !== "offscreen";

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const until = async (promise, ms, fallback) => {
  let timer;
  try {
    return await Promise.race([promise, new Promise((resolve) => { timer = setTimeout(() => resolve(fallback), ms); })]);
  } finally { clearTimeout(timer); }
};
let socket;
try {
  const supervisor = await checkFrozenSupervisor(exe, environment, scratch);
  app = spawn(exe, [], {
    cwd: path.dirname(exe), windowsHide: true, stdio: ["ignore", "ignore", "pipe"],
    env: { ...environment, QTWEBENGINE_REMOTE_DEBUGGING: `127.0.0.1:${port}` },
  });
  app.stderr.on("data", (chunk) => { stderr = boundedText(stderr, chunk); });
  appClose = new Promise((resolve) => app.once("close", (code, signal) => resolve({ code, signal })));
  app.once("error", (error) => { launchError = error; });
  let target;
  let lastError;
  for (let i = 0; i < 30; i += 1) {
    if (launchError) throw launchError;
    if (app.exitCode !== null || app.signalCode !== null) throw new Error(`Package exited before UI was ready (exit=${app.exitCode}, signal=${app.signalCode})`);
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`, {
        signal: AbortSignal.timeout(2000), headers: { Connection: "close" },
      }).then((r) => r.json());
      target = targets.find((item) => item.type === "page");
      if (!target) throw new Error("CDP page hedefi yok");
      break;
    } catch (error) {
      lastError = error;
      await delay(500);
    }
  }
  if (!target) throw lastError;

  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  const errors = [];
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
    } else if (message.method === "Runtime.exceptionThrown") {
      errors.push(message.params.exceptionDetails.text);
    } else if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
      errors.push(message.params.entry.text);
    }
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP command timed out: ${method}`)); }, 15000);
    pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await command("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  const waitFor = async (expression, label) => {
    for (let i = 0; i < 40; i += 1) {
      if (await evaluate(expression)) return;
      await delay(250);
    }
    throw new Error(`${label} zaman aşımı`);
  };
  await command("Runtime.enable");
  await command("Log.enable");
  await waitFor("document.documentElement.hasAttribute('data-ready')", "UI hazır");

  const bridgePresent = await evaluate("Boolean(window.qt?.webChannelTransport)");
  let rpcId = 900000;
  const bridgeCall = (method, params) => evaluate(`new Promise((resolve) => {
      new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
        const host = channel.objects.host;
        window.__imeceSmokeHost = host;
        const id = ${++rpcId};
        host.reply.connect((raw) => {
          const message = JSON.parse(raw);
          if (message.id === id) resolve(message);
        });
        host.call(JSON.stringify({id, method:${JSON.stringify(method)}, params:${JSON.stringify(params)}}));
      });
    })`);
  const settings = await bridgeCall("settings.get", {});
  const keys = await bridgeCall("keys.status", {});
  const log = await bridgeCall("app.log", { level: "info", message: "Beta-3 package smoke" });
  const terminal = await bridgeCall("terminal.create", { cols: 80, rows: 24 });
  // Beta-3: create YETMEZ (termId ConPTY ölmeden önce döner). GERÇEK kanıt: write →
  // terminal.data olayında marker geri gelmeli (ConPTY OpenConsole.exe'yi bulabildi mi).
  let terminalWriteOk = false;
  if (terminal.ok) {
    const termId = terminal.result.termId;
    terminalWriteOk = await evaluate(`new Promise((resolve) => {
      new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
        const host = channel.objects.host;
        let done = false;
        const finish = (v) => { if (!done) { done = true; resolve(v); } };
        host.event.connect((raw) => {
          try {
            const m = JSON.parse(raw);
            if (m.channel === "terminal.data" && m.payload.termId === ${JSON.stringify(termId)}
                && String(m.payload.data).includes("SMOKE_PTY_OK")) finish(true);
          } catch {}
        });
        host.call(JSON.stringify({id: 990001, method: "terminal.write",
          params: {termId: ${JSON.stringify(termId)}, data: ${JSON.stringify(windows ? "Write-Output SMOKE_PTY_OK\r" : "printf 'SMOKE_PTY_OK\\n'\r")}}}));
        setTimeout(() => finish(false), 12000);
      });
    })`);
    await bridgeCall("terminal.kill", { termId });
  }
  const welcomeVisible = await evaluate("document.body.innerText.includes('Klasör Aç')");

  const result = {
    ...bundleDigests,
    supervisor,
    platform,
    nativeDisplay,
    rendering: windows ? "platform-default" : "frozen-linux-software-default",
    diagnostics: stderr,
    title: await evaluate("document.title"),
    url: target.url,
    bridgePresent,
    welcomeVisible,
    settingsOk: settings.ok,
    keysOk: keys.ok,
    envPath: keys.result?.envPath,
    logPath: log.result?.logPath,
    terminalOk: terminal.ok,
    terminalWriteOk,
    consoleErrors: errors,
  };
  const isolated = (value) => typeof value === "string" && path.resolve(value).startsWith(path.resolve(windows ? environment.LOCALAPPDATA : environment.XDG_DATA_HOME) + path.sep);
  result.isolatedDataPaths = isolated(result.envPath) && isolated(result.logPath);
  result.ok = Boolean(bridgePresent && welcomeVisible && settings.ok && keys.ok
    && terminal.ok && terminalWriteOk && result.isolatedDataPaths && !errors.length);
  // Exercise the real closeEvent/shutdown path, not a SIGTERM success surrogate.
  // Send without awaiting the CDP reply: quitting Qt can close its socket first.
  socket.send(JSON.stringify({ id: ++nextId, method: "Runtime.evaluate", params: {
    expression: "window.__imeceSmokeHost.call(JSON.stringify({id: 999999, method: 'window.confirmClose', params: {}})); true",
  }}));
  const closed = await until(appClose, 5000, null);
  result.normalCloseOk = closed?.code === 0 && !closed?.signal;
  result.ok = result.ok && result.normalCloseOk;
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (process.env.IMECE_PACKAGE_SMOKE_REPORT) await writeFile(process.env.IMECE_PACKAGE_SMOKE_REPORT, JSON.stringify(result, null, 2) + "\n");
  if (!result.ok) {
    process.exitCode = 1;
  }
} catch (error) {
  if (process.env.IMECE_PACKAGE_SMOKE_REPORT) {
    await writeFile(process.env.IMECE_PACKAGE_SMOKE_REPORT, JSON.stringify({
      ...bundleDigests,
      ok: false, platform, nativeDisplay,
      error: error instanceof Error ? error.message : "Package smoke failed",
      diagnostics: stderr,
      process: app ? { exitCode: app.exitCode, signalCode: app.signalCode } : null,
    }, null, 2) + "\n");
  }
  throw error;
} finally {
  if (socket) socket.close();
  let childClosed = !app;
  if (app) {
    if (app.exitCode === null && app.signalCode === null) app.kill("SIGTERM");
    if (appClose) {
      childClosed = await until(appClose.then(() => true), 3000, false);
      if (!childClosed) {
        app.kill("SIGKILL");
        childClosed = await until(appClose.then(() => true), 2000, false);
      }
    }
  }
  if (childClosed) {
    await rm(scratch, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }).catch(() => undefined);
  } else {
    process.exitCode = 1;
    if (process.env.IMECE_PACKAGE_SMOKE_REPORT) await writeFile(process.env.IMECE_PACKAGE_SMOKE_REPORT,
      JSON.stringify({ ok: false, platform, nativeDisplay, error: "Smoke child did not close; isolated scratch retained", diagnostics: stderr }, null, 2) + "\n");
  }
}
