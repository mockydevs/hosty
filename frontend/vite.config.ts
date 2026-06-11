/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // Point at the dev VM with: HOSTY_API_TARGET=http://<vm-ip>:8800 pnpm dev
    proxy: ((target) => ({ "/api": target, "/files": target, "/adminer": target }))(
      process.env.HOSTY_API_TARGET ?? "http://127.0.0.1:8800",
    ),
  },
  build: {
    sourcemap: false,
    chunkSizeWarningLimit: 300,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
