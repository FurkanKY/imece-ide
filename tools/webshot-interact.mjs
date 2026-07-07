import { createRequire } from "node:module";
import path from "node:path";
const require = createRequire(path.join(process.cwd(), "web", "ui", "package.json"));
const { chromium } = require("playwright");
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1320, height: 880 }, colorScheme: "dark" });
p.on("pageerror", e => console.log("[PAGEERROR]", e.message.slice(0,150)));

await p.goto("http://localhost:5173/?scenario=editor", { waitUntil: "domcontentloaded" });
await p.waitForSelector("html[data-ready]", { timeout: 20000 });
await p.waitForTimeout(400);

// 1) Ctrl+K komut paleti
await p.keyboard.press("Control+k");
await p.waitForTimeout(350);
await p.screenshot({ path: ".uishots/palette-commands.png" });
await p.keyboard.press("Escape");
await p.waitForTimeout(200);

// 2) Ctrl+P dosya paleti + fuzzy sorgu
await p.keyboard.press("Control+p");
await p.waitForTimeout(300);
await p.keyboard.type("apts");   // fuzzy: App.tsx eşleşmeli
await p.waitForTimeout(300);
await p.screenshot({ path: ".uishots/palette-files.png" });
await p.keyboard.press("Escape");
await p.waitForTimeout(200);

// 3) Gezginde sağ-tık bağlam menüsü
const row = p.locator("text=README.md").first();
await row.click({ button: "right" });
await p.waitForTimeout(350);
await p.screenshot({ path: ".uishots/context-menu.png" });
await p.keyboard.press("Escape");
await p.waitForTimeout(200);

// 4) Sil onay diyaloğu (sağ-tık → Sil)
await row.click({ button: "right" });
await p.waitForTimeout(250);
await p.locator("text=Sil").first().click();
await p.waitForTimeout(350);
await p.screenshot({ path: ".uishots/dialog-delete.png" });
await p.keyboard.press("Escape");

// 5) Dirty sekme + toast: editörde yaz, Ctrl+S → toast yok ama kaydet olur; toast'u dosya op ile göster
// Yeni dosya diyaloğu → oluştur → toast
await p.keyboard.press("Control+k");
await p.waitForTimeout(250);
await p.keyboard.type("yeni dosya");
await p.waitForTimeout(250);
await p.keyboard.press("Enter");
await p.waitForTimeout(300);
await p.keyboard.type("deneme.ts");
await p.keyboard.press("Enter");
await p.waitForTimeout(500);
await p.screenshot({ path: ".uishots/toast-newfile.png" });

console.log("OK - 5 ekran görüntüsü");
await b.close();
