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
  build: { target: "chrome120", sourcemap: false },
});
