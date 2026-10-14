import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built output is committed and served by `ironwright platform` at /console/,
// so the one-command local story (uv only, no node at runtime) holds.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: { outDir: "dist" },
  server: { proxy: { "/workspaces": "http://localhost:8000" } },
});
