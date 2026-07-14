import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// base './' → göreli yollar: hem `app://ui/` özel şeması hem vite preview altında çalışır.
// Monaco worker'ları P0 smoke testinde göreli base ile doğrulanır; sorun çıkarsa
// base 'app://ui/' + worker.format 'iife' kombinasyonuna geçilir (plan, risk 5).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  worker: { format: "iife" },
  server: { port: 5173, strictPort: true },
  build: {
    target: "chrome120",
    sourcemap: false,
    // Ağır, isteğe bağlı IDE yüzeyleri kendi lazy-import sınırlarında kalır.
    // Geri kalan bağımlılıklar da kararlı vendor paketlerine ayrılır; böylece
    // küçük uygulama değişiklikleri Monaco/xterm önbelleğini geçersiz kılmaz.
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (id.includes("monaco-editor")) return "monaco";
          if (id.includes("@xterm")) return "xterm";
          // motion ve UI paketleri React'e bağlı olduğundan ayrı vendor grupları
          // birbirine import döngüsü oluşturabilir. Ağır editör/terminalden ayrı,
          // tek ortak paket hem bu döngüyü hem de başlangıç cache'ini dengeler.
          return "vendor";
        },
      },
    },
  },
});
