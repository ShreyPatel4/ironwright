import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built output is gitignored (make console) and served by `ironwright platform` at
// /console/. It warns when dist is missing or older than src.
// Asset URLs are relative. The server puts a <base href> in the page that names where the
// console lives, so one build works at the root and under coconutlabs.org's path.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist" },
  server: { proxy: { "/workspaces": "http://localhost:8000" } },
});
