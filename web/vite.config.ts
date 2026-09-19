import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` proxies /api to the FastAPI server on :8000.
// Prod: `npm run build` → dist/, served by FastAPI itself (single origin, cookie auth).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { chunkSizeWarningLimit: 800 },
});
