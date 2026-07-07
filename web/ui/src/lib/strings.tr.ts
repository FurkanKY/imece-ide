/* strings.tr.ts — tüm görünür UI metinleri tek yerde (Türkçe).
   Not: CSS text-transform KULLANMA (İ/i sorunu) — büyük harf burada, string'de. */

export const S = {
  appName: "Multi-Agent IDE",
  window: {
    minimize: "Simge durumuna küçült",
    maximize: "Ekranı kapla",
    restore: "Aşağı geri yükle",
    close: "Kapat",
  },
  welcome: {
    title: "Multi-Agent IDE",
    subtitle: "Claude · DeepSeek · Gemini — bir yazılım ekibi gibi.",
    openFolder: "Klasör Aç",
    hintShortcuts: "Ctrl+K komutlar · Ctrl+P dosyalar · Ctrl+` terminal",
  },
  common: {
    noProject: "Proje seçilmedi",
  },
} as const;
