/* fixtures/run.ts — tools/uishot.py RUN_PARTIAL/RUN_FULL olay dizilerinin portu.
   Mock koşu bunları zamanlamalı akıtır; ?scenario=running kısmi durur,
   result tam biter, error ortada patlar. */

import { RunEvent } from "../../protocol";

export const TASK = "utils.py'deki tarih biçimini ISO 8601 yap";

const NEW_UTILS = `from datetime import datetime


def format_date(d: datetime) -> str:
    return d.isoformat()


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s)
`;

const DIFF_UTILS = `--- a/utils.py
+++ b/utils.py
@@ -1,8 +1,8 @@
 from datetime import datetime


 def format_date(d: datetime) -> str:
-    return d.strftime('%d/%m/%Y')
+    return d.isoformat()


 def parse_date(s: str) -> datetime:
-    return datetime.strptime(s, '%d/%m/%Y')
+    return datetime.fromisoformat(s)`;

/** [gecikme_ms, olay] çiftleri — akış hissi için kademeli */
export const RUN_PARTIAL: [number, RunEvent][] = [
  [200, { type: "info", text: "Projede 9 dosya bulundu." }],
  [300, { type: "stage", stage: "plan", provider: "claude" }],
  [900, { type: "output", stage: "plan", text: "1. utils.py okunacak\n2. format_date/parse_date ISO 8601'e çevrilecek\n3. Çağıran yerler kontrol edilecek" }],
  [500, { type: "metric", stage: "plan", provider: "claude", model: "claude-code", latency_s: 8.5, tokens: 176, cost_usd: 0.029 }],
  [250, { type: "info", text: "Okunacak dosyalar: utils.py" }],
  [300, { type: "stage", stage: "code", provider: "deepseek" }],
];

export const RUN_REST: [number, RunEvent][] = [
  // gerçek Coder biçimi: ### FILE + fenced kod (sohbet markdown/renklendirme testi)
  [900, {
    type: "output", stage: "code",
    text: "### FILE: utils.py\n```python\n" + NEW_UTILS + "```",
  }],
  [300, { type: "metric", stage: "code", provider: "deepseek", model: "deepseek-v4-pro", latency_s: 5.0, tokens: 650, cost_usd: 0.0004 }],
  [300, { type: "stage", stage: "review", provider: "gemini" }],
  [900, { type: "output", stage: "review", text: "VERDICT: APPROVED\nDönüşüm doğru; ISO 8601 round-trip korunmuş. Temiz iş." }],
  [400, { type: "metric", stage: "review", provider: "gemini", model: "gemini-3.5-flash", latency_s: 3.8, tokens: 205, cost_usd: 0.00002 }],
  [200, { type: "verdict", verdict: "APPROVED", note: "Dönüşüm doğru; temiz iş." }],
  [250, { type: "diff", path: "src/utils.ts", is_new: false, diff: DIFF_UTILS }],
  [300, {
    type: "proposal",
    proposals: [{ path: "src/utils.ts", new: NEW_UTILS, diff: DIFF_UTILS, is_new: false }],
    totals: { latency_s: 17.3, tokens: 1031, cost_usd: 0.0294 },
    verdict: "APPROVED",
  }],
];

export const RUN_FULL: [number, RunEvent][] = [...RUN_PARTIAL, ...RUN_REST];
