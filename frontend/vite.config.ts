import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
