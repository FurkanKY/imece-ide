/* MonacoSmoke — P0 smoke testi: Monaco'nun (worker dahil) bu ortamda çalıştığını kanıtlar.
   Yalnız ?smoke=monaco ile lazy yüklenir; ana bundle'a girmez.
   data-monaco-ready="1" → webshot senkron noktası. */

import { useEffect, useRef, useState } from "react";

export default function MonacoSmoke() {
  const ref = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Monaco yükleniyor…");

  useEffect(() => {
    let disposed = false;
    (async () => {
      const monaco = await import("monaco-editor");
      // worker kablolaması: Vite ?worker importları
      const [editorWorker, tsWorker] = await Promise.all([
        import("monaco-editor/esm/vs/editor/editor.worker?worker"),
        import("monaco-editor/esm/vs/language/typescript/ts.worker?worker"),
      ]);
      self.MonacoEnvironment = {
        getWorker(_: unknown, label: string) {
          if (label === "typescript" || label === "javascript") return new tsWorker.default();
          return new editorWorker.default();
        },
      };
      if (disposed || !ref.current) return;
      const ed = monaco.editor.create(ref.current, {
        value: [
          "// Monaco worker smoke testi",
          "function selam(ad: string): string {",
          "  return `Merhaba ${ad}!`;",
          "}",
          "selam(42); // ← tip hatası: worker çalışıyorsa kırmızı çizer",
        ].join("\n"),
        language: "typescript",
        theme: "vs-dark",
        automaticLayout: true,
        fontFamily: "JetBrains Mono",
        fontSize: 13,
      });
      // worker kanıtı: TS diagnostics'in gelmesini bekle
      const check = setInterval(() => {
        const markers = monaco.editor.getModelMarkers({});
        if (markers.length > 0) {
          clearInterval(check);
          setStatus(`✓ Worker çalışıyor. ${markers.length} diagnostic üretildi`);
          document.documentElement.dataset.monacoReady = "1";
        }
      }, 250);
      setTimeout(() => {
        clearInterval(check);
        setStatus((s) =>
          s.startsWith("✓") ? s : "⚠ Diagnostics gelmedi. Worker fallback gerekebilir",
        );
        document.documentElement.dataset.monacoReady = "1";
      }, 8000);
      return () => ed.dispose();
    })();
    return () => {
      disposed = true;
    };
  }, []);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border-w bg-panel px-3 py-2 text-text2"
           style={{ fontSize: "var(--t-label)" }}>
        {status}
      </div>
      <div ref={ref} className="flex-1" />
    </div>
  );
}
