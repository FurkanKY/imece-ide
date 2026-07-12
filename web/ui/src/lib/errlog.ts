/* errlog.ts — Beta-2 web hata kanalı:
   1) window.onerror + unhandledrejection → app.log (dosya log'una)
   2) Python'dan `app.error` olayı → kullanıcıya hata toast'ı (log yolu ile)
   Gizlilik: yalnız hata mesajı + stack gönderilir; istem/anahtar içeriği asla. */

import { bridge } from "@/bridge";
import { toast } from "@/components/toasts/toasts";

let installed = false;
const seen = new Set<string>(); // aynı hatayı log'a arka arkaya basma

export function logError(message: string, stack?: string) {
  const key = message.slice(0, 200);
  if (seen.has(key)) return;
  seen.add(key);
  if (seen.size > 50) seen.clear();
  void bridge.call("app.log", { level: "error", message, stack }).catch(() => {});
}

export function installErrlog() {
  if (installed) return;
  installed = true;

  window.addEventListener("error", (e) => {
    logError(String(e.message ?? "window.onerror"), e.error?.stack);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    logError(
      "unhandledrejection: " + (r instanceof Error ? r.message : String(r)),
      r instanceof Error ? r.stack : undefined,
    );
  });

  // Python tarafı yakalanmamış istisna — kullanıcı görmeli (sessiz ölüm yok)
  bridge.on("app.error", ({ message, logPath }) => {
    toast.err(`Beklenmeyen hata: ${message} — ayrıntı: ${logPath}`);
  });
}
