/* Üretilmiş Qt paketini gerçek QWebChannel + CDP üzerinden sınar. */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const exe = path.join(ROOT, "dist", "ImeceIDE", "ImeceIDE.exe");
const port = 9333;
const windowsPath = [
  process.env.SystemRoot,
  path.join(process.env.SystemRoot, "System32"),
  path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0"),
].join(";");
const app = spawn(exe, [], {
  cwd: path.dirname(exe),
  windowsHide: true,
  // Son kullanıcı senaryosu: Python/npm PATH'te yok; yalnız Windows sistem araçları var.
  env: { ...process.env, PATH: windowsPath, QTWEBENGINE_REMOTE_DEBUGGING: String(port) },
  stdio: "ignore",
});

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
try {
  let target;
  let lastError;
  for (let i = 0; i < 30; i += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then((r) => r.json());
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
    pending.set(id, { resolve, reject });
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
          params: {termId: ${JSON.stringify(termId)}, data: "Write-Output SMOKE_PTY_OK\\r"}}));
        setTimeout(() => finish(false), 12000);
      });
    })`);
    await bridgeCall("terminal.kill", { termId });
  }
  const welcomeVisible = await evaluate("document.body.innerText.includes('Klasör Aç')");

  const result = {
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
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (!bridgePresent || !welcomeVisible || !settings.ok || !keys.ok
      || !terminal.ok || !terminalWriteOk || errors.length) {
    process.exitCode = 1;
  }
} finally {
  if (socket) socket.close();
  app.kill();
}
