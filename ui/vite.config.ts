import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// local dev: vite serves the ui, the python service serves /api on 8787
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
  build: {
    // built straight into the python package so the wheel ships the dashboard
    outDir: "../taskboy/ui_dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
