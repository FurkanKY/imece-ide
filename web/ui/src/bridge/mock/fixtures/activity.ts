/* fixtures/activity.ts — F1 (canlı ajan etkinliği) mock verisi.
   [gecikme_ms, öğe] çiftleri; MockBridge.streamActivity() bunları run.activity
   kanalından akıtır (webshot ile Etkinlik sekmesinin ekran görüntüsü için). */

import { ActivityItem } from "../../protocol";

type Partial_ = Omit<ActivityItem, "runId" | "seq" | "ts">;

export const ACTIVITY_FEED: [number, Partial_][] = [
  [150, { id: "plan:p1", role: "planner", kind: "stage", status: "running", title: "Planlanıyor…" }],
  [400, { id: "tool:acp_planner:t1", role: "planner", kind: "tool", status: "running", title: "Okundu: src/utils.ts" }],
  [250, { id: "tool:acp_planner:t1", role: "planner", kind: "tool", status: "ok", title: "Okundu: src/utils.ts" }],
  [300, { id: "tool:acp_planner:t2", role: "planner", kind: "tool", status: "ok", title: "Arandı: format_date" }],
  [350, {
    id: "plan:p1", role: "planner", kind: "stage", status: "ok", title: "Plan tamamlandı",
    detail: "utils.py içindeki tarih dönüşümünü ISO 8601'e taşı ve çağıran yerleri doğrula.",
  }],
  [300, { id: "execution:e1", role: "worker", kind: "stage", status: "running", title: "Çalışıyor…" }],
  [350, { id: "tool:e1:c1", role: "worker", kind: "tool", status: "running", title: "Okundu: utils.py" }],
  [200, { id: "tool:e1:c1", role: "worker", kind: "tool", status: "ok", title: "Okundu: utils.py",
    detail: "def format_date(d):\n    return d.strftime('%d/%m/%Y')\n" }],
  [300, { id: "tool:e1:c2", role: "worker", kind: "tool", status: "running", title: "Düzenlendi: utils.py" }],
  [250, { id: "tool:e1:c2", role: "worker", kind: "tool", status: "ok", title: "Düzenlendi: utils.py" }],
  [200, { id: "usage:u1", role: "worker", kind: "usage", status: "info", title: "Kullanım: 650 token, $0.0004" }],
  [300, { id: "execution:e1", role: "worker", kind: "stage", status: "ok", title: "Çalışma tamamlandı" }],
  [250, { id: "verification:v1", role: "verification", kind: "stage", status: "running", title: "Doğrulama çalıştırılıyor…" }],
  [400, {
    id: "check:v1:unit", role: "verification", kind: "check", status: "running", title: "Kontrol: Unit tests",
    detail: "python3 -m pytest -q",
  }],
  [700, {
    id: "check:v1:unit", role: "verification", kind: "check", status: "ok", title: "Kontrol geçti: Unit tests",
    detail: "9 passed in 1.42s",
  }],
  [200, { id: "verification:v1", role: "verification", kind: "stage", status: "ok", title: "Doğrulama: pass" }],
  [250, { id: "review:r1", role: "reviewer", kind: "stage", status: "running", title: "İnceleniyor…" }],
  [300, { id: "activity_tool:acp_review:t1", role: "reviewer", kind: "tool", status: "ok", title: "Okundu: utils.py" }],
  [300, { id: "activity_tool:acp_review:think", role: "reviewer", kind: "note", status: "info", title: "Düşünüyor…" }],
  [400, {
    id: "review:r1", role: "reviewer", kind: "stage", status: "ok", title: "İnceleme sonucu: APPROVED",
    detail: "Dönüşüm doğru; ISO 8601 round-trip korunmuş. Temiz iş.",
  }],
];
