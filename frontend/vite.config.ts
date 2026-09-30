import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Where the backend runs: GOVERNIX_API_URL from the shell, else from frontend/.env, else this machine.
const shellEnv = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};

export default defineConfig(({ mode }) => {
  const apiUrl = shellEnv.GOVERNIX_API_URL ?? loadEnv(mode, ".", "GOVERNIX_").GOVERNIX_API_URL;
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        // The API is served under /api in the browser and forwarded to FastAPI.
        "/api": {
          target: apiUrl || "http://localhost:8000",
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});
