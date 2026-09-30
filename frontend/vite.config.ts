import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiUrl = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.GOVERNIX_API_URL;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // The API is served under /api in the browser and forwarded to FastAPI.
      "/api": {
        target: apiUrl ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
