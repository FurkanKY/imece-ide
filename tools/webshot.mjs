/* webshot.mjs — görsel öz-doğrulama harness'ı (uishot.py'nin halefi).
   Mock-bridge'li web UI'ı gerçek Chromium'da açar, senaryoları gezer,
   .uishots/*.png üretir. Monaco/xterm DAHİL her şey görünür.

   Kullanım:
     node tools/webshot.mjs                # çalışan dev sunucusuna bağlanır (5173),
                                           # yoksa vite dev'i kendi başlatır
     node tools/webshot.mjs --scenario=empty,monaco-smoke
     BASE_URL=http://localhost:4173 node tools/webshot.mjs   # vite preview vb.
*/

import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// playwright web/ui/node_modules'te yaşar — çözünürlüğü oraya sabitle
const require = createRequire(path.join(ROOT, "web", "ui", "package.json"));
const { chromium } = require("playwright");
const OUT = path.join(ROOT, ".uishots");
const BASE = process.env.BASE_URL || "http://localhost:5173";

// senaryo → {query, readySelector}
const SCENARIOS = {
  "empty": { query: "?scenario=empty", ready: "html[data-ready]" },
  "project": { query: "?scenario=project", ready: "html[data-ready]" },
  "editor": { query: "?scenario=editor", ready: "html[data-ready]" },
  "monaco-smoke": { query: "?smoke=monaco", ready: "html[data-monaco-ready]" },
  // P2 koşu senaryoları: görev başlatılır, akış beklenir (bkz. App.tsx scenario işleme)
  "running": { query: "?scenario=running", ready: "html[data-ready]", settleMs: 3500 },
  "result": { query: "?scenario=result", ready: "html[data-ready]", settleMs: 8000 },
  "error": { query: "?scenario=error", ready: "html[data-ready]", settleMs: 3500 },
};

const argScen = process.argv.find((a) => a.startsWith("--scenario="));
const wanted = argScen ? argScen.split("=")[1].split(",") : Object.keys(SCENARIOS);

async function serverUp(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch {
    return false;
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true });

  let devProc = null;
  if (!(await serverUp(BASE))) {
    console.log("Dev sunucusu yok — vite başlatılıyor…");
    devProc = spawn("npm", ["run", "dev"], {
      cwd: path.join(ROOT, "web", "ui"),
      shell: true,
      stdio: "ignore",
    });
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (await serverUp(BASE)) break;
      if (i === 59) throw new Error("Vite dev sunucusu 30 sn içinde açılmadı.");
    }
  }

  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1320, height: 880 },
    colorScheme: "dark",
  });

  for (const name of wanted) {
    const sc = SCENARIOS[name];
    if (!sc) {
      console.warn(`bilinmeyen senaryo atlandı: ${name}`);
      continue;
    }
    await page.goto(BASE + "/" + sc.query, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(sc.ready, { timeout: 20000 });
    await page.waitForTimeout(sc.settleMs ?? 350); // font/animasyon/koşu akışı otursun
    const file = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: file });
    console.log("✓", path.relative(ROOT, file));
  }

  await browser.close();
  if (devProc) devProc.kill();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
