/* Frozen helper smoke: system command only; no provider/project/LAN. */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";

async function run(exe, args, payload, env, cwd, inherited = false) {
  const child = spawn(exe, args, { env, cwd, windowsHide: true,
    stdio: inherited ? ["pipe", "pipe", "pipe", "pipe", "pipe"] : ["pipe", "pipe", "pipe"] });
  return await new Promise((resolve, reject) => {
    let output = "", receipt = "", settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => { child.kill(); finish(new Error("Supervisor smoke timed out; no quiescence assumed")); }, 20000);
    child.once("error", (error) => finish(error));
    for (const stream of [child.stdin, ...(inherited ? [child.stdio[3]] : [])])
      stream.on("error", (error) => { if (error.code !== "EPIPE") finish(error); });
    for (const stream of [child.stdout, child.stderr]) stream.on("data", (chunk) => {
      output += chunk.toString("utf8");
      if (output.length > 65536) { child.kill(); finish(new Error("Supervisor output exceeded bound")); }
    });
    if (inherited) {
      child.stdio[4].on("data", (chunk) => {
        receipt += chunk.toString("ascii");
        if (receipt.length > 1024) { child.kill(); finish(new Error("Supervisor receipt exceeded bound")); }
      });
      child.stdio[4].on("error", (error) => finish(error));
      child.stdio[3].end(payload);
      child.stdin.end();
    } else child.stdin.end(payload);
    child.once("close", (code) => finish(null, { code, output, receipt }));
  });
}

export async function checkFrozenSupervisor(exe, env, cwd, platform = process.platform) {
  if (!["win32", "linux"].includes(platform)) throw new Error("Unsupported smoke platform");
  const windows = platform === "win32", nonce = randomBytes(32).toString("hex");
  const receiptPath = path.join(cwd, "process-receipt.json"), cancelPath = path.join(cwd, "process-cancel");
  const argv = windows ? [path.join(env.SystemRoot, "System32", "cmd.exe"), "/d", "/s", "/c", "echo IMECE_SUPERVISOR_OK & exit /b 7"]
    : ["/bin/sh", "-c", "printf 'IMECE_SUPERVISOR_OK\\n'; exit 7"];
  const payload = JSON.stringify({ argv, cwd, env, stdio: "process" }) + "\n";
  const args = windows ? ["--windows-process", receiptPath, cancelPath, nonce] : ["3", "4", nonce];
  const result = await run(exe, ["--imece-process-supervisor", ...args], payload, env, cwd, !windows);
  if (result.code !== 0 || !result.output.includes("IMECE_SUPERVISOR_OK")) throw new Error("Supervisor did not execute/drain the test command");
  let raw = result.receipt;
  if (windows) {
    const receiptStat = await stat(receiptPath);
    if (!receiptStat.isFile() || receiptStat.size > 1024) throw new Error("Invalid supervisor receipt size");
    raw = await readFile(receiptPath, "utf8");
  }
  const receipt = JSON.parse(raw), keys = windows ? ["cancelled", "exit_code", "nonce", "quiescent"] : ["exit_code", "nonce", "quiescent"];
  if (JSON.stringify(Object.keys(receipt).sort()) !== JSON.stringify(keys) ||
      receipt.nonce !== nonce || receipt.quiescent !== true || receipt.exit_code !== 7 || windows && receipt.cancelled !== false)
    throw new Error("Supervisor receipt failed authentication");
  const invalid = await run(exe, ["--imece-process-supervisor", "--invalid"], "", env, cwd);
  if (invalid.code !== 125) throw new Error("Invalid dispatch did not fail closed before UI startup");
  return { commandExitCode: 7, producerQuiescent: true, invalidDispatchExitCode: 125 };
}
