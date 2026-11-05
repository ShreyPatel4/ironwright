import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built output is gitignored (make console) and served by `ironwright platform` at
// /console/. It warns when dist is missing or older than src.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: { outDir: "dist" },
  server: { proxy: { "/workspaces": "http://localhost:8000" } },
});
