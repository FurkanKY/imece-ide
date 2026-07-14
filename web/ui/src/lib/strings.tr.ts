/* strings.tr.ts — tüm görünür UI metinleri tek yerde (Türkçe).
   Not: CSS text-transform KULLANMA (İ/i sorunu) — büyük harf burada, string'de. */

export const S = {
  appName: "Imece IDE",
  window: {
    minimize: "Simge durumuna küçült",
    maximize: "Ekranı kapla",
    restore: "Aşağı geri yükle",
    close: "Kapat",
  },
  welcome: {
    title: "Imece IDE",
    subtitle: "Claude, DeepSeek ve Gemini ile çalışır.",
    openFolder: "Klasör Aç",
    hintShortcuts: "Ctrl+K komutlar · Ctrl+P dosyalar · Ctrl+` terminal",
  },
  common: {
    noProject: "Proje seçilmedi",
  },
} as const;
