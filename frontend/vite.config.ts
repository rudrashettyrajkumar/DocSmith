import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev: Vite on :5173 proxies API calls to FastAPI on :8000.
// Prod: `npm run build` -> frontend/dist, served by FastAPI itself.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/agent": "http://127.0.0.1:8000",
      "/api": "http://127.0.0.1:8000",
      "/download": "http://127.0.0.1:8000",
    },
  },
});
