import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev: фронт на :5199, бэкенд на :8765 — проксируем API и видео
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5199,
    strictPort: true,
    proxy: {
      "/api": "http://localhost:8765",
      "/videos": "http://localhost:8765",
    },
  },
});
