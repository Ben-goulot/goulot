import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is served by the Goulot server with a strict CSP (script-src 'self', style-src 'self'):
// no inline script, no inline style attribute.
export default defineConfig({
  plugins: [react()],
  build: { modulePreload: { polyfill: false }, sourcemap: false, assetsInlineLimit: 0 },
  server: { host: "localhost", port: 5173, strictPort: true },
});
